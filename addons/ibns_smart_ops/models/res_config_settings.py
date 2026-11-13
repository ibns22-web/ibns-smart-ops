# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    sfa_api_key = fields.Char(
        string="SFA Integration API Key",
        config_parameter="ibns_smart_ops.sfa_api_key",
        help="System integration token for headless SFA mobile applications. Stored in system parameters (ir.config_parameter)."
    )
    sfa_api_user_id = fields.Many2one(
        'res.users',
        string="SFA Default API User",
        config_parameter="ibns_smart_ops.sfa_api_user_id",
        help="System user context used when authenticating via the integration API key."
    )
