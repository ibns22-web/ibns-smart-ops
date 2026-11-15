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
        """Sinkronisasi nama driver saat driver_id dipilih, diubah, atau dikosongkan di form UI."""
        if self.driver_id:
            self.driver_name = self.driver_id.name
        else:
            self.driver_name = False

    @api.model_create_multi
    def create(self, vals_list):
        """Override create untuk sinkronisasi driver_name jika driver_id disediakan tanpa driver_name."""
        for vals in vals_list:
            if 'driver_id' in vals and 'driver_name' not in vals:
                if vals['driver_id']:
                    driver = self.env['res.users'].browse(vals['driver_id'])
                    vals['driver_name'] = driver.name if driver.exists() else False
                else:
                    vals['driver_name'] = False
        return super().create(vals_list)

    def write(self, vals):
        """Override write untuk mempertahankan konsistensi antara driver_id dan driver_name saat update maupun unassign."""
        if 'driver_id' in vals and 'driver_name' not in vals:
            if vals['driver_id']:
                driver = self.env['res.users'].browse(vals['driver_id'])
                vals['driver_name'] = driver.name if driver.exists() else False
            else:
                vals['driver_name'] = False
        return super().write(vals)

    @api.model
    def _register_hook(self):
        """Menjamin domain_force pada rule_stock_picking_sfa_user dan rule_stock_move_sfa_user
        secara ketat mengunci kunci asing relational driver_id = user.id dan kebal dari spoofing nama profil,
        serta mereset bendera noupdate pada ir.rule agar pembaruan modul (-u) selalu menerapkan aturan terbaru."""
        super()._register_hook()
        rules_data = self.env['ir.model.data'].search([
            ('module', '=', 'ibns_smart_ops'),
            ('model', '=', 'ir.rule'),
            ('noupdate', '=', True)
        ])
        if rules_data:
            rules_data.sudo().write({'noupdate': False})

        rule_picking = self.env.ref('ibns_smart_ops.rule_stock_picking_sfa_user', raise_if_not_found=False)
        if rule_picking and rule_picking.domain_force != "[('driver_id', '=', user.id)]":
            rule_picking.sudo().write({'domain_force': "[('driver_id', '=', user.id)]"})
        rule_move = self.env.ref('ibns_smart_ops.rule_stock_move_sfa_user', raise_if_not_found=False)
        if rule_move and rule_move.domain_force != "[('picking_id.driver_id', '=', user.id)]":
            rule_move.sudo().write({'domain_force': "[('picking_id.driver_id', '=', user.id)]"})

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
        'move_ids.sale_line_id',
        'move_ids.sale_line_id.qty_delivered',
        'move_ids.sale_line_id.qty_invoiced',
        'sale_id',
        'sale_id.invoice_ids.state',
        'sale_id.invoice_ids.move_type',
        'sale_id.order_line.qty_delivered',
        'sale_id.order_line.qty_invoiced',
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

            # Evaluasi khusus baris SO yang terkait dengan move pada picking ini
            sale_lines = picking.move_ids.mapped('sale_line_id').filtered(lambda l: not l.display_type)
            if not sale_lines:
                sale_lines = picking.sale_id.order_line.filtered(lambda l: not l.display_type)

            total_delivered = sum(sale_lines.mapped('qty_delivered'))
            total_invoiced = sum(sale_lines.mapped('qty_invoiced'))

            if not is_delivered:
                # Hanya tandai invoiced_pending_delivery jika total tagihan melebihi total barang yang sudah terkirim
                if has_posted_invoices and total_invoiced > total_delivered:
                    picking.delivered_vs_invoiced_status = 'invoiced_pending_delivery'
                else:
                    picking.delivered_vs_invoiced_status = 'not_delivered'
            else:
                if has_posted_invoices and total_delivered > 0 and total_invoiced >= total_delivered:
                    picking.delivered_vs_invoiced_status = 'fully_reconciled'
                else:
                    picking.delivered_vs_invoiced_status = 'delivered_pending_invoice'
