# -*- coding: utf-8 -*-
# Part of TCA. See LICENSE file for full copyright and licensing details.

from odoo import fields, models


class AccountMoveLine(models.Model):
    """
    Extends account.move.line with UAE PINT AE-specific fields:
      - tca_commodity_type: G (Goods) or S (Services) — BTAE-13, mandatory
      - tca_hs_code: HS or CPV classification code — IBT-158, optional
    """
    _inherit = 'account.move.line'

    tca_commodity_type = fields.Selection(
        selection=[
            ('G', 'Goods'),
            ('S', 'Services'),
        ],
        string='Commodity Type (UAE)',
        help=(
            'BTAE-13: Whether this line item is a Good (G) or Service (S). '
            'Mandatory for PINT AE (UAE Peppol) invoices.'
        ),
        default=False,
    )
    tca_hs_code = fields.Char(
        string='HS / CPV Code',
        size=30,
        help=(
            'IBT-158: Harmonised System (HS) or CPV classification code for this item. '
            'Optional but recommended for goods imports/exports. '
            'Example HS code: 88098432324. Will be output with listID="HS".'
        ),
    )
    tca_rc_description = fields.Char(
        string='Goods/Services Type (BTAE-09)',
        help=(
            'BTAE-09: Description of the type of goods or services supplied. '
            'Mandatory when the VAT category on this line is AE (Reverse Charge — UC2). '
            'Describe the nature of the supply, e.g. "Professional IT consultancy services" '
            'or "Industrial machinery components".'
        ),
    )

    def _get_default_commodity_type(self):
        """
        Infer commodity type from the product if tca_commodity_type is not set.
        Falls back to 'S' (Services) — the safer default for B2B.
        """
        self.ensure_one()
        if self.product_id and self.product_id.type == 'consu':
            return 'G'
        return 'S'
