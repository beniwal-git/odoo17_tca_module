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
          - The invoices are not already in-flight on TCA

        The buyer's Peppol registration is NOT checked here — TCA handles
        routing to the buyer's AP. The seller just uploads the XML.
        """
        for wizard in self:
            if not wizard.company_id.tca_is_active:
                wizard.enable_tca = False
                continue

            wizard.enable_tca = not all(
                m.tca_move_state in ('processing', 'delivered', 'buyer_confirmed')
                for m in wizard.move_ids
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
    # ENSURE XML IS GENERATED WHEN SENDING VIA TCA (but hide the separate checkbox)
    # ──────────────────────────────────────────────────────────────────────────

    @api.depends('checkbox_send_tca')
    def _compute_checkbox_ubl_cii_xml(self):
        super()._compute_checkbox_ubl_cii_xml()
        for wizard in self:
            if wizard.checkbox_send_tca and wizard.enable_ubl_cii_xml:
                wizard.checkbox_ubl_cii_xml = True

    def _needs_ubl_cii_placeholder(self):
        # Hide the "PINT AE (UAE Peppol)" XML checkbox when sending via TCA —
        # TCA handles XML generation internally, user doesn't need to see it separately
        return super()._needs_ubl_cii_placeholder() and not self.checkbox_send_tca

    # ──────────────────────────────────────────────────────────────────────────
    # XML POST-PROCESSING — fix XSD order for PDF AdditionalDocumentReference
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _postprocess_invoice_ubl_xml(self, invoice, invoice_data):
        """
        EXTENDS account_edi_ubl_cii wizard.

        Parent inserts the PDF AdditionalDocumentReference at the index of
        AccountingSupplierParty, putting it AFTER our PINT AE ProjectReference
        (which was injected before AccountingSupplierParty via QWeb xpath).
        UBL InvoiceType XSD requires AdditionalDocumentReference < ProjectReference,
        so the parent's anchor choice produces an XSD-invalid sequence for PINT AE.

        Workaround: temporarily inject a sentinel ProjectReference removal so
        parent's anchor lookup hits the correct insertion point, then restore.
        Cleaner: re-walk the tree after super() and reorder if needed.
        """
        super()._postprocess_invoice_ubl_xml(invoice, invoice_data)

        from lxml import etree
        try:
            raw = invoice_data.get('ubl_cii_xml_attachment_values', {}).get('raw')
            if not raw:
                return
            tree = etree.fromstring(raw)
            # Find positions of AdditionalDocumentReference (last) + ProjectReference
            adrs = tree.xpath("./*[local-name()='AdditionalDocumentReference']")
            if not adrs:
                return
            project_refs = tree.xpath("./*[local-name()='ProjectReference']")
            if not project_refs:
                return
            last_adr = adrs[-1]
            first_project = project_refs[0]
            adr_idx = tree.index(last_adr)
            project_idx = tree.index(first_project)
            # If AdditionalDocumentReference is AFTER ProjectReference, swap to fix XSD order
            if adr_idx > project_idx:
                tree.remove(last_adr)
                tree.insert(project_idx, last_adr)
                invoice_data['ubl_cii_xml_attachment_values']['raw'] = etree.tostring(
                    tree, xml_declaration=True, encoding='UTF-8'
                )
        except Exception as exc:
            _logger.warning('TCA: PINT AE postprocess reorder failed: %s', exc)

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
        """
        super()._call_web_service_after_invoice_pdf_render(invoices_data)

        from psycopg2 import OperationalError
        from urllib.error import HTTPError, URLError

        api_svc = self.env['tca.api.service']

        for invoice, invoice_data in invoices_data.items():
            if not invoice_data.get('send_tca'):
                continue
            if invoice.tca_move_state in ('processing', 'delivered', 'buyer_confirmed'):
                _logger.info('TCA: skipping already-submitted invoice %s', invoice.name)
                continue

            # Resolve company per invoice. Standard `account.move.send`
            # supports multi-company batches; using a single company for the
            # whole batch would route every invoice through the first
            # invoice's TCA credentials and corrupt state on the others.
            company = invoice.company_id

            # ── #8: Idempotency guard — lock invoice row ─────────────────────
            try:
                with self.env.cr.savepoint(flush=False):
                    self.env.cr.execute(
                        'SELECT id FROM account_move WHERE id = %s FOR UPDATE NOWAIT',
                        [invoice.id]
                    )
            except OperationalError:
                _logger.info('TCA: invoice %s locked by another transaction, skipping', invoice.name)
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

            # ── #7: Pre-submission PINT AE validation ────────────────────────
            # Only run for PINT AE partners — running PINT-AE-specific vals/constraints
            # on a non-PINT-AE builder (e.g. plain bis3) produces irrelevant keys and
            # may surface false-positive errors.
            if partner.ubl_cii_format == 'ubl_pint_ae':
                builder = partner._get_edi_builder()
                if hasattr(builder, '_export_invoice_constraints'):
                    try:
                        vals = builder._export_invoice_vals(invoice)
                        constraints = builder._export_invoice_constraints(invoice, vals)
                        # Parent returns {key: None} for passed checks — filter them out
                        errors = {k: v for k, v in constraints.items() if v}
                        if errors:
                            error_msg = '\n'.join(errors.values())
                            invoice.tca_move_state = 'error'
                            invoice.tca_submission_error = error_msg
                            invoice_data['error'] = error_msg
                            invoice._message_log(body=_('TCA PINT AE validation failed:\n%s', error_msg))
                            continue
                    except Exception as exc:
                        _logger.warning('TCA: pre-validation failed for %s: %s', invoice.name, exc)

            # ── 2b. Schematron validation on generated XML ───────────────────
            sch_validator = self.env['tca.schematron.validator']
            if sch_validator.is_available():
                sch_result = sch_validator.validate_xml(
                    xml_bytes,
                    is_credit_note=invoice.move_type in ('out_refund', 'in_refund'),
                )
                if not sch_result.get('skipped') and not sch_result.get('valid'):
                    fatal_msgs = [e['message'] for e in sch_result['fatal_errors']]
                    error_msg = _('PINT AE XML validation errors:\n\n%s',
                                  '\n'.join(f'• {m}' for m in fatal_msgs[:10]))
                    invoice.tca_move_state = 'error'
                    invoice.tca_submission_error = error_msg
                    invoice_data['error'] = error_msg
                    invoice._message_log(body=error_msg)
                    continue
                # Log warnings but don't block
                for w in sch_result.get('warnings', []):
                    _logger.info('TCA schematron warning for %s: %s', invoice.name, w['message'])

            # ── 3. Step 1: Get document upload URL from TCA ──────────────────
            invoice.tca_move_state = 'uploading'
            try:
                upload_response = api_svc.get_document_upload_url(company, filename=xml_filename)
                upload_url = upload_response.get('upload_url')
                source_file_path = (
                    upload_response.get('path')
                    or upload_response.get('s3_uri')
                    or upload_response.get('s3_path')
                    or upload_response.get('file_key')
                )
                if not upload_url or not source_file_path:
                    raise UserError(_(
                        'TCA did not return a valid upload URL. Response: %s', upload_response
                    ))
            except (URLError, TimeoutError) as exc:
                # #5: Transient error — keep as 'submitted' so cron retries
                invoice.tca_move_state = 'submitted'
                invoice.tca_submission_error = str(exc)
                invoice._message_log(body=_('TCA Peppol: transient error getting upload URL, will retry: %s', exc))
                if self._can_commit():
                    self._cr.commit()
                continue
            except Exception as exc:
                invoice.tca_move_state = 'error'
                invoice.tca_submission_error = str(exc)
                invoice_data['error'] = str(exc)
                invoice._message_log(body=_('TCA Peppol upload URL error: %s', exc))
                continue

            # ── 4. Step 2: PUT XML bytes to S3 presigned URL ─────────────────
            try:
                api_svc.upload_to_s3(upload_url, xml_bytes)
            except (URLError, TimeoutError) as exc:
                invoice.tca_move_state = 'submitted'
                invoice.tca_submission_error = str(exc)
                invoice._message_log(body=_('TCA Peppol: transient S3 upload error, will retry: %s', exc))
                if self._can_commit():
                    self._cr.commit()
                continue
            except Exception as exc:
                invoice.tca_move_state = 'error'
                invoice.tca_submission_error = str(exc)
                invoice_data['error'] = str(exc)
                invoice._message_log(body=_('TCA S3 upload error: %s', exc))
                continue

            # ── 5. Step 3: Register invoice with TCA ─────────────────────────
            # UAE compliance: each submission carries a unique invoice_number
            # (TCA rejects re-use of the same ID). Build it from the helper
            # on account.move so this wizard path matches the credit-note
            # atomic-post path (account_move._tca_submit_outbound).
            submission_id = invoice._tca_build_submission_id()
            try:
                result = api_svc.submit_invoice(
                    company=company,
                    name=submission_id,
                    invoice_number=submission_id,
                    source_file_path=source_file_path,
                )
            except UserError as exc:
                # #5: Transient errors
                if any(t in str(exc).lower() for t in ('503', 'timeout', 'cannot reach', 'urlopen')):
                    invoice.tca_move_state = 'submitted'
                    invoice.tca_submission_error = str(exc)
                    invoice._message_log(body=_('TCA Peppol: transient submission error, will retry: %s', exc))
                    if self._can_commit():
                        self._cr.commit()
                    continue
                # Permanent error
                invoice.tca_move_state = 'error'
                invoice.tca_submission_error = str(exc)
                invoice_data['error'] = str(exc)
                invoice._message_log(body=_('TCA invoice submission error: %s', exc))
                continue
            except (URLError, TimeoutError) as exc:
                # #5: Transient network error
                invoice.tca_move_state = 'submitted'
                invoice.tca_submission_error = str(exc)
                invoice._message_log(body=_('TCA Peppol: transient submission error, will retry: %s', exc))
                if self._can_commit():
                    self._cr.commit()
                continue

            # ── #4: Handle 409/400-already-exists duplicate as success ───────
            # Defensive: should not happen with our per-attempt unique
            # submission_id, but catches state-desync edge cases.
            if result.get('tca_duplicate'):
                _logger.info('TCA: invoice %s already exists on TCA, treating as success', invoice.name)
                invoice.write({
                    'tca_move_state': 'submitted',
                    'tca_submission_error': False,
                    'tca_last_submission_id': submission_id,
                })
                invoice._message_log(body=_('TCA: Invoice already registered (duplicate). Status will sync via cron.'))
                if self._can_commit():
                    self._cr.commit()
                continue

            # ── 6. Success: store TCA id, submission id, mark submitted ─────
            tca_id = result.get('id', '')
            invoice.write({
                'tca_invoice_uuid': tca_id,
                'tca_move_state': 'submitted',
                'tca_submission_error': False,
                'tca_last_submission_id': submission_id,
            })
            invoice._message_log(
                body=_('Invoice submitted to TCA Peppol network. '
                       'TCA invoice_number: %(sid)s — TCA ID: %(tid)s',
                       sid=submission_id, tid=tca_id)
            )
            _logger.info('TCA: invoice %s submitted (sid=%s, tcaid=%s)', invoice.name, submission_id, tca_id)

            # ── #1: Per-invoice commit — don't lose this on later failures ───
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
