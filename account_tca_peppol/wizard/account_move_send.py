# -*- coding: utf-8 -*-
# Part of TCA. See LICENSE file for full copyright and licensing details.
"""
Override of account.move.send wizard to intercept the Peppol send step
and route outbound invoices through TCA instead of the native IAP proxy.

Flow:
  1. User opens "Send & Print" dialog for a posted invoice
  2. Wizard shows checkbox_send_tca when company has TCA active + partner is PINT AE
  3. User confirms → action_send_and_print() is called
  4. _call_web_service_after_invoice_pdf_render() is our override:
     a. Get UBL XML from invoice_data or existing attachment
     b. POST /api/v1/documents/              → { upload_url, s3_uri }
     c. PUT XML bytes to S3 presigned URL    (no auth)
     d. POST /api/v1/invoices/               { name, invoice_number, source_file_path }
     e. Store response 'id' as tca_invoice_uuid; set tca_move_state = 'submitted'
     f. On error: set tca_move_state = 'error' + log to chatter
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AccountMoveSend(models.TransientModel):
    _inherit = 'account.move.send'

    # ── New wizard field ───────────────────────────────────────────────────────

    checkbox_send_tca = fields.Boolean(
        string='Send via TCA Peppol',
        compute='_compute_checkbox_send_tca',
        store=True,
        readonly=False,
        help='Submit this invoice to the TCA Peppol Access Point for UAE e-invoicing.',
    )
    enable_tca = fields.Boolean(compute='_compute_enable_tca')
    tca_warning = fields.Char(string='TCA Warning', compute='_compute_tca_warning')

    # ──────────────────────────────────────────────────────────────────────────
    # COMPUTE
    # ──────────────────────────────────────────────────────────────────────────

    @api.depends('move_ids', 'enable_ubl_cii_xml')
    def _compute_enable_tca(self):
        """
        Show the TCA Peppol send option when:
          - The company has TCA integration active
          - All invoices in the batch have a PINT AE-format partner
          - The UBL XML option is available (or XML already exists on the move)

        NOTE: do NOT include 'enable_tca' in depends — it is the computed field itself
        and would create a circular dependency.
        """
        for wizard in self:
            if not wizard.company_id.tca_is_active:
                wizard.enable_tca = False
                continue

            non_pint = wizard.move_ids.partner_id.commercial_partner_id.filtered(
                lambda p: p.ubl_cii_format != 'ubl_pint_ae'
            )
            wizard.enable_tca = (
                not non_pint
                and (
                    wizard.enable_ubl_cii_xml
                    or any(
                        m.ubl_cii_xml_id and m.tca_move_state not in ('processing', 'delivered', 'received')
                        for m in wizard.move_ids
                    )
                )
            )

    @api.depends('enable_tca', 'move_ids', 'tca_warning')
    def _compute_checkbox_send_tca(self):
        """
        Auto-tick the TCA send checkbox when:
          - TCA option is available (enable_tca = True)
          - No configuration warning
          - Company has 'invoice_is_tca' set (analogous to invoice_is_ubl_cii)
        """
        for wizard in self:
            wizard.checkbox_send_tca = (
                wizard.enable_tca
                and not wizard.tca_warning
                and wizard.company_id.invoice_is_tca
            )

    @api.depends('move_ids')
    def _compute_tca_warning(self):
        for wizard in self:
            invalid = wizard.move_ids.partner_id.commercial_partner_id.filtered(
                lambda p: p.ubl_cii_format == 'ubl_pint_ae'
                and (not p.peppol_eas or not p.peppol_endpoint)
            )
            if invalid:
                names = ', '.join(invalid[:3].mapped('display_name'))
                wizard.tca_warning = _(
                    'Partners missing Peppol EAS/Endpoint: %s. '
                    'Configure Peppol settings on the partner before sending.', names
                )
            else:
                wizard.tca_warning = False

    # ──────────────────────────────────────────────────────────────────────────
    # WIZARD VALUES
    # ──────────────────────────────────────────────────────────────────────────

    def _get_wizard_values(self):
        values = super()._get_wizard_values()
        values['send_tca'] = self.checkbox_send_tca
        return values

    @api.model
    def _get_wizard_vals_restrict_to(self, only_options):
        values = super()._get_wizard_vals_restrict_to(only_options)
        return {'checkbox_send_tca': False, **values}

    # ──────────────────────────────────────────────────────────────────────────
    # ENSURE XML IS GENERATED WHEN SENDING VIA TCA
    # ──────────────────────────────────────────────────────────────────────────

    @api.depends('checkbox_send_tca')
    def _compute_checkbox_ubl_cii_xml(self):
        super()._compute_checkbox_ubl_cii_xml()
        for wizard in self:
            if wizard.checkbox_send_tca and wizard.enable_ubl_cii_xml:
                wizard.checkbox_ubl_cii_xml = True

    # ──────────────────────────────────────────────────────────────────────────
    # SEND ACTION
    # ──────────────────────────────────────────────────────────────────────────

    def action_send_and_print(self, force_synchronous=False, allow_fallback_pdf=False, **kwargs):
        """
        EXTENDS account.move.send.
        Mark invoices as 'uploading' before sending so the user sees
        immediate state feedback.
        """
        self.ensure_one()
        if self.checkbox_send_tca and self.enable_tca:
            # Ensure XML checkbox is ticked
            if self.enable_ubl_cii_xml and not self.checkbox_ubl_cii_xml:
                self.checkbox_ubl_cii_xml = True
            # Mark as uploading (will be updated in _call_web_service_after_invoice_pdf_render)
            for move in self.move_ids:
                if move.tca_move_state in ('not_sent', 'error', 'rejected'):
                    move.sudo().tca_move_state = 'uploading'
        return super().action_send_and_print(
            force_synchronous=force_synchronous,
            allow_fallback_pdf=allow_fallback_pdf,
            **kwargs
        )

    # ──────────────────────────────────────────────────────────────────────────
    # MAIN SEND OVERRIDE — this is where TCA submission happens
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _call_web_service_after_invoice_pdf_render(self, invoices_data):
        """
        OVERRIDES account.move.send (from account_edi_ubl_cii or base).
        Handles TCA submission for invoices flagged with send_tca=True.
        Native Peppol (account_peppol) is conflicted, so super() here
        calls the base account module's no-op version.
        """
        # Let the parent handle any other EDI (though account_peppol is conflicted)
        super()._call_web_service_after_invoice_pdf_render(invoices_data)

        api_svc = self.env['tca.api.service']
        company = next(iter(invoices_data)).company_id if invoices_data else self.env.company

        for invoice, invoice_data in invoices_data.items():
            if not invoice_data.get('send_tca'):
                continue
            if invoice.tca_move_state in ('processing', 'delivered', 'received'):
                _logger.info('TCA: skipping already-submitted invoice %s', invoice.name)
                continue

            # ── 1. Get the UBL XML bytes ──────────────────────────────────────
            if invoice_data.get('ubl_cii_xml_attachment_values'):
                xml_bytes = invoice_data['ubl_cii_xml_attachment_values']['raw']
                xml_filename = invoice_data['ubl_cii_xml_attachment_values']['name']
            elif invoice.ubl_cii_xml_id:
                xml_bytes = invoice.ubl_cii_xml_id.raw
                xml_filename = invoice.ubl_cii_xml_id.name
            else:
                invoice.tca_move_state = 'error'
                invoice.tca_submission_error = 'No UBL XML attachment found.'
                invoice_data['error'] = _('TCA: No UBL XML found for invoice %s.', invoice.name)
                continue

            # ── 2. Validate partner Peppol config ────────────────────────────
            partner = invoice.partner_id.commercial_partner_id
            if not partner.peppol_eas or not partner.peppol_endpoint:
                invoice.tca_move_state = 'error'
                error_msg = _('Partner %s is missing Peppol EAS and/or Endpoint.', partner.name)
                invoice.tca_submission_error = error_msg
                invoice_data['error'] = error_msg
                continue

            # ── 3. Step 1: Get document upload URL from TCA ──────────────────
            invoice.tca_move_state = 'uploading'
            try:
                upload_response = api_svc.get_document_upload_url(company)
                # API returns: { upload_url, s3_uri, expires_in }
                upload_url = upload_response.get('upload_url')
                source_file_path = (
                    upload_response.get('s3_uri')
                    or upload_response.get('s3_path')
                    or upload_response.get('file_key')
                )
                if not upload_url or not source_file_path:
                    raise UserError(_(
                        'TCA did not return a valid upload URL. Response: %s', upload_response
                    ))
            except Exception as exc:
                invoice.tca_move_state = 'error'
                invoice.tca_submission_error = str(exc)
                invoice_data['error'] = str(exc)
                invoice._message_log(body=_('TCA Peppol upload URL error: %s', exc))
                continue

            # ── 4. Step 2: PUT XML bytes to S3 presigned URL ─────────────────
            try:
                api_svc.upload_to_s3(upload_url, xml_bytes)
            except Exception as exc:
                invoice.tca_move_state = 'error'
                invoice.tca_submission_error = str(exc)
                invoice_data['error'] = str(exc)
                invoice._message_log(body=_('TCA S3 upload error: %s', exc))
                continue

            # ── 5. Step 3: Register invoice with TCA ─────────────────────────
            try:
                result = api_svc.submit_invoice(
                    company=company,
                    name=invoice.name,
                    invoice_number=invoice.name,
                    source_file_path=source_file_path,
                )
            except Exception as exc:
                invoice.tca_move_state = 'error'
                invoice.tca_submission_error = str(exc)
                invoice_data['error'] = str(exc)
                invoice._message_log(body=_('TCA invoice submission error: %s', exc))
                continue

            # ── 6. Success: store TCA id and move to submitted ───────────────
            tca_id = result.get('id', '')
            invoice.write({
                'tca_invoice_uuid': tca_id,
                'tca_move_state': 'submitted',
                'tca_submission_error': False,
            })
            invoice._message_log(
                body=_('Invoice submitted to TCA Peppol network. ID: %s', tca_id)
            )
            _logger.info(
                'TCA: invoice %s submitted successfully. ID=%s', invoice.name, tca_id
            )

        if self._can_commit():
            self._cr.commit()

    # ──────────────────────────────────────────────────────────────────────────
    # ERROR HOOK
    # ──────────────────────────────────────────────────────────────────────────

    def _hook_if_errors(self, moves_data, from_cron=False, allow_fallback_pdf=False):
        """
        EXTENDS account.move.send.
        Reset tca_move_state to 'error' for moves that failed PDF/XML generation.
        """
        for move, move_data in moves_data.items():
            if move_data.get('send_tca') and move_data.get('blocking_error'):
                move.tca_move_state = 'error'
                move.tca_submission_error = move_data.get('error', 'PDF/XML generation failed.')
        return super()._hook_if_errors(
            moves_data, from_cron=from_cron, allow_fallback_pdf=allow_fallback_pdf
        )
