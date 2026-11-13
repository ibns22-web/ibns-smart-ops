# -*- coding: utf-8 -*-
{
    'name': 'IBNS Smart Logistics & SFA Engine',
    'version': '18.0.1.0.0',
    'category': 'Operations/Logistics',
    'summary': 'TMS Dispatching, Coordinate Mapping, Geofencing, and SFA Mobile REST API',
    'description': """
        IBNS Smart Logistics & SFA Engine for Odoo 18 Community:
        - TMS: OpenStreetMap Embed, Route Sequencing, and Automated Delivered vs Invoiced Audit Tracking
        - SFA: Token-Authenticated REST API with In-Memory Rate Guard (60 req/min)
        - Geofencing: Haversine distance verification during field visit check-ins
        - Reliability: Idempotent invoice generation and sanitized null JSON serialization
        - Security: Role-Based Access Control (RBAC) and record-level isolation (ir.rule)
    """,
    'author': 'IBNS (Iban Supriatman)',
    'website': 'https://web.ibns.workers.dev',
    'depends': ['base', 'sale_management', 'stock', 'account', 'sale_stock'],
    'data': [
        'security/sfa_security.xml',
        'security/ir.model.access.csv',
        'views/partner_tms_views.xml',
        'views/delivery_route_views.xml',
        'views/account_move_views.xml',
        'views/res_config_settings_views.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
