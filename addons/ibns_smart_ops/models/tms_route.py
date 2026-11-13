# -*- coding: utf-8 -*-
from odoo import models, fields, api


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    tms_route_sequence = fields.Integer(string='Route Sequence', default=1)
    delivery_status = fields.Selection([
        ('unassigned', 'Unassigned'),
        ('scheduled', 'Scheduled'),
        ('in_transit', 'In Transit'),
        ('delivered', 'Delivered'),
        ('failed', 'Failed'),
    ], string='TMS Status', default='unassigned', tracking=True)

    # Relasi Driver berbasis ID (Many2one) dengan backward compatibility terhadap driver_name
    driver_id = fields.Many2one('res.users', string='Driver', index=True, tracking=True)
    driver_name = fields.Char(string='Driver Name', tracking=True)

    destination_latitude = fields.Float(related='partner_id.partner_latitude', string='Dest Latitude', store=True)
    destination_longitude = fields.Float(related='partner_id.partner_longitude', string='Dest Longitude', store=True)

    # -------------------------------------------------------------------------
    # AUDIT: DELIVERED VS INVOICED STATUS
    # -------------------------------------------------------------------------
    delivered_vs_invoiced_status = fields.Selection([
        ('not_delivered', 'Belum Terkirim'),
        ('delivered_pending_invoice', 'Terkirim - Menunggu Faktur'),
        ('invoiced_pending_delivery', 'Faktur Terbit - Belum Terkirim'),
        ('fully_reconciled', 'Terkirim & Tertagih'),
        ('no_sale_order', 'Pengiriman Tanpa Penjualan'),
    ], string='Audit Delivered vs Invoiced', compute='_compute_delivered_vs_invoiced_status', store=True, tracking=True)

    delivered_qty_total = fields.Float(
        string='Total Qty Terkirim',
        compute='_compute_delivery_audit_quantities',
        store=True,
        help="Akumulasi kuantitas produk yang telah tervalidasi terkirim pada pengiriman ini."
    )
    invoiced_qty_total = fields.Float(
        string='Total Qty Tertagih',
        compute='_compute_delivery_audit_quantities',
        store=True,
        help="Akumulasi kuantitas produk yang telah diterbitkan fakturnya pada Sales Order terkait."
    )

    @api.onchange('driver_id')
    def _onchange_driver_id(self):
        """Sinkronisasi nama driver saat driver_id dipilih di form UI."""
        if self.driver_id and not self.driver_name:
            self.driver_name = self.driver_id.name

    @api.model_create_multi
    def create(self, vals_list):
        """Override create untuk sinkronisasi driver_name jika driver_id disediakan tanpa driver_name."""
        for vals in vals_list:
            if vals.get('driver_id') and not vals.get('driver_name'):
                driver = self.env['res.users'].browse(vals['driver_id'])
                if driver.exists():
                    vals['driver_name'] = driver.name
        return super().create(vals_list)

    def write(self, vals):
        """Override write untuk mempertahankan konsistensi antara driver_id dan driver_name."""
        if vals.get('driver_id') and 'driver_name' not in vals:
            driver = self.env['res.users'].browse(vals['driver_id'])
            if driver.exists():
                vals['driver_name'] = driver.name
        return super().write(vals)

    @api.depends(
        'state',
        'delivery_status',
        'picking_type_id.code',
        'move_ids.quantity',
        'move_ids.state',
        'sale_id.order_line.qty_delivered',
        'sale_id.order_line.qty_invoiced'
    )
    def _compute_delivery_audit_quantities(self):
        for picking in self:
            del_qty = 0.0
            inv_qty = 0.0
            picking_type = picking.picking_type_code or (picking.picking_type_id and picking.picking_type_id.code)
            if picking_type == 'outgoing':
                if picking.state == 'done':
                    del_qty = sum(m.quantity for m in picking.move_ids if m.state == 'done')
                elif picking.delivery_status == 'delivered':
                    del_qty = sum(m.quantity or m.product_uom_qty for m in picking.move_ids)
                elif picking.sale_id:
                    lines = picking.sale_id.order_line.filtered(lambda l: not l.display_type)
                    del_qty = sum(lines.mapped('qty_delivered'))

                if picking.sale_id:
                    lines = picking.sale_id.order_line.filtered(lambda l: not l.display_type)
                    inv_qty = sum(lines.mapped('qty_invoiced'))

            picking.delivered_qty_total = del_qty
            picking.invoiced_qty_total = inv_qty

    @api.depends(
        'state',
        'delivery_status',
        'picking_type_id.code',
        'sale_id',
        'sale_id.invoice_status',
        'sale_id.invoice_ids.state',
        'sale_id.invoice_ids.move_type',
        'sale_id.order_line.qty_delivered',
        'sale_id.order_line.qty_invoiced'
    )
    def _compute_delivered_vs_invoiced_status(self):
        for picking in self:
            picking_type = picking.picking_type_code or (picking.picking_type_id and picking.picking_type_id.code)
            if picking_type != 'outgoing':
                picking.delivered_vs_invoiced_status = False
                continue

            if not picking.sale_id:
                picking.delivered_vs_invoiced_status = 'no_sale_order'
                continue

            is_delivered = (picking.state == 'done' or picking.delivery_status == 'delivered')
            posted_invoices = picking.sale_id.invoice_ids.filtered(
                lambda m: m.state == 'posted' and m.move_type == 'out_invoice'
            )
            has_posted_invoices = bool(posted_invoices)

            lines = picking.sale_id.order_line.filtered(lambda l: not l.display_type)
            total_delivered = sum(lines.mapped('qty_delivered'))
            total_invoiced = sum(lines.mapped('qty_invoiced'))

            if not is_delivered and not has_posted_invoices:
                picking.delivered_vs_invoiced_status = 'not_delivered'
            elif not is_delivered and has_posted_invoices:
                picking.delivered_vs_invoiced_status = 'invoiced_pending_delivery'
            elif is_delivered and not has_posted_invoices:
                picking.delivered_vs_invoiced_status = 'delivered_pending_invoice'
            elif is_delivered and has_posted_invoices:
                if picking.sale_id.invoice_status == 'invoiced' or (total_invoiced >= total_delivered and total_delivered > 0):
                    picking.delivered_vs_invoiced_status = 'fully_reconciled'
                else:
                    picking.delivered_vs_invoiced_status = 'delivered_pending_invoice'
            else:
                picking.delivered_vs_invoiced_status = 'not_delivered'
