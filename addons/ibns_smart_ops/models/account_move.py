# -*- coding: utf-8 -*-
from odoo import models, fields


class AccountMove(models.Model):
    _inherit = 'account.move'

    sfa_idempotency_key = fields.Char(
        string='SFA Idempotency Key',
        index=True,
        copy=False,
        readonly=True,
        help="Unique idempotency token supplied by external SFA clients to prevent duplicate billing on network retry."
    )

    _sql_constraints = [
        ('sfa_idempotency_key_uniq', 'unique(sfa_idempotency_key)', 'The SFA idempotency key must be unique across all invoices!'),
    ]
