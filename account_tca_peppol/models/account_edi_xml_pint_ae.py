# -*- coding: utf-8 -*-
# Part of TCA. See LICENSE file for full copyright and licensing details.
"""
PINT AE (UAE Peppol CIUS) UBL 2.1 XML builder.

Specification: urn:peppol:pint:billing-1@ae-1
Profile ID:    urn:peppol:bis:billing
Based on:      PINT (Peppol International Invoice model) aligned to UAE e-invoicing mandate
               Cabinet Decision No. 106 of 2025

UAE-specific additions over PEPPOL BIS3:
  BTAE-01  Buyer internal identification number  (BuyerCustomerParty/PartyIdentification/ID)
  BTAE-02  Invoice transaction type code          (ProfileExecutionID — 8-digit binary flags)
  BTAE-03  Credit note reason code                (DiscrepancyResponse/ResponseCode — mandatory on CN)
  BTAE-04  Currency exchange rate                  (TaxExchangeRate/CalculationRate, max 6dp)
  BTAE-05  Contract value                          (ContractDocumentReference/DocumentDescription)
  BTAE-06  Supply period description code          (InvoicePeriod/DescriptionCode)
  BTAE-07  Invoice UUID                            (cbc:UUID — UUID4 per document)
  BTAE-08  Per-line VAT amount                     (InvoiceLine/ItemPriceExtension/TaxTotal/TaxAmount)
  BTAE-09  Type of goods/services (RC)             (Item/AdditionalItemProperty — mandatory when AE)
  BTAE-10  Per-line amount payable                 (InvoiceLine/ItemPriceExtension/Amount)
  BTAE-11  Buyer trade license authority name      (CompanyID/@schemeAgencyName when BTAE-16=TL)
  BTAE-12  Seller trade license authority name     (CompanyID/@schemeAgencyName when BTAE-15=TL)
  BTAE-13  Commodity type code  G/S                (CommodityClassification/CommodityCode)
  BTAE-14  Principal TRN (Disclosed Agent)         (field stored; XML binding TBD)
  BTAE-15  Seller legal registration ID type       (CompanyID/@schemeAgencyID supplier)
  BTAE-16  Buyer legal registration ID type        (CompanyID/@schemeAgencyID buyer)
  BTAE-18  Seller passport issuing country         (CompanyID/@schemeAgencyName when BTAE-15=PAS)
  BTAE-19  Buyer passport issuing country          (CompanyID/@schemeAgencyName when BTAE-16=PAS)
  BTAE-20  Tax total in AED                        (second TaxTotal with currencyID=AED)
  IBT-003  Out of scope type codes                 (480 invoice / 81 credit note via tca_is_out_of_scope)
  IBT-200  Tax included indicator                  (TaxTotal/TaxIncludedIndicator = false)
"""

import logging
import uuid as _uuid_mod

from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


# ── PINT AE identifiers ────────────────────────────────────────────────────────
PINT_AE_CUSTOMIZATION_ID = 'urn:peppol:pint:billing-1@ae-1'
PINT_AE_PROFILE_ID = 'urn:peppol:bis:billing'
UAE_EAS = '0235'

# UAE VAT categories used by the mandate
UAE_VAT_CATEGORIES = {
    'S': 5.0,    # Standard Rate 5%
    'Z': 0.0,    # Zero Rated
    'E': 0.0,    # Exempt
    'O': None,   # Out of scope / Not subject to VAT
    'AE': 5.0,   # Reverse Charge (VAT accounted by buyer)
    'G': 0.0,    # Free export (Zero-rated, export)
    'K': 0.0,    # Intra-community supply (not used in UAE but included for completeness)
}


