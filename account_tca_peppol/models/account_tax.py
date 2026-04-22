# -*- coding: utf-8 -*-
# Part of TCA. See LICENSE file for full copyright and licensing details.

from odoo import fields, models

# UAE VAT category codes per PINT AE / UNCL5305
UAE_TAX_CATEGORY_SELECTION = [
    ('S',  'S — Standard Rate (5%)'),
    ('Z',  'Z — Zero Rated'),
    ('E',  'E — Exempt'),
    ('AE', 'AE — Reverse Charge'),
    ('G',  'G — Free Export / Zero-Rated Export'),
    ('O',  'O — Not Subject to VAT (Out of Scope)'),
    ('K',  'K — Intra-Community Supply'),
]


class AccountTax(models.Model):
    """
    Extends account.tax with PINT AE UAE-specific VAT classification fields.

    These fields allow administrators to precisely classify each tax record
    for UAE e-invoicing purposes, overriding Odoo's EU-centric default logic
    in _get_tax_unece_codes().

    IBT-118: TaxCategory/ID          ← tca_tax_category
    IBT-121: TaxExemptionReasonCode   ← tca_exemption_reason_code
    IBT-120: TaxExemptionReason       ← tca_exemption_reason
    """
    _inherit = 'account.tax'

    tca_tax_category = fields.Selection(
        selection=UAE_TAX_CATEGORY_SELECTION,
        string='UAE VAT Category (IBT-118)',
        help=(
            'PINT AE: VAT category code for this tax per UNCL5305 / UAE mandate.\n'
            'When set, overrides Odoo\'s auto-detected category in PINT AE XML.\n'
            'S = Standard (5%), Z = Zero-Rated, E = Exempt, AE = Reverse Charge,\n'
            'G = Export/Free, O = Out of Scope, K = Intra-Community.'
        ),
    )

    tca_exemption_reason_code = fields.Char(
        string='UAE Exemption Reason Code (IBT-121)',
        size=64,
        help=(
            'PINT AE IBT-121: Code from the AE-Exempt code list explaining why '
            'this tax is exempt or zero-rated (e.g. "VATEX-AE-SPEC").\n'
            'Mandatory when tca_tax_category is Z, E, or G.\n'
            'Leave blank to use the Odoo default (EU codes — not valid for UAE).'
        ),
    )

    tca_exemption_reason = fields.Char(
        string='UAE Exemption Reason Text (IBT-120)',
        size=256,
        help=(
            'PINT AE IBT-120: Human-readable description of why this supply '
            'is exempt or zero-rated under UAE VAT law.\n'
            'Example: "Zero-rated export of goods outside UAE" or '
            '"Exempt under Article 42 of UAE VAT Decree-Law".'
        ),
    )
