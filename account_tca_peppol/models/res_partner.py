# -*- coding: utf-8 -*-
# Part of TCA. See LICENSE file for full copyright and licensing details.

from odoo import fields, models, api, _

# UAE-specific legal entity identifier type codes (schemeAgencyID values)
UAE_LEGAL_ID_TYPES = [
    ('TL', 'Trade License (Commercial)'),
    ('EID', 'Emirates ID'),
    ('PAS', 'Passport'),
    ('CD', 'Cabinet Decision'),
]

# UAE Emirates codes for CountrySubentity validation (ibr-128-ae)
UAE_EMIRATES = ['AUH', 'DXB', 'SHJ', 'UAQ', 'FUJ', 'AJM', 'RAK']


class ResPartner(models.Model):
    """
    Extends res.partner to:
    1. Add 'ubl_pint_ae' to the ubl_cii_format selection field
    2. Register AE → ubl_pint_ae in the country format mapping
    3. Add UAE-specific Peppol fields (legal entity type, trade license authority)
    """
    _inherit = 'res.partner'

    # ── UAE-specific Peppol fields ────────────────────────────────────────────

    tca_legal_id_type = fields.Selection(
        selection=UAE_LEGAL_ID_TYPES,
        string='Legal ID Type (UAE)',
        help=(
            'Type of legal registration identifier (BTAE-15 for supplier, BTAE-16 for buyer). '
            'Required when EAS is 0235 and legal registration ID is provided. '
            'TL=Trade License, EID=Emirates ID, PAS=Passport, CD=Cabinet Decision.'
        ),
    )
    tca_legal_authority = fields.Char(
        string='Issuing Authority (UAE)',
        help=(
            'Name of the authority that issued the legal registration document (BTAE-12/11). '
            'Mandatory when Legal ID Type is "Trade License" (BTAE-15/16 = TL). '
            'Example: "Department of Economic Development - Abu Dhabi".'
        ),
    )
    tca_trade_license = fields.Char(
        string='Trade License / Registration ID',
        help=(
            'Legal registration identifier (IBT-030 supplier / IBT-047 buyer). '
            'For UAE companies this is the Trade License number, Emirates ID number, '
            'Passport number, or Cabinet Decision reference, depending on Legal ID Type.'
        ),
    )
    tca_emirate = fields.Selection(
        selection=[(e, e) for e in UAE_EMIRATES],
        string='Emirate',
        help=(
            'UAE emirate code for postal address (ibr-128-ae). '
            'Used in CountrySubentity when country is AE. '
            'AUH=Abu Dhabi, DXB=Dubai, SHJ=Sharjah, UAQ=Umm Al Quwain, '
            'FUJ=Fujairah, AJM=Ajman, RAK=Ras Al Khaimah.'
        ),
    )
    tca_passport_country_id = fields.Many2one(
        'res.country',
        string='Passport Issuing Country (BTAE-18/19)',
        help=(
            'BTAE-18 (Seller) / BTAE-19 (Buyer): ISO 3166-1 alpha-2 country code of the '
            'authority that issued the passport.\n'
            'Required when Legal ID Type is "Passport" (PAS).'
        ),
    )

    # ── ubl_cii_format: add ubl_pint_ae to the selection ─────────────────────
    # We extend the selection defined in account_edi_ubl_cii.
    # Odoo 17 allows adding selection items via _inherit + selection_add.

    ubl_cii_format = fields.Selection(
        selection_add=[('ubl_pint_ae', 'PINT AE (UAE Peppol)')],
        ondelete={'ubl_pint_ae': 'set null'},
    )

    # ── Country → format mapping ──────────────────────────────────────────────

    @api.model
    def _get_ubl_cii_formats(self):
        """
        EXTENDS account_edi_ubl_cii.
        Add UAE → PINT AE to the country-format mapping so that partners
        with country AE auto-select the PINT AE format.
        """
        fmt = super()._get_ubl_cii_formats()
        fmt['AE'] = 'ubl_pint_ae'
        return fmt

    # ── EDI builder dispatch ──────────────────────────────────────────────────

    def _get_edi_builder(self):
        """
        EXTENDS account_edi_ubl_cii.
        Route ubl_pint_ae format to the PINT AE builder model.
        """
        if self.ubl_cii_format == 'ubl_pint_ae':
            return self.env['account.edi.xml.ubl_pint_ae']
        return super()._get_edi_builder()

    # ── Peppol endpoint validation override ──────────────────────────────────

    def _build_error_peppol_endpoint(self, eas, endpoint):
        """
        EXTENDS account_edi_ubl_cii.
        For UAE EAS 0235, the endpoint is a TRN (Tax Registration Number):
          - 15 digits for registered businesses (e.g. 123456789000003)
          - '1XXXXXXXXX' placeholder for unknown/anonymous buyers (10 chars)
        """
        if eas == '0235':
            if endpoint == '1XXXXXXXXX':
                return None  # Anonymous buyer — always valid
            if not endpoint or not endpoint.isdigit() or len(endpoint) != 15:
                return _(
                    'The UAE Peppol endpoint (TRN) must be exactly 15 digits '
                    '(e.g. 123456789000003), or the placeholder "1XXXXXXXXX" for anonymous buyers.'
                )
            return None
        return super()._build_error_peppol_endpoint(eas, endpoint)
