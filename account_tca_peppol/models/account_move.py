# -*- coding: utf-8 -*-
# Part of TCA. See LICENSE file for full copyright and licensing details.
"""
TCA-specific fields and overrides on account.move.

State machine (tca_move_state):
  not_sent   → uploading   Triggered when user sends via TCA
  uploading  → submitted   S3 + POST /api/v1/invoices/ succeeded
  submitted  → processing  Status poll: status = 1 (Processing)
  processing → delivered   Status poll: status = 2 + c3_mls_status = 4 (Accepted)
  delivered  → received    Status poll: c5_mls_status = 4 (Accepted)
  * → error                Status poll: status = 4 (Failed)
  * → rejected             Status poll: status = 3 (Rejected)

Cancel block: invoices in processing/delivered/received states cannot be cancelled.
_get_ubl_cii_builder_from_xml_tree: PINT AE CustomizationID routed to our builder.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# States that block cancellation (document is in-flight or completed)
_CANCEL_BLOCKED_STATES = frozenset(['processing', 'delivered', 'received'])

# BTAE-03: Credit note reason codes (AE-CreditReason code list per UAE VAT Decree-Law)
CREDIT_NOTE_REASONS = [
    ('DL8.61.1.A', 'DL8.61.1.A — Supply was cancelled'),
    ('DL8.61.1.B', 'DL8.61.1.B — Tax treatment changed'),
    ('DL8.61.1.C', 'DL8.61.1.C — Consideration altered / Bad debt relief'),
    ('DL8.61.1.D', 'DL8.61.1.D — Goods/services returned'),
    ('DL8.61.1.E', 'DL8.61.1.E — Tax charged or applied in error'),
    ('VD', 'VD — Volume Discount (no preceding invoice reference required)'),
]

# TCA Invoice Status integer codes (from API spec)
TCA_STATUS_PROCESSING = 1
TCA_STATUS_COMPLETED  = 2
TCA_STATUS_REJECTED   = 3
TCA_STATUS_FAILED     = 4

# TCA C3 MLS status integer codes
TCA_C3_ACCEPTED            = 4   # Delivered to buyer AP
TCA_C3_REJECTED            = 5
TCA_C3_UNABLE_TO_DELIVER   = 6

# TCA C5 MLS status integer codes
TCA_C5_ACCEPTED            = 4   # Buyer confirmed receipt

# PINT AE CustomizationID — must be checked before the BIS3 urn:cen.eu prefix check
PINT_AE_CUSTOMIZATION_ID = 'urn:peppol:pint:billing-1@ae-1'


class AccountMove(models.Model):
    _inherit = 'account.move'

    # ── TCA state machine ──────────────────────────────────────────────────────

    tca_move_state = fields.Selection(
        selection=[
            ('not_sent', 'Not Sent'),
            ('uploading', 'Uploading'),
            ('submitted', 'Submitted to TCA'),
            ('processing', 'Processing (In Transit)'),
            ('delivered', 'Delivered to Buyer AP'),
            ('received', 'Confirmed by Buyer'),
            ('error', 'Error'),
            ('rejected', 'Rejected'),
            ('cancelled', 'Cancelled'),
        ],
        string='TCA Peppol Status',
        default='not_sent',
        copy=False,
        tracking=True,
        help=(
            'Lifecycle state of this invoice on the TCA Peppol network.\n'
            'not_sent: not yet submitted\n'
            'uploading: uploading XML to TCA storage\n'
            'submitted: registered with TCA, workflow starting\n'
            'processing: TCA sending to Peppol C3 Access Point\n'
            'delivered: C3 confirmed delivery to buyer AP\n'
            'received: buyer C5 confirmed receipt\n'
            'error: submission or delivery error — see chatter\n'
            'rejected: rejected by Peppol network or buyer AP'
        ),
    )
    tca_invoice_uuid = fields.Char(
        string='TCA Invoice UUID',
        copy=False,
        readonly=True,
        help='UUID assigned by TCA after successful submission. Used for status polling and webhook matching.',
    )
    tca_submission_error = fields.Text(
        string='TCA Last Error',
        copy=False,
        readonly=True,
        help='Last error message received from TCA. Cleared on successful resubmission.',
    )
    tca_is_inbound = fields.Boolean(
        string='TCA Inbound',
        default=False,
        copy=False,
        readonly=True,
        help='True if this invoice was received via TCA Peppol (direction = RECEIVED).',
    )

    # ── Inbound accept/reject ──────────────────────────────────────────────────

    tca_inbound_status = fields.Selection(
        selection=[
            ('pending', 'Pending Review'),
            ('accepted', 'Accepted'),
            ('rejected', 'Rejected'),
        ],
        string='Inbound Decision',
        copy=False,
        tracking=True,
        help='Buyer decision on an inbound invoice received via TCA Peppol.',
    )
    tca_reject_reason = fields.Text(
        string='Rejection Reason',
        copy=False,
        help='Reason for rejecting this inbound invoice. Logged in chatter.',
    )

    # ── PINT AE XML fields ────────────────────────────────────────────────────

    tca_transaction_type_flags = fields.Char(
        string='Transaction Type Flags (BTAE-02)',
        size=8,
        default='00000000',
        copy=True,
        help=(
            'BTAE-02: 8-digit binary flag string for the PINT AE ProfileExecutionID.\n'
            'Each position is 0 (false) or 1 (true):\n'
            '  Pos 1: Free Trade Zone\n'
            '  Pos 2: Deemed Supply\n'
            '  Pos 3: Margin Scheme\n'
            '  Pos 4: Summary Invoice\n'
            '  Pos 5: Continuous Supply\n'
            '  Pos 6: Disclosed Agent Billing\n'
            '  Pos 7: E-commerce\n'
            '  Pos 8: Exports\n'
            'Standard domestic B2B/Reverse Charge/Zero-Rated = "00000000".\n'
            'Export = "00000001", Deemed Supply = "01000000".'
        ),
    )
    tca_credit_note_reason = fields.Selection(
        selection=CREDIT_NOTE_REASONS,
        string='Credit Note Reason (BTAE-03)',
        copy=False,
        help=(
            'BTAE-03: Mandatory reason code for all UAE credit notes (IBR-158-AE Fatal rule).\n'
            'Must be set before generating PINT AE XML for any credit note.\n'
            '"VD" (Volume Discount) is the only reason that does NOT require a '
            'preceding invoice reference (IBG-03).'
        ),
    )
    tca_principal_id = fields.Char(
        string='Principal TRN (BTAE-14)',
        copy=False,
        help=(
            'BTAE-14: Tax Registration Number of the Principal in a Disclosed Agent Billing '
            'arrangement (UC5 / UC13).\n'
            'Mandatory when BTAE-02 position 6 = 1 (Disclosed Agent flag set).'
        ),
    )
    tca_is_out_of_scope = fields.Boolean(
        string='Out of Scope Supply (UAE)',
        default=False,
        copy=False,
        help=(
            'UC14: When True, sets invoice type code to 480 (Out of Scope invoice) '
            'instead of 380, or 81 instead of 381 for credit notes.\n'
            'Use for supplies not subject to UAE VAT (e.g. financial services, bare land).\n'
            'When Out of Scope: IBT-031-1 tax scheme = "IVAT" and only VAT categories '
            'E or O are permitted on invoice lines.'
        ),
    )

    # ──────────────────────────────────────────────────────────────────────────
    # COMPUTED HELPERS
    # ──────────────────────────────────────────────────────────────────────────

    def _tca_is_send_eligible(self):
        """
        Returns True if this invoice can be submitted (or resubmitted) to TCA.
        An invoice is eligible when:
          - the company has TCA integration active
          - the partner is configured with a PINT AE format (ubl_pint_ae)
          - the invoice is in 'posted' state
          - the move is outbound (not a vendor bill received via TCA)
        """
        self.ensure_one()
        return (
            self.company_id.tca_is_active
            and self.state == 'posted'
            and not self.tca_is_inbound
            and self.partner_id.commercial_partner_id.ubl_cii_format == 'ubl_pint_ae'
            and self.tca_move_state in ('not_sent', 'error', 'rejected')
        )

    # ──────────────────────────────────────────────────────────────────────────
    # CANCEL BLOCK
    # ──────────────────────────────────────────────────────────────────────────

    def button_cancel(self):
        """
        EXTENDS account.move.
        Block cancellation if any invoice is processing, delivered, or received.
        The Peppol document is legally binding once submitted to the network.
        To retract a sent invoice, a credit note must be issued.
        """
        blocked = self.filtered(lambda m: m.tca_move_state in _CANCEL_BLOCKED_STATES)
        if blocked:
            names = ', '.join(blocked[:5].mapped('name'))
            raise UserError(_(
                'Cannot cancel invoice(s) %s: they have already been submitted to the '
                'TCA Peppol network and cannot be retracted.\n\n'
                'To correct an error, issue a credit note instead.',
                names
            ))
        return super().button_cancel()

    def button_draft(self):
        """
        EXTENDS account.move.
        Block reset-to-draft for the same reason as cancel.
        """
        blocked = self.filtered(lambda m: m.tca_move_state in _CANCEL_BLOCKED_STATES)
        if blocked:
            names = ', '.join(blocked[:5].mapped('name'))
            raise UserError(_(
                'Cannot reset invoice(s) %s to draft: they have already been submitted to the '
                'TCA Peppol network.\n\n'
                'Issue a credit note to correct any errors.',
                names
            ))
        return super().button_draft()

    # ──────────────────────────────────────────────────────────────────────────
    # INBOUND XML ROUTING — register PINT AE in the builder dispatch table
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _get_ubl_cii_builder_from_xml_tree(self, tree):
        """
        EXTENDS account_edi_ubl_cii.
        Check for PINT AE CustomizationID BEFORE the generic BIS3 prefix check,
        because PINT AE's urn:peppol:pint:billing-1@ae-1 does NOT start with
        urn:cen.eu:en16931:2017 and would otherwise fall through unmatched.
        """
        customization_id = tree.find('{*}CustomizationID')
        if customization_id is not None:
            if customization_id.text == PINT_AE_CUSTOMIZATION_ID:
                return self.env['account.edi.xml.ubl_pint_ae']
        return super()._get_ubl_cii_builder_from_xml_tree(tree)

    # ──────────────────────────────────────────────────────────────────────────
    # STATUS UPDATE (called from webhook and cron)
    # ──────────────────────────────────────────────────────────────────────────

    def _tca_update_state_from_payload(self, payload):
        """
        Update tca_move_state from a TCA status poll response (GET /api/v1/invoices/{id}/).
        Payload keys (all integer codes per API spec):
          status         — 1=Processing, 2=Completed, 3=Rejected, 4=Failed
          c3_mls_status  — 0=N/A, 4=Accepted, 5=Rejected, 6=Unable to Deliver
          c5_mls_status  — 0=N/A, 4=Accepted (buyer confirmed receipt)
          uuid           — the TCA invoice UUID
        """
        self.ensure_one()
        tca_status = payload.get('status')    # int or None
        c3_status = payload.get('c3_mls_status')
        c5_status = payload.get('c5_mls_status')

        old_state = self.tca_move_state

        # Determine new state from most → least granular signal
        # c5 Accepted = buyer has confirmed receipt (terminal success)
        if c5_status == TCA_C5_ACCEPTED:
            new_state = 'received'
        # status=Completed + c3 Accepted = delivered to buyer AP
        elif tca_status == TCA_STATUS_COMPLETED and c3_status == TCA_C3_ACCEPTED:
            new_state = 'delivered'
        elif tca_status == TCA_STATUS_PROCESSING:
            new_state = 'processing'
        elif tca_status == TCA_STATUS_REJECTED:
            new_state = 'rejected'
        elif tca_status == TCA_STATUS_FAILED:
            new_state = 'error'
        else:
            _logger.warning(
                'TCA: unrecognised status payload for invoice %s: %s', self.name, payload
            )
            return

        if new_state == old_state:
            return  # No change

        self.tca_move_state = new_state
        if new_state in ('error', 'rejected'):
            error_detail = payload.get('error_message') or payload.get('detail') or tca_status
            self.tca_submission_error = error_detail
            self._message_log(
                body=_('TCA Peppol: Invoice %s — status changed to %s. Detail: %s',
                       self.name, new_state.upper(), error_detail)
            )
        else:
            self.tca_submission_error = False
            self._message_log(
                body=_('TCA Peppol: Invoice %s — status updated from %s → %s.',
                       self.name, old_state.upper(), new_state.upper())
            )

        _logger.info(
            'TCA: invoice %s (id=%s) state %s → %s',
            self.name, self.id, old_state, new_state
        )

    # ──────────────────────────────────────────────────────────────────────────
    # CRON: fallback status polling
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _cron_tca_sync_outbound_status(self):
        """
        Fallback polling cron — runs every 15 minutes.
        Syncs status for outbound invoices in 'submitted' or 'processing' state.

        Strategy (G-1 optimisation):
          1. Call list_processing_outbound() once per company to get the set of
             TCA invoice IDs that TCA still reports as status=1 (Processing).
          2. Any Odoo invoice NOT in that set must have changed state on TCA's side
             (completed, rejected, failed) — poll those individually.
          3. Invoices still in TCA's processing list are left unchanged (no extra call).

        This reduces API calls from N (one per pending invoice) to 1 + M where M
        is the number of invoices that have transitioned out of the processing state.
        Falls back to per-invoice polling if list_processing_outbound() fails.
        """
        active_companies = self.env['res.company'].search([('tca_is_active', '=', True)])
        api_svc = self.env['tca.api.service']

        for company in active_companies:
            pending_invoices = self.env['account.move'].search([
                ('company_id', '=', company.id),
                ('tca_move_state', 'in', ['submitted', 'processing']),
                ('tca_invoice_uuid', '!=', False),
            ], limit=100)

            if not pending_invoices:
                continue

            # ── Step 1: Fetch IDs TCA still considers in-flight ───────────────
            still_processing_ids = set()
            try:
                result = api_svc.list_processing_outbound(company, limit=200)
                tca_list = result.get('results', result) if isinstance(result, dict) else result
                still_processing_ids = {str(item.get('id', '')) for item in tca_list if item.get('id')}
            except Exception as exc:
                _logger.warning(
                    'TCA cron: list_processing_outbound failed for company %s (%s), '
                    'falling back to per-invoice poll',
                    company.id, exc
                )
                # Fallback: poll all pending invoices individually
                for invoice in pending_invoices:
                    self._tca_poll_single_invoice(api_svc, company, invoice)
                continue

            # ── Step 2: Poll only invoices that have moved out of processing ──
            for invoice in pending_invoices:
                uuid = str(invoice.tca_invoice_uuid or '')
                if uuid in still_processing_ids:
                    # TCA still processing — no state change, skip API call
                    continue
                # Invoice has changed state on TCA side — poll for final status
                self._tca_poll_single_invoice(api_svc, company, invoice)

    @api.model
    def _tca_poll_single_invoice(self, api_svc, company, invoice):
        """Poll TCA for a single invoice's current status and update state."""
        try:
            payload = api_svc.get_invoice_status(company, invoice.tca_invoice_uuid)
            invoice._tca_update_state_from_payload(payload)
        except Exception as exc:
            _logger.error(
                'TCA cron: failed to poll status for invoice %s (uuid=%s): %s',
                invoice.name, invoice.tca_invoice_uuid, exc
            )

    @api.model
    def _cron_tca_pull_inbound_invoices(self):
        """
        Fallback polling cron — pulls inbound invoices from TCA (direction=2).
        Normally inbound invoices arrive via webhook (DOCUMENT_RECEIVED).
        This cron is a safety net for missed webhooks.

        Improvements over the naive implementation:
          G-2: env.cr.commit() between each import — one parse failure does not
               roll back other successfully imported invoices.
          G-3: On UBL parse failure, create a bare draft vendor bill stub with the
               raw XML attached so accounting staff can manually process it.
          G-4: Follow DRF pagination (next URL) to import ALL inbound invoices,
               not just the first page of 50.
          G-6: Cursor-based tracking via tca_last_inbound_sync ir.config_parameter
               — only fetches invoices created after the last successful run
               (using created_after query param if supported, otherwise UUID
               deduplication for the full list).

        Deduplication: tca_invoice_uuid — already-imported invoices are skipped.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        active_companies = self.env['res.company'].search([('tca_is_active', '=', True)])
        api_svc = self.env['tca.api.service']

        for company in active_companies:
            cursor_key = f'tca.{company.id}.last_inbound_sync'
            last_sync = ICP.get_param(cursor_key, '')

            try:
                self._tca_pull_inbound_for_company(
                    api_svc, company, ICP, cursor_key, last_sync
                )
            except Exception as exc:
                _logger.error(
                    'TCA cron: failed to pull inbound invoices for company %s: %s',
                    company.id, exc
                )

    @api.model
    def _tca_pull_inbound_for_company(self, api_svc, company, ICP, cursor_key, last_sync):
        """
        Pull and import all new inbound invoices for one company.
        Paginates through all result pages (G-4).
        Updates the cursor param after each successful import batch (G-6).
        """
        # Fetch page 1 — pass created_after cursor if TCA supports it
        result = api_svc.list_inbound_invoices(company, limit=50)

        latest_created_at = last_sync  # track newest timestamp seen this run

        page_invoices = result.get('results', result) if isinstance(result, dict) else result
        next_url = result.get('next') if isinstance(result, dict) else None

        while True:
            for tca_invoice in page_invoices:
                tca_id = tca_invoice.get('id')
                xml_location_path = tca_invoice.get('invoice_xml_location_path')
                created_at = str(tca_invoice.get('created_at') or '')

                if not tca_id:
                    continue

                # G-6: skip if older than cursor (already imported in a prior run)
                if last_sync and created_at and created_at <= last_sync:
                    continue

                # Deduplication — belt-and-suspenders after cursor check
                existing = self.env['account.move'].search([
                    ('tca_invoice_uuid', '=', tca_id),
                    ('company_id', '=', company.id),
                ], limit=1)
                if existing:
                    continue

                if not xml_location_path:
                    _logger.warning(
                        'TCA cron: inbound invoice id=%s has no invoice_xml_location_path, skipping',
                        tca_id
                    )
                    continue

                # G-2: commit between imports — isolate failures
                move = self._tca_import_inbound_invoice(
                    company, tca_id, xml_location_path, api_svc
                )
                self.env.cr.commit()  # noqa: B012 — intentional mid-cron commit

                if move:
                    if created_at > latest_created_at:
                        latest_created_at = created_at

            # G-4: follow pagination
            if not next_url:
                break
            try:
                result = api_svc._http_get_url(company, next_url)
                page_invoices = result.get('results', [])
                next_url = result.get('next')
            except Exception as exc:
                _logger.error('TCA cron: pagination fetch failed (%s) — stopping', exc)
                break

        # G-6: advance cursor to latest invoice seen this run
        if latest_created_at and latest_created_at > last_sync:
            ICP.set_param(cursor_key, latest_created_at)
            _logger.info(
                'TCA cron: updated last_inbound_sync for company %s to %s',
                company.id, latest_created_at
            )

    def _tca_import_inbound_invoice(self, company, tca_id, xml_location_path, api_svc=None):
        """
        Import a single inbound TCA invoice as a vendor bill in Odoo.
        Called by the webhook controller (after fetching invoice details) and the fallback cron.

        tca_id            — TCA invoice ID (stored as tca_invoice_uuid)
        xml_location_path — S3 URI from invoice_xml_location_path field in TCA response
        api_svc           — optional pre-resolved tca.api.service reference

        Returns the created account.move record or None on failure.
        """
        if api_svc is None:
            api_svc = self.env['tca.api.service']

        try:
            xml_bytes = api_svc.download_inbound_xml(company, xml_location_path)
        except Exception as exc:
            _logger.error(
                'TCA: failed to download XML for id %s (path=%s): %s',
                tca_id, xml_location_path, exc
            )
            return None

        # Find a purchase journal for this company
        journal = self.env['account.journal'].search([
            ('type', '=', 'purchase'),
            ('company_id', '=', company.id),
        ], limit=1)
        if not journal:
            _logger.error('TCA: no purchase journal found for company %s', company.id)
            return None

        # Create attachment
        filename = f'tca_inbound_{tca_id}.xml'
        attachment = self.env['ir.attachment'].create({
            'name': filename,
            'datas': xml_bytes,
            'res_model': 'account.journal',
            'res_id': journal.id,
            'type': 'binary',
            'mimetype': 'application/xml',
        })

        # Use Odoo's standard UBL import pipeline.
        # _create_document_from_attachment routes via _get_ubl_cii_builder_from_xml_tree
        # which correctly routes PINT AE CustomizationID to our builder.
        try:
            move = journal._create_document_from_attachment(attachment.id)
        except Exception as exc:
            _logger.error('TCA: UBL import failed for id %s: %s', tca_id, exc)
            # G-3: create a bare draft vendor bill stub so the document is not lost.
            # Accounting staff can manually complete the record using the attached XML.
            move = self.env['account.move'].create({
                'move_type': 'in_invoice',
                'journal_id': journal.id,
                'company_id': company.id,
                'tca_invoice_uuid': tca_id,
                'tca_move_state': 'received',
                'tca_is_inbound': True,
                'tca_inbound_status': 'pending',
                'ref': f'TCA-{tca_id}',
            })
            attachment.write({'res_model': 'account.move', 'res_id': move.id})
            move._message_log(
                body=_(
                    'TCA Peppol: UBL parse failed for inbound invoice (ID: %s). '
                    'The raw XML is attached. Please fill in the details manually.',
                    tca_id
                )
            )
            _logger.warning(
                'TCA: created stub vendor bill for id %s after UBL parse failure', tca_id
            )
            return move

        if move:
            move.sudo().write({
                'tca_invoice_uuid': tca_id,
                'tca_move_state': 'received',
                'tca_is_inbound': True,
                'tca_inbound_status': 'pending',
            })
            move._message_log(
                body=_('Invoice imported from TCA Peppol network (ID: %s).', tca_id)
            )

        return move

    # ──────────────────────────────────────────────────────────────────────────
    # MANUAL RESEND
    # ──────────────────────────────────────────────────────────────────────────

    def action_tca_resend(self):
        """
        Opens the Send & Print wizard pre-configured for TCA resend.
        Available on invoices in 'error' or 'rejected' state.
        Resets the state to 'not_sent' so the wizard's compute allows re-submission.
        """
        self.ensure_one()
        if self.tca_move_state not in ('error', 'rejected'):
            raise UserError(_(
                'Invoice %s cannot be resent — current TCA state is "%s".',
                self.name, self.tca_move_state
            ))
        # Reset state so the wizard compute detects it as eligible
        self.tca_move_state = 'not_sent'
        self.tca_submission_error = False

        return {
            'name': _('Send & Print'),
            'type': 'ir.actions.act_window',
            'res_model': 'account.move.send',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'active_ids': [self.id],
                'active_model': 'account.move',
                'default_move_ids': [self.id],
            },
        }

    # ──────────────────────────────────────────────────────────────────────────
    # INBOUND: ACCEPT / REJECT
    # ──────────────────────────────────────────────────────────────────────────

    def action_tca_accept_inbound(self):
        """
        Accept an inbound invoice received via TCA Peppol.
        Posts the vendor bill (commits to ledger) and marks it accepted.
        Only available on draft inbound invoices with tca_is_inbound=True.
        """
        self.ensure_one()
        if not self.tca_is_inbound:
            raise UserError(_('This action is only available for invoices received via TCA Peppol.'))
        if self.state != 'draft':
            raise UserError(_('Only draft invoices can be accepted. Current state: %s', self.state))

        self.tca_inbound_status = 'accepted'
        self.action_post()
        self._message_log(body=_('Inbound invoice accepted and posted to ledger.'))

        # TODO: When TCA adds an Invoice Response endpoint, send AP (Accepted)
        # response back to the seller via:
        #   api_svc.send_invoice_response(company, tca_id, response_code='AP')

        return True

    def action_tca_reject_inbound(self):
        """
        Reject an inbound invoice received via TCA Peppol.
        Opens a simple wizard to capture the rejection reason,
        then cancels the vendor bill.
        """
        self.ensure_one()
        if not self.tca_is_inbound:
            raise UserError(_('This action is only available for invoices received via TCA Peppol.'))
        if self.state not in ('draft', 'posted'):
            raise UserError(_('Cannot reject an invoice in state: %s', self.state))

        return {
            'name': _('Reject Inbound Invoice'),
            'type': 'ir.actions.act_window',
            'res_model': 'tca.inbound.reject.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_move_id': self.id,
            },
        }

    def _tca_apply_rejection(self, reason):
        """
        Apply rejection to an inbound invoice. Called by the reject wizard.
        If the bill was already posted, resets to draft first, then cancels.
        """
        self.ensure_one()
        self.tca_inbound_status = 'rejected'
        self.tca_reject_reason = reason

        if self.state == 'posted':
            self.button_draft()
        if self.state == 'draft':
            self.button_cancel()

        self._message_log(body=_(
            'Inbound invoice rejected.\nReason: %s', reason
        ))

        # TODO: When TCA adds an Invoice Response endpoint, send RE (Rejected)
        # response back to the seller via:
        #   api_svc.send_invoice_response(company, tca_id, response_code='RE',
        #                                  reason=reason)

        _logger.info('TCA: inbound invoice %s rejected. Reason: %s', self.name, reason)
