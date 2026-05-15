# -*- coding: utf-8 -*-
# Part of TCA. See LICENSE file for full copyright and licensing details.

import re

from odoo import fields, models, api, _
from odoo.exceptions import ValidationError

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
    tca_legal_form = fields.Char(
        string='Legal Form (IBT-033)',
        help=(
            'IBT-033: Additional legal information about the seller/buyer, '
            'e.g. "Merchant", "LLC", "Free Zone Company". '
            'Rendered as CompanyLegalForm in the PINT AE XML.'
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

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _tca_get_tin(self):
        """
        Return the 10-digit UAE TIN for this partner.

        Per FTA, the TIN is the first 10 digits of the TRN. Users keep
        storing the full 15-character TRN in `partner.vat`; this helper
        derives the TIN for PINT AE IBT-032 emission (PartyTaxScheme/
        CompanyID), which schematron ibr-148-ae requires to match
        ^1[0-9]{9}$. Falls back to peppol_endpoint when vat is empty.

        Returns '' when neither source is set.
        """
        self.ensure_one()
        raw = (self.vat or self.peppol_endpoint or '').strip()
        if not raw:
            return ''
        # Already a 10-digit TIN? Use as-is.
        if len(raw) == 10:
            return raw
        # 15-char TRN: first 10 chars. (TRN regex allows alphanumeric in
        # later positions but real UAE TRNs are all-digit; slice is safe.)
        return raw[:10]

    # ── Peppol endpoint validation override ──────────────────────────────────

    # ── Format constraints ────────────────────────────────────────────────────
    # UAE FTA: TIN is the first 10 digits of the 15-character TRN. Users keep
    # storing the full TRN in `partner.vat`; the XML builder derives the
    # 10-digit TIN for IBT-032 (PartyTaxScheme/CompanyID) at emission time.
    # Both forms are therefore accepted by the validator.
    #   - TRN: 15 chars, starts with 1 (legacy VAT registration number)
    #   - TIN: 10 digits, starts with 1 (first 10 chars of TRN); IBT-032
    # UAE Peppol Participant ID (EndpointID @schemeID=0235): 10 digits
    # starting with "1" — happens to share the TIN shape, conceptually
    # different identifier.
    _RE_UAE_TRN = re.compile(r'^1[a-zA-Z0-9]{14}$')
    _RE_UAE_TIN = re.compile(r'^1[0-9]{9}$')
    _RE_UAE_PARTICIPANT = re.compile(r'^1[0-9]{9}$')
    _RE_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
    # Phone: allow digits + common separators (space, dash, parens, plus, dot).
    # At least 7 digits total. Loose pattern — strict E.164 validation would
    # need an external module.
    _RE_PHONE = re.compile(r'^[\d\s\-\(\)\+\.]+$')
    _UAE_PLACEHOLDER_PARTICIPANT = '1XXXXXXXXX'

    def _build_error_peppol_endpoint(self, eas, endpoint):
        """
        EXTENDS account_edi_ubl_cii.
        For UAE EAS 0235, the Peppol endpoint must be:
          - exactly 10 digits starting with "1" (UAE Peppol Participant ID), or
          - '1XXXXXXXXX' placeholder for unknown/anonymous buyers
        The 15-digit TRN goes in the Tax ID / PartyTaxScheme, not here.
        """
        if eas == '0235':
            if not endpoint:
                return _('The UAE Peppol endpoint is required for EAS 0235.')
            if endpoint == self._UAE_PLACEHOLDER_PARTICIPANT:
                return None
            if not self._RE_UAE_PARTICIPANT.match(endpoint):
                return _(
                    'The UAE Peppol endpoint must be exactly 10 digits starting with "1" '
                    '(UAE Peppol Participant ID). The 15-digit TRN belongs in the "Tax ID" '
                    'field instead. Current: "%s".', endpoint,
                )
            return None
        return super()._build_error_peppol_endpoint(eas, endpoint)

    @api.depends('peppol_eas')
    def _compute_peppol_endpoint(self):
        """
        EXTENDS account_edi_ubl_cii.
        Base implementation auto-fills `peppol_endpoint` from another field
        (e.g. `vat` for UAE EAS=0235) when the compute trigger fires. For UAE
        we treat the Peppol endpoint as a user-set identifier (Participant ID,
        10 digits) — distinct from the TRN. Auto-filling from vat would
        produce 15-digit values that fail our `_RE_UAE_PARTICIPANT` regex.

        Strategy: only delegate to the parent for records that currently have
        NO endpoint set. Records the user has already filled keep their value
        untouched (no self-assignment, no spurious dirty-marker that would
        trigger downstream recomputes on every multi-record write).
        """
        # Records the user has already set: preserve untouched.
        # Records still empty: let the parent fill if it can (caller may have
        # set peppol_eas to a non-UAE EAS where auto-fill is appropriate).
        to_fill = self.filtered(lambda p: not p.peppol_endpoint)
        if to_fill:
            super(ResPartner, to_fill)._compute_peppol_endpoint()

    # ──────────────────────────────────────────────────────────────────────────
    # CONSOLIDATED UAE PARTNER VALIDATION — runs on save (create/write).
    # Fires only for UAE business partners (country=AE, is_company=True).
    # Collects ALL field issues into a single ValidationError so the user
    # sees every fix needed in one dialog instead of one-error-at-a-time.
    # ──────────────────────────────────────────────────────────────────────────

    @api.constrains(
        'name', 'is_company', 'country_id',
        'street', 'city',
        'vat',
        'peppol_eas', 'peppol_endpoint',
        'email', 'phone', 'mobile',
        'tca_emirate', 'tca_legal_id_type', 'tca_trade_license',
        'tca_legal_authority', 'tca_passport_country_id',
    )
    def _check_tca_partner_complete(self):
        """
        Enforce UAE PINT AE mandatory fields + format rules on UAE business
        partners. Returns a single dialog listing every issue.

        Scope: UAE company partners that the user has started configuring
        for PINT AE — i.e. peppol_eas is '0235' OR at least one tca_* field
        is set. Auto-created partners (e.g. the partner Odoo creates inside
        `res.company.create`) carry none of these markers, so the check is
        skipped and company creation isn't blocked. Once the user opens
        the partner and fills any PINT AE field, the full set becomes
        mandatory on the next save.
        """
        for partner in self:
            if not partner.is_company:
                continue
            if not (partner.country_id and partner.country_id.code == 'AE'):
                continue
            # Skip until the user has explicitly started filling the partner
            # form. peppol_eas is intentionally excluded — Odoo's base module
            # auto-computes it from country=AE, so it fires on the auto
            # partner Odoo creates inside res.company.create and would block
            # company creation. We rely on real user-touched fields instead:
            # any address detail, any tca_* PINT field, or peppol_endpoint.
            opted_in = (
                partner.street
                or partner.peppol_endpoint
                or partner.tca_emirate
                or partner.tca_legal_id_type
                or partner.tca_trade_license
                or partner.tca_legal_authority
                or partner.tca_passport_country_id
                or partner.tca_legal_form
            )
            if not opted_in:
                continue

            errors = []

            # ── Mandatory fields ─────────────────────────────────────────────
            if not partner.street:
                errors.append(_('"Street" (IBT-035/050) is required.'))
            if not partner.city:
                errors.append(_('"City" (IBT-037/052) is required.'))
            if not partner.vat:
                errors.append(_('"Tax ID" / TRN (IBT-031/048) is required.'))
            if not partner.peppol_eas:
                errors.append(_(
                    '"Peppol EAS" is required. Set it to 0235 in the "E-Invoicing" tab.'
                ))
            if not partner.peppol_endpoint:
                errors.append(_(
                    '"Peppol Endpoint" (IBT-034/049) is required.'
                ))
            if not partner.tca_emirate:
                errors.append(_(
                    '"Emirate" (ibr-128-ae) is required. '
                    'Select AUH / DXB / SHJ / UAQ / FUJ / AJM / RAK.'
                ))
            if not partner.tca_legal_id_type:
                errors.append(_(
                    '"Legal ID Type" (BTAE-15/16) is required. '
                    'Set TL / EID / PAS / CD.'
                ))
            if not partner.tca_trade_license:
                errors.append(_(
                    '"Trade License / Registration ID" (IBT-030/047) is required.'
                ))

            # ── Conditional fields based on Legal ID Type ────────────────────
            if partner.tca_legal_id_type == 'TL' and not partner.tca_legal_authority:
                errors.append(_(
                    '"Issuing Authority" (BTAE-11/12) is required when '
                    'Legal ID Type is Trade License.'
                ))
            if partner.tca_legal_id_type == 'PAS' and not partner.tca_passport_country_id:
                errors.append(_(
                    '"Passport Issuing Country" (BTAE-18/19) is required when '
                    'Legal ID Type is Passport.'
                ))

            # ── Format checks ────────────────────────────────────────────────
            # Accept either:
            #   - 15-char TRN (UAE VAT registration), or
            #   - 10-digit TIN (= first 10 digits of the TRN, per FTA).
            # The XML builder derives the 10-digit TIN for IBT-032 emission.
            if partner.vat:
                v = partner.vat.strip()
                if not (self._RE_UAE_TRN.match(v) or self._RE_UAE_TIN.match(v)):
                    errors.append(_(
                        '"Tax ID" must be either the 15-character UAE TRN or the '
                        '10-digit TIN, both starting with "1". Current: "%s".', v
                    ))

            # Peppol endpoint format (uses existing helper which respects placeholder)
            if partner.peppol_eas and partner.peppol_endpoint:
                ep_err = self._build_error_peppol_endpoint(
                    partner.peppol_eas, partner.peppol_endpoint
                )
                if ep_err:
                    errors.append(ep_err)

            # Email format (Odoo doesn't enforce by default)
            if partner.email and not self._RE_EMAIL.match(partner.email.strip()):
                errors.append(_(
                    '"Email" must be a valid email address (e.g. name@example.com). '
                    'Current: "%s".', partner.email
                ))

            # Phone format — at least 7 digits, only digits + common separators
            for phone_field_name, phone_label in [('phone', 'Phone'), ('mobile', 'Mobile')]:
                phone_val = (partner[phone_field_name] or '').strip()
                if not phone_val:
                    continue
                if not self._RE_PHONE.match(phone_val):
                    errors.append(_(
                        '"%s" may only contain digits, spaces, dashes, parentheses, '
                        'dots and a leading +. Current: "%s".',
                        phone_label, phone_val
                    ))
                else:
                    digit_count = sum(c.isdigit() for c in phone_val)
                    if digit_count < 7:
                        errors.append(_(
                            '"%s" must contain at least 7 digits. Current: "%s".',
                            phone_label, phone_val
                        ))

            if errors:
                raise ValidationError(_(
                    'Please fix the following before saving "%(name)s":\n\n%(list)s',
                    name=partner.display_name or _('this contact'),
                    list='\n'.join(f'• {e}' for e in errors),
                ))
