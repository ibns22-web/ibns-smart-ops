# -*- coding: utf-8 -*-
try:
    from markupsafe import Markup
except ImportError:
    from odoo.tools import Markup

from odoo import models, fields, api
from odoo.exceptions import ValidationError


class ResPartner(models.Model):
    _inherit = 'res.partner'

    partner_latitude = fields.Float(string='Latitude', digits=(10, 7))
    partner_longitude = fields.Float(string='Longitude', digits=(10, 7))
    outlet_zone = fields.Selection([
        ('north', 'North Zone'),
        ('south', 'South Zone'),
        ('east', 'East Zone'),
        ('west', 'West Zone'),
    ], string='Distribution Zone')

    geofence_radius_meters = fields.Float(
        string='Geofence Radius (M)',
        default=200.0,
        help="Batas radius toleransi (dalam meter) saat sales/driver melakukan check-in kunjungan lapangan."
    )

    map_embed_html = fields.Html(
        string='Live Map Preview (OSM)',
        compute='_compute_map_embed',
        sanitize=False
    )

    @api.constrains('geofence_radius_meters', 'partner_latitude', 'partner_longitude')
    def _check_geofence_and_coordinates(self):
        for rec in self:
            if rec.geofence_radius_meters is not False and rec.geofence_radius_meters is not None:
                if rec.geofence_radius_meters <= 0:
                    raise ValidationError("Radius geofence harus bernilai positif lebih besar dari 0 meter.")
            if rec.partner_latitude is not False and rec.partner_latitude is not None:
                if not (-90.0 <= rec.partner_latitude <= 90.0):
                    raise ValidationError("Koordinat Latitude harus berada dalam rentang -90.0 hingga 90.0 derajat.")
            if rec.partner_longitude is not False and rec.partner_longitude is not None:
                if not (-180.0 <= rec.partner_longitude <= 180.0):
                    raise ValidationError("Koordinat Longitude harus berada dalam rentang -180.0 hingga 180.0 derajat.")

    @api.depends('partner_latitude', 'partner_longitude')
    def _compute_map_embed(self):
        for rec in self:
            if rec.partner_latitude and rec.partner_longitude:
                # Konversi float eksplisit untuk menutup potensi celah XSS injeksi URL
                lat = float(rec.partner_latitude)
                lon = float(rec.partner_longitude)
                bbox = f"{lon - 0.005:.7f},{lat - 0.005:.7f},{lon + 0.005:.7f},{lat + 0.005:.7f}"
                rec.map_embed_html = Markup(f'''
                    <div style="margin-top: 10px; border-radius: 8px; overflow: hidden; border: 1px solid #ccc;">
                        <iframe width="100%" height="280" frameborder="0" scrolling="no" marginheight="0" marginwidth="0" 
                                src="https://www.openstreetmap.org/export/embed.html?bbox={bbox}&amp;layer=mapnik&amp;marker={lat:.7f},{lon:.7f}">
                        </iframe>
                        <div style="padding: 6px 12px; background: #f8f9fa; font-size: 12px;">
                            <a href="https://www.google.com/maps?q={lat:.7f},{lon:.7f}" target="_blank" style="color: #007bff; text-decoration: none; font-weight: bold;">
                                📍 Buka di Google Maps Langsung &#8599;
                            </a>
                        </div>
                    </div>
                ''')
            else:
                rec.map_embed_html = Markup('<p style="color: #888; font-style: italic; margin-top: 10px;">Isi koordinat Latitude & Longitude untuk melihat peta outlet.</p>')
