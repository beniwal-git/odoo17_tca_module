# -*- coding: utf-8 -*-
# Part of TCA. See LICENSE file for full copyright and licensing details.

from odoo import fields, models, api, _
from odoo.exceptions import UserError


class ResCompany(models.Model):
    """
    Extends res.company with TCA OAuth2 credentials and configuration.
    Each Odoo company maps to exactly one TCA organization.
    Credentials are stored per-company via ir.config_parameter.
    """
    _inherit = 'res.company'

    # ── TCA connection settings ────────────────────────────────────────────────

    tca_base_url = fields.Char(
        string='TCA API Base URL',
        default='https://api.tcapeppol.com',
        help='Base URL for the TCA Peppol API. Change only for sandbox/staging environments.',
    )
    tca_client_id = fields.Char(
        string='TCA Client ID',
        help='OAuth2 client_id from TCA portal: Settings → API Clients → Create Client.',
    )
    tca_client_secret = fields.Char(
        string='TCA Client Secret',
        help='OAuth2 client_secret. Visible only once at creation time in the TCA portal.',
    )
    tca_webhook_secret = fields.Char(
        string='TCA Webhook Secret',
        help=(
            'HMAC-SHA256 secret for validating inbound webhook payloads from TCA. '
            'Register the webhook in TCA portal: Settings → Webhooks → Create Endpoint.'
        ),
    )

    # ── Read-only info fetched from TCA ───────────────────────────────────────

    tca_org_name = fields.Char(
        string='TCA Organisation',
        readonly=True,
        help='Organisation name as registered in TCA. Populated after a successful connection test.',
    )
    tca_is_active = fields.Boolean(
        string='TCA Integration Active',
        default=False,
        help='When enabled, invoices with a PINT AE partner can be sent via TCA Peppol.',
    )
    invoice_is_tca = fields.Boolean(
        string='Submit via TCA Peppol by default',
        default=False,
        help=(
            'When enabled, the "Submit via TCA Peppol" checkbox will be pre-selected '
            'by default when sending invoices to PINT AE (UAE) partners.'
        ),
    )

    # ── Computed helpers ──────────────────────────────────────────────────────

    def _get_tca_config_key(self, key):
        """Return a company-scoped ir.config_parameter key."""
        self.ensure_one()
        return f'tca.{self.id}.{key}'

    def _get_tca_param(self, key, default=None):
        """Read a company-scoped token param."""
        self.ensure_one()
        return self.env['ir.config_parameter'].sudo().get_param(
            self._get_tca_config_key(key), default
        )

    def _set_tca_param(self, key, value):
        """Write a company-scoped token param."""
        self.ensure_one()
        self.env['ir.config_parameter'].sudo().set_param(
            self._get_tca_config_key(key), value or ''
        )

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_tca_test_connection(self):
        """
        Test the TCA connection using the stored credentials.
        Fetches a token and retrieves the organisation name.
        """
        self.ensure_one()
        if not self.tca_client_id or not self.tca_client_secret:
            raise UserError(_(
                'Please enter both the TCA Client ID and Client Secret before testing the connection.'
            ))
        api_service = self.env['tca.api.service']
        try:
            token = api_service._fetch_new_token(self)
            if not token:
                raise UserError(_('Failed to obtain access token from TCA. Check your credentials.'))
            # Fetch org info to confirm the token works
            org_info = api_service.get_org_info(self)
            self.tca_org_name = org_info.get('name', '')
            self.tca_is_active = True
        except Exception as exc:
            self.tca_is_active = False
            self.tca_org_name = ''
            raise UserError(_('TCA connection test failed: %s', str(exc))) from exc

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('TCA Connection Successful'),
                'message': _('Connected to TCA organisation: %s', self.tca_org_name or 'Unknown'),
                'type': 'success',
                'sticky': False,
            },
        }

    def action_tca_disconnect(self):
        """Clear all stored TCA tokens and mark the integration inactive."""
        self.ensure_one()
        for key in ('access_token', 'access_token_expires_at', 'refresh_token'):
            self._set_tca_param(key, '')
        self.tca_is_active = False
        self.tca_org_name = ''
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('TCA Disconnected'),
                'message': _('TCA integration has been disconnected for this company.'),
                'type': 'warning',
                'sticky': False,
            },
        }