class AccountEdiXmlUBLPintAe(models.AbstractModel):
    """
    PINT AE XML builder — inherits the full UBL BIS3 pipeline and
    overrides/extends only the UAE-specific elements.
    """
    _name = 'account.edi.xml.ubl_pint_ae'
    _inherit = 'account.edi.xml.ubl_bis3'
    _description = 'UAE PINT AE (Peppol International Invoice — UAE Annex)'

    # ──────────────────────────────────────────────────────────────────────────
    # FILENAME & SCHEMATRON
    # ──────────────────────────────────────────────────────────────────────────

    def _export_invoice_filename(self, invoice):
        return f"{invoice.name.replace('/', '_')}_pint_ae.xml"

    def _export_invoice_ecosio_schematrons(self):
        return {}  # TCA runs its own schematron; no ecosio integration needed

    # ──────────────────────────────────────────────────────────────────────────
    # CUSTOMIZATION / PROFILE IDs
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _get_customization_ids(self):
        ids = super()._get_customization_ids()
        ids['ubl_pint_ae'] = PINT_AE_CUSTOMIZATION_ID
        return ids

    # ──────────────────────────────────────────────────────────────────────────
    # BTAE-02: ProfileExecutionID
    # ──────────────────────────────────────────────────────────────────────────

    def _get_profile_execution_id(self, invoice):
        """
        BTAE-02: UAE ProfileExecutionID — 8-digit binary flag string.

        Positions: [FTZ][DeemedSupply][MarginScheme][SummaryInv][ContinuousSupply]
                   [DisclosedAgent][Ecommerce][Exports]

        All standard use cases (UC1 Standard, UC2 Reverse Charge, UC3 Zero-Rated):
          '00000000'
        UC4 Deemed Supply: '01000000'
        UC6 Summary Invoice: '00010000'
        UC7 Continuous Supply: '00001000'
        UC8 Free Trade Zone: '10000000'
        UC9 E-commerce: '00000010'
        UC10 Exports: '00000001'
        UC11 Margin Scheme: '00100000'
        UC5/UC13 Disclosed Agent: '00000100'

        Reads from tca_transaction_type_flags field. Defaults to '00000000'.
        """
        flags = (invoice.tca_transaction_type_flags or '00000000').strip()
        if len(flags) != 8 or not all(c in '01' for c in flags):
            _logger.warning(
                'PINT AE: invalid BTAE-02 flags "%s" on invoice %s — using 00000000',
                flags, invoice.name
            )
            flags = '00000000'

        # F2-9 / F1-3: auto-detect export — if buyer country is not AE, the
        # Exports bit (position 8, index 7) must be 1 per PINT AE spec.
        # Overrides any user-set value for that bit (it's factual, not a choice).
        buyer = invoice.partner_id.commercial_partner_id
        if buyer.country_id and buyer.country_id.code != 'AE':
            flags = flags[:7] + '1'

        return flags

    # ──────────────────────────────────────────────────────────────────────────
    # F2-2: VAT CATEGORY / EXEMPTION — UAE-specific overrides
    # ──────────────────────────────────────────────────────────────────────────

    def _get_tax_unece_codes(self, invoice, tax):
        """
        EXTENDS account.edi.common.
        If the tax has UAE-specific fields set (tca_tax_category, tca_exemption_reason_code,
        tca_exemption_reason), use them instead of Odoo's EU-centric defaults.

        IBT-118: TaxCategory/ID          ← tca_tax_category
        IBT-121: TaxExemptionReasonCode  ← tca_exemption_reason_code
        IBT-120: TaxExemptionReason      ← tca_exemption_reason
        """
        result = super()._get_tax_unece_codes(invoice, tax)

        if not tax:
            return result

        # Override category code if UAE category is explicitly set
        if tax.tca_tax_category:
            result['tax_category_code'] = tax.tca_tax_category

        # Override exemption codes if UAE-specific values are provided
        if tax.tca_exemption_reason_code:
            result['tax_exemption_reason_code'] = tax.tca_exemption_reason_code
        if tax.tca_exemption_reason:
            result['tax_exemption_reason'] = tax.tca_exemption_reason

        return result

    # ──────────────────────────────────────────────────────────────────────────
    # PARTY VALS — UAE-specific overrides
    # ──────────────────────────────────────────────────────────────────────────

    def _get_partner_party_legal_entity_vals_list(self, partner):
        """
        EXTENDS account.edi.xml.ubl_bis3.
        Adds UAE-specific schemeAgencyID (BTAE-15/16) and schemeAgencyName:
          - BTAE-12/11: Issuing authority name when type = TL
          - BTAE-18/19: Passport issuing country code when type = PAS
        """
        vals_list = super()._get_partner_party_legal_entity_vals_list(partner)

        # Use tca_trade_license if set; otherwise fall back to vat / company_registry
        trade_license = (
            partner.tca_trade_license
            or partner.company_registry
            or partner.vat
            or ''
        )
        legal_id_type = partner.tca_legal_id_type       # TL / EID / PAS / CD
        legal_authority = partner.tca_legal_authority   # Authority name when TL
        passport_country = partner.tca_passport_country_id  # Country when PAS

        for vals in vals_list:
            if trade_license:
                vals['company_id'] = trade_license
                if legal_id_type:
                    attrs = {'schemeAgencyID': legal_id_type}
                    if legal_id_type == 'TL' and legal_authority:
                        # BTAE-12/11: trade license issuing authority
                        attrs['schemeAgencyName'] = legal_authority
                    elif legal_id_type == 'PAS' and passport_country:
                        # BTAE-18/19: passport issuing country code
                        attrs['schemeAgencyName'] = passport_country.code
                    vals['company_id_attrs'] = attrs

        return vals_list

    def _get_partner_party_tax_scheme_vals_list(self, partner, role):
        """
        EXTENDS account.edi.xml.ubl_bis3.
        For UAE:
          - Normal invoices: CompanyID in PartyTaxScheme = full 15-digit TRN (IBT-031/048)
          - Out of scope (UC14): tax scheme = 'IVAT' instead of 'VAT'
        """
        vals_list = super()._get_partner_party_tax_scheme_vals_list(partner, role)

        if partner.peppol_eas == UAE_EAS:
            # TRN from peppol_endpoint is the full 15-digit value
            trn = partner.peppol_endpoint or partner.vat or ''
            for vals in vals_list:
                tax_scheme_id = vals.get('tax_scheme_vals', {}).get('id')
                if tax_scheme_id in ('VAT', 'IVAT'):
                    vals['company_id'] = trn
        return vals_list

    def _get_partner_party_vals(self, partner, role):
        """
        EXTENDS account.edi.xml.ubl_bis3.
        UAE EndpointID uses EAS 0235 + TRN (handled by BIS3 via peppol_endpoint/peppol_eas).
        """
        vals = super()._get_partner_party_vals(partner, role)
        return vals

    def _get_partner_address_vals(self, partner):
        """
        EXTENDS account.edi.xml.ubl_bis3.
        UAE mandate (ibr-128-ae): when country = AE, CountrySubentity must be
        one of AUH / DXB / SHJ / UAQ / FUJ / AJM / RAK.
        """
        vals = super()._get_partner_address_vals(partner)
        if partner.country_id and partner.country_id.code == 'AE':
            emirate = partner.tca_emirate or ''
            if not emirate and partner.state_id:
                emirate = partner.state_id.code or ''
            vals['country_subentity'] = emirate
        return vals

    # ──────────────────────────────────────────────────────────────────────────
    # TAX TOTAL VALS — IBT-200 + BTAE-20 (AED total for foreign currency)
    # ──────────────────────────────────────────────────────────────────────────

    def _get_invoice_tax_totals_vals_list(self, invoice, taxes_vals):
        """
        EXTENDS account.edi.xml.ubl_bis3.
        1. IBT-200: Adds TaxIncludedIndicator = false (mandatory in PINT AE).
        2. BTAE-20: When invoice currency != AED, adds a second minimal TaxTotal
           in AED (accounting currency) containing only cbc:TaxAmount.
        """
        vals_list = super()._get_invoice_tax_totals_vals_list(invoice, taxes_vals)

        company_currency = invoice.company_id.currency_id

        for vals in vals_list:
            # IBT-200: UAE always VAT-exclusive pricing for B2B
            vals['tax_included_indicator'] = 'false'

        # BTAE-20: second TaxTotal in AED when invoice is in foreign currency
        if invoice.currency_id and invoice.currency_id != company_currency:
            # tax_amount in taxes_vals is already in company currency (AED)
            aed_tax_amount = round(taxes_vals.get('tax_amount', 0.0), 2)
            vals_list.append({
                'currency': company_currency,
                'currency_dp': 2,
                'tax_amount': aed_tax_amount,
                'btae_20_aed_total': True,   # marker — template renders this without TaxSubtotal
            })

        return vals_list

    # ──────────────────────────────────────────────────────────────────────────
    # INVOICE LINE VALS — BTAE-08, BTAE-09, BTAE-10, BTAE-13, IBT-158
    # ──────────────────────────────────────────────────────────────────────────

    def _get_invoice_line_vals(self, line, line_id, taxes_vals):
        """
        EXTENDS account.edi.xml.ubl_bis3.
        Adds UAE-specific per-line elements:
          - BTAE-10: ItemPriceExtension/Amount (net line amount payable)
          - BTAE-08: ItemPriceExtension/TaxTotal/TaxAmount (per-line VAT)
          - BTAE-09: Item/AdditionalItemProperty — type of goods/services (RC mandatory)
          - BTAE-13: CommodityClassification/CommodityCode (G/S)
          - IBT-158: CommodityClassification/ItemClassificationCode (HS/CPV code)
        """
        vals = super()._get_invoice_line_vals(line, line_id, taxes_vals)

        currency = line.currency_id or line.company_currency_id
        dp = 2

        # ── BTAE-10: net line amount ──────────────────────────────────────────
        line_net_amount = vals.get('line_extension_amount', 0.0)

        # ── BTAE-08: per-line VAT amount ──────────────────────────────────────
        line_vat_amount = sum(
            detail.get('tax_amount_currency', 0.0)
            for detail in taxes_vals.get('tax_details', {}).values()
        )

        vals['item_price_extension_vals'] = {
            'amount': round(line_net_amount, dp),
            'currency': currency,
            'currency_dp': dp,
            'tax_total_vals': {
                'tax_amount': round(line_vat_amount, dp),
                'currency': currency,
                'currency_dp': dp,
            },
        }

        # ── Commodity classification (BTAE-13 + IBT-158) ─────────────────────
        commodity_code = line.tca_commodity_type or line._get_default_commodity_type()
        hs_code = line.tca_hs_code or ''

        item_vals = vals.get('item_vals', {})
        pint_classifications = [{'commodity_code': commodity_code}]
        if hs_code:
            pint_classifications.append({
                'item_classification_code': hs_code,
                'item_classification_code_attrs': {
                    'listID': 'HS',
                    'listVersionID': '1.0',
                },
            })
        item_vals['pint_ae_commodity_classifications'] = pint_classifications

        # ── BTAE-09: type of goods/services (mandatory when VAT category = AE) ─
        rc_description = line.tca_rc_description or ''
        if rc_description:
            # Rendered as AdditionalItemProperty in the line template
            item_vals['btae_09_rc_description'] = rc_description

        vals['item_vals'] = item_vals
        return vals

    # ──────────────────────────────────────────────────────────────────────────
    # MAIN EXPORT — root-level PINT AE fields
    # ──────────────────────────────────────────────────────────────────────────

    def _export_invoice_vals(self, invoice):
        """
        EXTENDS account.edi.xml.ubl_bis3.
        Overrides customization_id/profile_id and injects all UAE-specific
        root-level fields:
          - CustomizationID → PINT AE value
          - ProfileID        → urn:peppol:bis:billing
          - ProfileExecutionID (BTAE-02) — 8-digit flags from tca_transaction_type_flags
          - UUID (BTAE-07)
          - IssueTime (IBT-168)
          - TaxExchangeRate/CalculationRate (BTAE-04) when currency != AED
          - BuyerCustomerParty/PartyIdentification (BTAE-01)
          - DiscrepancyResponse/ResponseCode (BTAE-03) on credit notes
          - Invoice type code 480/81 when tca_is_out_of_scope = True
          - Tax amount in AED marker for BTAE-20 (handled in TaxTotal vals)
          - IVAT tax scheme for out-of-scope invoices
        """
        vals = super()._export_invoice_vals(invoice)

        # ── Override CustomizationID & ProfileID ─────────────────────────────
        vals['vals']['customization_id'] = PINT_AE_CUSTOMIZATION_ID
        vals['vals']['profile_id'] = PINT_AE_PROFILE_ID

        # ── BTAE-02: ProfileExecutionID ───────────────────────────────────────
        vals['vals']['profile_execution_id'] = self._get_profile_execution_id(invoice)

        # ── BTAE-07: UUID ──────────────────────────────────────────────────────
        vals['vals']['uuid'] = str(_uuid_mod.uuid4())

        # ── IssueTime (IBT-168) ───────────────────────────────────────────────
        if invoice.invoice_date:
            vals['vals']['issue_time'] = fields.Datetime.now().strftime('%H:%M:%S')

        # ── BTAE-04: Currency exchange rate ────────────────────────────────────
        # Required when invoice currency differs from AED (company currency).
        company_currency = invoice.company_id.currency_id
        if invoice.currency_id and invoice.currency_id != company_currency:
            rate = self.env['res.currency']._get_conversion_rate(
                invoice.currency_id,
                company_currency,
                invoice.company_id,
                invoice.invoice_date or fields.Date.today(),
            )
            # PINT AE rule ibr-002-ae: max 6 decimal places
            vals['vals']['tax_exchange_rate'] = round(rate, 6)
            vals['vals']['tax_exchange_rate_currency_code'] = invoice.currency_id.name
            vals['vals']['tax_exchange_rate_base_currency_code'] = company_currency.name

        # ── BTAE-01: Buyer internal identification (BuyerCustomerParty) ───────
        buyer = invoice.partner_id.commercial_partner_id
        if buyer.ref:
            vals['vals']['buyer_customer_party_id'] = buyer.ref

        # ── BTAE-03: Credit note reason code (DiscrepancyResponse) ────────────
        if invoice.move_type in ('out_refund', 'in_refund'):
            vals['vals']['btae_03_reason'] = invoice.tca_credit_note_reason or ''

        # ── IBT-003: Out of scope type codes (480 invoice / 81 credit note) ───
        if invoice.tca_is_out_of_scope:
            if invoice.move_type in ('out_invoice', 'in_invoice'):
                # Override invoice type code: 380 → 480
                vals['vals']['document_type_code'] = 480
            elif invoice.move_type in ('out_refund', 'in_refund'):
                # Override credit note type code: 381 → 81
                vals['vals']['document_type_code'] = 81

        # ── Override template references to use PINT AE templates ─────────────
        vals['InvoiceType_template'] = 'account_tca_peppol.pint_ae_InvoiceType'
        vals['CreditNoteType_template'] = 'account_tca_peppol.pint_ae_CreditNoteType'
        vals['InvoiceLineType_template'] = 'account_tca_peppol.pint_ae_InvoiceLineType'
        vals['CreditNoteLineType_template'] = 'account_tca_peppol.pint_ae_CreditNoteLineType'
        vals['TaxTotalType_template'] = 'account_tca_peppol.pint_ae_TaxTotalType'

        return vals

    # ──────────────────────────────────────────────────────────────────────────
    # CONSTRAINTS — UAE-specific validation before export
    # ──────────────────────────────────────────────────────────────────────────

    def _export_invoice_constraints(self, invoice, vals):
        """
        EXTENDS account.edi.xml.ubl_bis3.
        Adds UAE-specific pre-export validation.
        """
        constraints = super()._export_invoice_constraints(invoice, vals)

        supplier = invoice.company_id.partner_id.commercial_partner_id
        customer = invoice.partner_id.commercial_partner_id

        # ── ibr-128-ae: AE address must have valid emirate code ──────────────
        for party, label in [(supplier, 'Supplier'), (customer, 'Customer (Buyer)')]:
            if party.country_id and party.country_id.code == 'AE':
                emirate = party.tca_emirate or (party.state_id and party.state_id.code) or ''
                valid_emirates = ['AUH', 'DXB', 'SHJ', 'UAQ', 'FUJ', 'AJM', 'RAK']
                if emirate not in valid_emirates:
                    constraints[f'pint_ae_emirate_{label.lower()}'] = _(
                        '[ibr-128-ae] %s address: when country is AE, emirate must be one of '
                        'AUH, DXB, SHJ, UAQ, FUJ, AJM, RAK. '
                        'Please set the emirate on the partner record.',
                        label
                    )

        # ── ibr-150-ae: Supplier legal registration ID required when EAS=0235 ─
        if supplier.peppol_eas == UAE_EAS and not (
            supplier.tca_trade_license or supplier.company_registry or supplier.vat
        ):
            constraints['pint_ae_supplier_legal_id'] = _(
                '[ibr-150-ae] The supplier legal registration identifier (IBT-030) '
                'must be provided when the EAS scheme is 0235 (UAE TIN). '
                'Set the Trade License, Emirates ID, or VAT on the company partner.'
            )

        # ── ibr-173-ae: Supplier legal ID type must be set when EAS=0235 ──────
        if (
            supplier.peppol_eas == UAE_EAS
            and supplier.country_id
            and supplier.country_id.code == 'AE'
            and (supplier.tca_trade_license or supplier.company_registry)
            and not supplier.tca_legal_id_type
        ):
            constraints['pint_ae_supplier_legal_id_type'] = _(
                '[ibr-173-ae] Supplier legal registration identifier type (BTAE-15) '
                'must be set when the legal ID is provided and EAS is 0235. '
                'Set "Legal ID Type (UAE)" on the company partner to TL, EID, PAS, or CD.'
            )

        # ── ibr-172-ae: Authority name required when legal ID type = TL ───────
        if supplier.tca_legal_id_type == 'TL' and not supplier.tca_legal_authority:
            constraints['pint_ae_supplier_authority'] = _(
                '[ibr-172-ae] Issuing authority name (BTAE-12) must be provided '
                'when the Seller legal registration type is "Trade License" (TL). '
                'Set "Issuing Authority (UAE)" on the company partner.'
            )

        # ── BTAE-18: Passport country required when seller legal ID type = PAS ─
        if supplier.tca_legal_id_type == 'PAS' and not supplier.tca_passport_country_id:
            constraints['pint_ae_supplier_passport_country'] = _(
                '[BTAE-18] Passport issuing country (BTAE-18) must be provided '
                'when the Seller legal registration type is "Passport" (PAS). '
                'Set "Passport Issuing Country" on the company partner.'
            )

        # ── BTAE-19: Passport country required when buyer legal ID type = PAS ──
        if customer.tca_legal_id_type == 'PAS' and not customer.tca_passport_country_id:
            constraints['pint_ae_customer_passport_country'] = _(
                '[BTAE-19] Passport issuing country (BTAE-19) must be provided '
                'when the Buyer legal registration type is "Passport" (PAS). '
                'Set "Passport Issuing Country" on the partner record.'
            )

        # ── IBR-158-AE: Credit note reason code mandatory ─────────────────────
        if invoice.move_type in ('out_refund', 'in_refund') and not invoice.tca_credit_note_reason:
            constraints['pint_ae_btae_03'] = _(
                '[IBR-158-AE] Credit Note Reason (BTAE-03) is mandatory for all UAE credit notes. '
                'Set the "Credit Note Reason" field on this credit note before generating XML.'
            )

        # ── IBR-055-AE: Preceding invoice reference mandatory (except VD) ─────
        if (
            invoice.move_type in ('out_refund', 'in_refund')
            and invoice.tca_credit_note_reason
            and invoice.tca_credit_note_reason != 'VD'
            and not invoice.reversed_entry_id
        ):
            constraints['pint_ae_btae_ibg03'] = _(
                '[IBR-055-AE] A preceding invoice reference (IBG-03) is required '
                'when the credit note reason is not "VD" (Volume Discount). '
                'Ensure this credit note is linked to the original invoice via "Reversal Of".'
            )

        # ── BTAE-02: validate flag format ──────────────────────────────────────
        flags = (invoice.tca_transaction_type_flags or '00000000').strip()
        if len(flags) != 8 or not all(c in '01' for c in flags):
            constraints['pint_ae_btae_02'] = _(
                '[BTAE-02] Transaction Type Flags must be exactly 8 characters, '
                'each 0 or 1 (e.g. "00000000" for standard, "00000001" for export). '
                'Current value: "%s".',
                flags
            )

        # ── BTAE-14: Principal TRN required when Disclosed Agent flag set ──────
        disclosed_agent_flag = flags[5:6] == '1' if len(flags) == 8 else False
        if disclosed_agent_flag and not invoice.tca_principal_id:
            constraints['pint_ae_btae_14'] = _(
                '[BTAE-14] Principal TRN is mandatory when the Disclosed Agent '
                'flag (position 6) is set in the Transaction Type Flags. '
                'Set "Principal TRN (BTAE-14)" on the invoice.'
            )

        # ── BTAE-09: RC description required for reverse charge lines ─────────
        for line in invoice.invoice_line_ids.filtered(lambda l: l.display_type == 'product'):
            has_rc_tax = any(
                t.l10n_ae_tax_category == 'AE'
                for t in line.tax_ids
                if hasattr(t, 'l10n_ae_tax_category')
            )
            # Also check VAT category via tax group name as fallback
            if not has_rc_tax:
                has_rc_tax = any('reverse' in (t.name or '').lower() for t in line.tax_ids)
            if has_rc_tax and not line.tca_rc_description:
                constraints[f'pint_ae_btae_09_{line.id}'] = _(
                    '[BTAE-09] Invoice line "%s" uses Reverse Charge VAT (AE category). '
                    'Set "Goods/Services Type (BTAE-09)" on this line.',
                    line.name or str(line.id)
                )
                break  # Report once, not for every RC line

        # ── ibr-126-ae: Price BaseQuantity and GrossPrice required ────────────
        for line in invoice.invoice_line_ids.filtered(lambda l: l.display_type == 'product'):
            if not line.quantity or line.quantity == 0:
                constraints[f'pint_ae_price_qty_{line.id}'] = _(
                    '[ibr-126-ae] Invoice line "%s": Quantity (IBT-149) cannot be zero.',
                    line.name or str(line.id)
                )
                break

        return constraints

    # ──────────────────────────────────────────────────────────────────────────
    # IMPORT — route inbound PINT AE XML to this builder
    # ──────────────────────────────────────────────────────────────────────────

    def _import_invoice_ubl_cii(self, invoice, file_data):
        """
        Delegates to the standard UBL 2.1 import pipeline.
        PINT AE is a profile of UBL 2.1, so the base import handles most fields.
        UAE-specific fields (UUID, ProfileExecutionID, etc.) are metadata only.
        """
        return super()._import_invoice_ubl_cii(invoice, file_data)

    # ──────────────────────────────────────────────────────────────────────────
    # HELPERS
    # ──────────────────────────────────────────────────────────────────────────

    def _is_reverse_charge_tax(self, tax):
        """Return True if the tax is a UAE reverse-charge (AE category) tax."""
        if hasattr(tax, 'l10n_ae_tax_category'):
            return tax.l10n_ae_tax_category == 'AE'
        return 'reverse' in (tax.name or '').lower()
