# -*- coding: utf-8 -*-
import json
import time
import uuid
import psycopg2
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import HttpCase, tagged
from odoo.tools import mute_logger
from odoo.addons.ibns_smart_ops.controllers.sfa_api import _rate_limit_records


@tagged('post_install', '-at_install')
class TestSfaApi(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.valid_token = f"test_suite_dummy_key_do_not_use_in_prod_{uuid.uuid4().hex}"
        cls.driver_token = f"test_suite_driver_dummy_key_do_not_use_in_prod_{uuid.uuid4().hex}"
        cls.endpoint = "/api/v1/sfa/outlets"

        # 1. User Manager SFA (memiliki hak akses billing / faktur)
        cls.sfa_manager = cls.env['res.users'].create({
            'name': 'SFA Test Manager',
            'login': f'sfa_manager_{uuid.uuid4().hex[:8]}',
            'email': 'sfa_manager@ibns.local',
            'groups_id': [
                (6, 0, [
                    cls.env.ref('base.group_user').id,
                    cls.env.ref('ibns_smart_ops.group_sfa_manager').id,
                    cls.env.ref('account.group_account_invoice').id,
                    cls.env.ref('stock.group_stock_user').id,
                ])
            ]
        })

        # 2. User Driver / Lapangan (hanya memiliki hak operasional)
        cls.sfa_driver = cls.env['res.users'].create({
            'name': 'SFA Test Driver',
            'login': f'sfa_driver_{uuid.uuid4().hex[:8]}',
            'email': 'driver_a@ibns.local',
            'groups_id': [
                (6, 0, [
                    cls.env.ref('base.group_user').id,
                    cls.env.ref('ibns_smart_ops.group_sfa_user').id,
                ])
            ]
        })

        # 3. User Driver B (untuk pengujian isolasi data antar pengemudi)
        cls.sfa_driver_b = cls.env['res.users'].create({
            'name': 'SFA Test Driver B',
            'login': f'sfa_driver_b_{uuid.uuid4().hex[:8]}',
            'email': 'driver_b@ibns.local',
            'groups_id': [
                (6, 0, [
                    cls.env.ref('base.group_user').id,
                    cls.env.ref('ibns_smart_ops.group_sfa_user').id,
                ])
            ]
        })

        # Daftarkan API key test ke sistem parameter
        cls.env['ir.config_parameter'].sudo().set_param('ibns_smart_ops.sfa_api_key', cls.valid_token)
        cls.env['ir.config_parameter'].sudo().set_param('ibns_smart_ops.sfa_api_user_id', str(cls.sfa_manager.id))

        # Setup data dummy outlet (res.partner) dengan koordinat GPS dan radius geofence 150m
        cls.test_outlet = cls.env['res.partner'].create({
            'name': 'Test Apex Outlet',
            'customer_rank': 1,
            'street': 'Jl. Asia Afrika No. 10',
            'city': 'Bandung',
            'partner_latitude': -6.917464,
            'partner_longitude': 107.619123,
            'geofence_radius_meters': 150.0,
            'outlet_zone': 'west',
        })

        cls.empty_fields_outlet = cls.env['res.partner'].create({
            'name': 'Outlet Tanpa Alamat Lengkap',
            'customer_rank': 1,
            'street': False,
            'city': False,
            'partner_latitude': 0.0,
            'partner_longitude': 0.0,
            'outlet_zone': False,
        })

        # Setup journal penjualan
        cls.sale_journal = cls.env['account.journal'].search([('type', '=', 'sale')], limit=1)
        if not cls.sale_journal:
            cls.sale_journal = cls.env['account.journal'].create({
                'name': 'Customer Invoices Test',
                'type': 'sale',
                'code': 'TSALE',
            })

    # =========================================================================
    # 1. PENGUJIAN AUTENTIKASI: TOKEN ABSEN & TIDAK VALID
    # =========================================================================

    def test_01_unauthorized_missing_token_all_endpoints(self):
        """Memverifikasi seluruh endpoint SFA menolak request tanpa token dengan HTTP 401."""
        # GET /api/v1/sfa/outlets
        res_get = self.url_open(self.endpoint)
        self.assertEqual(res_get.status_code, 401)
        self.assertEqual(res_get.json().get('status'), 'error')

        # POST /api/v1/sfa/sync-visit
        res_visit = self.url_open('/api/v1/sfa/sync-visit', data=json.dumps({'outlet_id': self.test_outlet.id}),
                                  headers={'Content-Type': 'application/json'})
        self.assertEqual(res_visit.status_code, 401)

        # POST /api/v1/sfa/invoices
        res_inv = self.url_open('/api/v1/sfa/invoices', data=json.dumps({'partner_id': self.test_outlet.id, 'amount': 1000}),
                                headers={'Content-Type': 'application/json'})
        self.assertEqual(res_inv.status_code, 401)

    def test_02_unauthorized_invalid_token(self):
        """Request dengan token kadaluarsa/salah via Bearer atau X-IBNS-KEY ditolak dengan HTTP 401:
        Termasuk pengujian khusus header non-ASCII/Unicode yang memverifikasi pencegahan TypeError/HTTP 500."""
        headers_bearer = {'Authorization': 'Bearer wrong_or_expired_token'}
        res_b = self.url_open(self.endpoint, headers=headers_bearer)
        self.assertEqual(res_b.status_code, 401)

        headers_custom = {'X-IBNS-KEY': 'invalid_custom_key_123'}
        res_c = self.url_open(self.endpoint, headers=headers_custom)
        self.assertEqual(res_c.status_code, 401)

        # 1. Verifikasi header X-IBNS-KEY berisi karakter non-ASCII (éñ) merespons HTTP 401 (bukan HTTP 500)
        headers_non_ascii_key = {'X-IBNS-KEY': 'token-non-ascii-éñ'}
        res_na_key = self.url_open(self.endpoint, headers=headers_non_ascii_key)
        self.assertEqual(res_na_key.status_code, 401)
        self.assertEqual(res_na_key.json().get('status'), 'error')

        # 2. Verifikasi header Authorization: Bearer berisi karakter non-ASCII/Unicode (éñü) merespons HTTP 401 (bukan HTTP 500)
        headers_non_ascii_bearer = {'Authorization': 'Bearer token_palsu_éñü'}
        res_na_bearer = self.url_open(self.endpoint, headers=headers_non_ascii_bearer)
        self.assertEqual(res_na_bearer.status_code, 401)
        self.assertEqual(res_na_bearer.json().get('status'), 'error')

    # =========================================================================
    # 2. PENGUJIAN DUAL HEADER AUTHENTICATION (X-IBNS-KEY & Authorization Bearer)
    # =========================================================================

    def test_03_authorized_via_x_ibns_key_header(self):
        """Memverifikasi autentikasi sukses menggunakan header khusus 'X-IBNS-KEY'."""
        headers = {'X-IBNS-KEY': self.valid_token}
        response = self.url_open(self.endpoint, headers=headers)
        self.assertEqual(response.status_code, 200)

        payload = response.json()
        self.assertEqual(payload.get('status'), 'success')
        self.assertTrue(payload.get('total') >= 1)
        self.assertEqual(payload.get('authenticated_user'), self.sfa_manager.name)

    def test_04_authorized_via_bearer_token(self):
        """Memverifikasi autentikasi sukses menggunakan header standar 'Authorization: Bearer <token>'."""
        headers = {'Authorization': f'Bearer {self.valid_token}'}
        response = self.url_open(self.endpoint, headers=headers)
        self.assertEqual(response.status_code, 200)

        payload = response.json()
        self.assertEqual(payload.get('status'), 'success')
        self.assertEqual(payload.get('authenticated_user'), self.sfa_manager.name)

    # =========================================================================
    # 3. PENGUJIAN FAKTUR LENGKAP: RESPON & AUDIT BARIS JURNAL (JOURNAL ITEMS)
    # =========================================================================

    def test_05_create_invoice_and_verify_journal_items(self):
        """POST /api/v1/sfa/invoices berhasil membuat faktur dan memverifikasi baris jurnal (account.move.line):
        - Debit Piutang Usaha (Receivable) == 1.500.000
        - Credit Pendapatan Penjualan (Revenue) == 1.500.000
        - Keseimbangan debit-kredit (Double-entry balance)"""
        dummy_partner = self.env['res.partner'].create({
            'name': 'PT SFA Mitra Sejahtera',
            'customer_rank': 1,
            'street': 'Jl. Sukajadi No. 88',
            'city': 'Bandung',
            'partner_latitude': -6.890000,
            'partner_longitude': 107.600000,
            'outlet_zone': 'north',
            'active': True,
        })

        headers = {
            'X-IBNS-KEY': self.valid_token,
            'Content-Type': 'application/json',
        }
        payload = {
            'partner_id': dummy_partner.id,
            'amount': 1500000.0,
            'description': 'Penagihan SFA Field Order Batch #4412',
        }
        response = self.url_open('/api/v1/sfa/invoices', data=json.dumps(payload), headers=headers)
        self.assertEqual(response.status_code, 201)

        res_data = response.json()
        self.assertEqual(res_data.get('status'), 'success')
        self.assertEqual(res_data.get('created_by'), self.sfa_manager.name)
        invoice_id = res_data.get('invoice_id')
        self.assertTrue(invoice_id)

        # 1. Verifikasi Record Faktur (account.move)
        invoice = self.env['account.move'].browse(invoice_id)
        self.assertTrue(invoice.exists(), "Record account.move harus ada di database")
        self.assertEqual(invoice.state, 'posted', "Faktur harus terposting otomatis (posted)")
        self.assertEqual(invoice.amount_total, 1500000.0, "Total faktur harus Rp 1.500.000")
        self.assertEqual(invoice.partner_id.id, dummy_partner.id)

        # 2. Verifikasi Invoice Lines (account.move.line spesifik produk/deskripsi)
        self.assertTrue(len(invoice.invoice_line_ids) >= 1, "Harus terdapat minimal 1 invoice line")
        inv_line = invoice.invoice_line_ids[0]
        self.assertEqual(inv_line.name, 'Penagihan SFA Field Order Batch #4412')
        self.assertEqual(inv_line.price_unit, 1500000.0)
        self.assertEqual(inv_line.quantity, 1.0)

        # 3. Verifikasi Baris Jurnal Akuntansi (Journal Items / line_ids)
        # Pada pembukuan berpasangan Odoo: harus ada debit dan credit yang seimbang
        journal_items = invoice.line_ids
        self.assertTrue(len(journal_items) >= 2, "Harus ada minimal 2 baris jurnal (Debit & Kredit)")

        total_debit = sum(journal_items.mapped('debit'))
        total_credit = sum(journal_items.mapped('credit'))
        self.assertEqual(total_debit, 1500000.0, "Total debit jurnal harus 1.500.000")
        self.assertEqual(total_credit, 1500000.0, "Total credit jurnal harus 1.500.000")
        self.assertEqual(total_debit, total_credit, "Prinsip akuntansi: Debit harus persis seimbang dengan Kredit")

        # 4. Verifikasi Baris Piutang (Receivable Line)
        receivable_line = journal_items.filtered(lambda l: l.debit > 0)
        self.assertTrue(receivable_line, "Harus ditemukan baris debit piutang")
        self.assertEqual(receivable_line[0].debit, 1500000.0)
        self.assertEqual(receivable_line[0].partner_id.id, dummy_partner.id)
        self.assertEqual(
            receivable_line[0].account_id.account_type,
            'asset_receivable',
            "Tipe akun baris debit harus Piutang Usaha (asset_receivable)"
        )

        # 5. Verifikasi Baris Pendapatan (Income / Revenue Line)
        income_line = journal_items.filtered(lambda l: l.credit > 0)
        self.assertTrue(income_line, "Harus ditemukan baris kredit pendapatan")
        self.assertEqual(income_line[0].credit, 1500000.0)
        self.assertIn(
            income_line[0].account_id.account_type,
            ('income', 'income_other'),
            "Tipe akun baris kredit harus Pendapatan (income atau income_other)"
        )

    # =========================================================================
    # 4. PENGUJIAN INPUT RUSAK, MALFORMED JSON & VALIDASI FIELD
    # =========================================================================

    def test_06_malformed_json_and_broken_input_rejection(self):
        """Memverifikasi penolakan input rusak pada endpoint faktur dan kunjungan:
        - Sintaks JSON terpotong / cacat
        - JSON non-objek (array atau string)
        - Field yang hilang atau tipe data salah"""
        headers = {
            'X-IBNS-KEY': self.valid_token,
            'Content-Type': 'application/json'
        }

        # 1. JSON cacat pada endpoint faktur
        res_broken_inv = self.url_open('/api/v1/sfa/invoices', data='{"partner_id": 1, amount: bad_syntax', headers=headers)
        self.assertEqual(res_broken_inv.status_code, 400)
        self.assertIn('Malformed JSON', res_broken_inv.json().get('message', ''))

        # 2. JSON cacat pada endpoint sync-visit
        res_broken_vis = self.url_open('/api/v1/sfa/sync-visit', data='{unclosed json', headers=headers)
        self.assertEqual(res_broken_vis.status_code, 400)
        self.assertIn('Malformed JSON', res_broken_vis.json().get('message', ''))

        # 3. JSON non-objek (misal array)
        res_array = self.url_open('/api/v1/sfa/invoices', data='[1, 2, 3]', headers=headers)
        self.assertEqual(res_array.status_code, 400)
        self.assertIn('valid JSON object', res_array.json().get('message', ''))

        # 4. Partner id format salah (string non-angka)
        res_wrong_partner = self.url_open('/api/v1/sfa/invoices',
                                          data=json.dumps({'partner_id': 'abc', 'amount': 100000.0}),
                                          headers=headers)
        self.assertEqual(res_wrong_partner.status_code, 400)
        self.assertIn('partner_id format', res_wrong_partner.json().get('message', ''))

        # 5. Partner aktif tidak ditemukan (ID 999999)
        res_not_found = self.url_open('/api/v1/sfa/invoices',
                                      data=json.dumps({'partner_id': 999999, 'amount': 100000.0}),
                                      headers=headers)
        self.assertEqual(res_not_found.status_code, 400)
        self.assertIn('partner not found', res_not_found.json().get('message', '').lower())

        # 6. Amount string non-angka
        res_bad_amount = self.url_open('/api/v1/sfa/invoices',
                                       data=json.dumps({'partner_id': self.test_outlet.id, 'amount': 'seratus_ribu'}),
                                       headers=headers)
        self.assertEqual(res_bad_amount.status_code, 400)
        self.assertIn('amount format', res_bad_amount.json().get('message', '').lower())

    # =========================================================================
    # 5. PENGUJIAN KHUSUS NaN, INFINITY, DAN NILAI AMOUNT NON-POSITIF
    # =========================================================================

    def test_07_nan_infinity_and_non_positive_amount(self):
        """Memverifikasi bahwa amount NaN, Infinity, -Infinity, 0, dan angka negatif ditolak dengan HTTP 400."""
        headers = {
            'X-IBNS-KEY': self.valid_token,
            'Content-Type': 'application/json'
        }

        invalid_amounts = ['nan', 'NaN', 'inf', '-inf', 'Infinity', '-Infinity', 0, -50000.0, '-10000']
        for bad_val in invalid_amounts:
            payload = {'partner_id': self.test_outlet.id, 'amount': bad_val}
            res = self.url_open('/api/v1/sfa/invoices', data=json.dumps(payload), headers=headers)
            self.assertEqual(res.status_code, 400, f"Amount '{bad_val}' harus ditolak dengan HTTP 400")
            msg = res.json().get('message', '')
            self.assertTrue('finite number' in msg or 'amount format' in msg)

    # =========================================================================
    # 6. PENGUJIAN ENDPOINT SYNC-VISIT SECARA MENDALAM
    # =========================================================================

    def test_08_sync_visit_input_validation(self):
        """Pengujian validasi input pada endpoint /api/v1/sfa/sync-visit:
        - outlet_id hilang
        - outlet_id salah format
        - outlet_id tidak ditemukan (404)"""
        headers = {
            'X-IBNS-KEY': self.valid_token,
            'Content-Type': 'application/json'
        }

        # 1. outlet_id hilang
        res_no_outlet = self.url_open('/api/v1/sfa/sync-visit', data=json.dumps({'notes': 'Halo'}), headers=headers)
        self.assertEqual(res_no_outlet.status_code, 400)
        self.assertIn('outlet_id is required', res_no_outlet.json().get('message', ''))

        # 2. outlet_id format salah
        res_bad_format = self.url_open('/api/v1/sfa/sync-visit', data=json.dumps({'outlet_id': 'outlet_satu'}), headers=headers)
        self.assertEqual(res_bad_format.status_code, 400)
        self.assertIn('outlet_id format', res_bad_format.json().get('message', ''))

        # 3. outlet_id tidak ditemukan
        res_not_found = self.url_open('/api/v1/sfa/sync-visit', data=json.dumps({'outlet_id': 999999}), headers=headers)
        self.assertEqual(res_not_found.status_code, 404)
        self.assertIn('Outlet not found', res_not_found.json().get('message', ''))

    def test_09_sync_visit_geofencing_and_chatter_markup(self):
        """Pengujian check-in kunjungan lapangan dengan kalkulasi Haversine Geofencing dan audit chatter:
        - Check-in via header X-IBNS-KEY
        - Check-in dalam radius geofence (jarak dekat)
        - Check-in di luar radius geofence (jarak jauh)
        - Format HTML markup pada chatter"""
        headers = {
            'X-IBNS-KEY': self.valid_token,
            'Content-Type': 'application/json'
        }

        # 1. Check-in dalam radius outlet (< 150m)
        payload_inside = {
            'outlet_id': self.test_outlet.id,
            'checkin_latitude': -6.917500,
            'checkin_longitude': 107.619150,
            'notes': 'Sales check-in langsung di depan outlet.'
        }
        res_inside = self.url_open('/api/v1/sfa/sync-visit', data=json.dumps(payload_inside), headers=headers)
        self.assertEqual(res_inside.status_code, 200)
        data_inside = res_inside.json()
        self.assertEqual(data_inside.get('status'), 'success')
        self.assertEqual(data_inside.get('logged_by'), self.sfa_manager.name)

        geofence_data = data_inside.get('geofence')
        self.assertTrue(geofence_data)
        self.assertTrue(geofence_data.get('verified'), "Check-in dekat harus terverifikasi (verified: True)")
        self.assertTrue(geofence_data.get('distance_meters') < 150.0)

        # Verifikasi chatter message
        latest_msg = self.test_outlet.message_ids[0]
        self.assertIn('Geofence Verified', latest_msg.body)
        self.assertIn('Sales check-in langsung di depan outlet.', latest_msg.body)

        # 2. Check-in jauh di luar radius outlet (> 50km)
        payload_outside = {
            'outlet_id': self.test_outlet.id,
            'checkin_latitude': -6.208800,
            'checkin_longitude': 106.845600,
            'notes': 'Check-in fiktif dari luar kota.'
        }
        res_outside = self.url_open('/api/v1/sfa/sync-visit', data=json.dumps(payload_outside), headers=headers)
        self.assertEqual(res_outside.status_code, 200)
        data_outside = res_outside.json()
        geofence_outside = data_outside.get('geofence')
        self.assertFalse(geofence_outside.get('verified'), "Check-in jauh harus mismatch (verified: False)")
        self.assertTrue(geofence_outside.get('distance_meters') > 10000.0)

        # Verifikasi chatter mencatat alert mismatch
        latest_msg_outside = self.test_outlet.message_ids[0]
        self.assertIn('Geofence Mismatch', latest_msg_outside.body)

    # =========================================================================
    # 7. PENGUJIAN HAK AKSES (RBAC & RECORD RULES ISOLATION)
    # =========================================================================

    def test_10_rbac_driver_permission_boundary(self):
        """Memverifikasi batasan hak akses akun Driver (group_sfa_user):
        - Driver BISA mengakses outlet dan submit visit
        - Driver DITOLAK (HTTP 403) saat mencoba membuat faktur penjualan"""
        # Konfigurasi sistem untuk menjalankan request dengan identitas Driver
        self.env['ir.config_parameter'].sudo().set_param('ibns_smart_ops.sfa_api_key', self.driver_token)
        self.env['ir.config_parameter'].sudo().set_param('ibns_smart_ops.sfa_api_user_id', str(self.sfa_driver.id))

        driver_headers = {
            'X-IBNS-KEY': self.driver_token,
            'Content-Type': 'application/json'
        }

        # 1. Driver membaca outlet -> Berhasil (HTTP 200)
        res_outlets = self.url_open(self.endpoint, headers=driver_headers)
        self.assertEqual(res_outlets.status_code, 200)
        self.assertEqual(res_outlets.json().get('authenticated_user'), self.sfa_driver.name)

        # 2. Driver submit visit check-in -> Berhasil (HTTP 200)
        res_visit = self.url_open('/api/v1/sfa/sync-visit',
                                  data=json.dumps({'outlet_id': self.test_outlet.id, 'notes': 'Driver tiba di outlet'}),
                                  headers=driver_headers)
        self.assertEqual(res_visit.status_code, 200)
        self.assertEqual(res_visit.json().get('logged_by'), self.sfa_driver.name)

        # 3. Driver mencoba membuat faktur -> DITOLAK (HTTP 403 Forbidden)
        res_inv = self.url_open('/api/v1/sfa/invoices',
                                data=json.dumps({'partner_id': self.test_outlet.id, 'amount': 250000.0}),
                                headers=driver_headers)
        self.assertEqual(res_inv.status_code, 403, "Driver tanpa hak billing harus ditolak dengan HTTP 403")
        self.assertIn('Forbidden', res_inv.json().get('message', ''))

    def test_11_record_rules_driver_picking_isolation(self):
        """Memverifikasi Record Rules (ir.rule): Driver hanya dapat melihat delivery order
        yang ditugaskan ke namanya sendiri atau unassigned, dan terisolasi dari driver lain."""
        picking_type = self.env['stock.picking.type'].search([('code', '=', 'outgoing')], limit=1)
        if not picking_type:
            return

        # 1. Buat Picking A untuk sfa_driver
        picking_a = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'partner_id': self.test_outlet.id,
            'location_id': picking_type.default_location_src_id.id,
            'location_dest_id': self.test_outlet.property_stock_customer.id,
            'driver_id': self.sfa_driver.id,
            'driver_name': self.sfa_driver.name,
            'delivery_status': 'scheduled',
        })

        # 2. Buat Picking B untuk Driver B (sfa_driver_b)
        picking_b = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'partner_id': self.test_outlet.id,
            'location_id': picking_type.default_location_src_id.id,
            'location_dest_id': self.test_outlet.property_stock_customer.id,
            'driver_id': self.sfa_driver_b.id,
            'driver_name': self.sfa_driver_b.name,
            'delivery_status': 'scheduled',
        })

        test_prod = self.env['product.product'].search([], limit=1)
        if not test_prod:
            test_prod = self.env['product.product'].create({'name': 'Barang Uji Kirim'})

        move_a = self.env['stock.move'].create({
            'name': 'Barang Kirim Driver A',
            'product_id': test_prod.id,
            'product_uom_qty': 10.0,
            'picking_id': picking_a.id,
            'location_id': picking_type.default_location_src_id.id,
            'location_dest_id': self.test_outlet.property_stock_customer.id,
        })
        move_b = self.env['stock.move'].create({
            'name': 'Barang Kirim Driver B',
            'product_id': test_prod.id,
            'product_uom_qty': 20.0,
            'picking_id': picking_b.id,
            'location_id': picking_type.default_location_src_id.id,
            'location_dest_id': self.test_outlet.property_stock_customer.id,
        })

        # 3. Query dengan konteks sfa_driver: Hanya picking_a dan move_a yang boleh terlihat
        driver_visible_pickings = self.env['stock.picking'].with_user(self.sfa_driver).search([
            ('id', 'in', [picking_a.id, picking_b.id])
        ])
        self.assertIn(picking_a, driver_visible_pickings, "Driver harus melihat order tugasnya sendiri")
        self.assertNotIn(picking_b, driver_visible_pickings, "Record rule harus menyembunyikan tugas driver lain")

        # Driver membaca picking form/fields dan relasi move_ids tanpa AccessError
        p_read = picking_a.with_user(self.sfa_driver).read(['name', 'partner_id', 'move_ids'])
        self.assertTrue(p_read)
        self.assertEqual(picking_a.with_user(self.sfa_driver).move_ids.ids, [move_a.id])

        # Verifikasi isolasi stock.move: Driver A hanya dapat melihat move_a, bukan move_b
        driver_visible_moves = self.env['stock.move'].with_user(self.sfa_driver).search([
            ('id', 'in', [move_a.id, move_b.id])
        ])
        self.assertIn(move_a, driver_visible_moves, "Driver harus bisa melihat baris barang tugasnya")
        self.assertNotIn(move_b, driver_visible_moves, "Driver tidak boleh melihat baris barang driver lain")

        # 4. Query dengan konteks Manager: Kedua picking harus terlihat
        manager_visible_pickings = self.env['stock.picking'].with_user(self.sfa_manager).search([
            ('id', 'in', [picking_a.id, picking_b.id])
        ])
        self.assertIn(picking_a, manager_visible_pickings)
        self.assertIn(picking_b, manager_visible_pickings)

        # 5. Uji Pengalihan Driver (Reassignment): Ubah driver_id picking_a ke sfa_driver_b tanpa menyertakan driver_name
        picking_a.write({'driver_id': self.sfa_driver_b.id})
        self.assertEqual(
            picking_a.driver_name,
            self.sfa_driver_b.name,
            "driver_name harus otomatis tersinkronisasi saat driver_id dialihkan ke driver lain"
        )
        # Driver A tidak boleh lagi melihat picking_a setelah dialihkan ke Driver B
        driver_a_visible_after = self.env['stock.picking'].with_user(self.sfa_driver).search([
            ('id', 'in', [picking_a.id, picking_b.id])
        ])
        self.assertNotIn(picking_a, driver_a_visible_after, "Driver A tidak boleh lagi melihat picking_a setelah dialihkan ke Driver B")

        # 6. Uji Penghapusan Driver (Unassignment): Set driver_id menjadi False
        picking_a.write({'driver_id': False})
        self.assertFalse(
            picking_a.driver_name,
            "driver_name wajib di-reset menjadi False saat driver_id dikosongkan (unassigned)"
        )

        # 7. Uji UI Onchange: Simulasi _onchange_driver_id pada form view Odoo
        new_picking = self.env['stock.picking'].new({
            'picking_type_id': picking_type.id,
            'partner_id': self.test_outlet.id,
        })
        new_picking.driver_id = self.sfa_driver
        new_picking._onchange_driver_id()
        self.assertEqual(new_picking.driver_name, self.sfa_driver.name)

        # Ganti driver via UI onchange saat driver_name sudah terisi sebelumnya
        new_picking.driver_id = self.sfa_driver_b
        new_picking._onchange_driver_id()
        self.assertEqual(new_picking.driver_name, self.sfa_driver_b.name, "Onchange harus memperbarui driver_name saat driver diganti")

        # Kosongkan driver via UI onchange
        new_picking.driver_id = False
        new_picking._onchange_driver_id()
        self.assertFalse(new_picking.driver_name, "Onchange harus mengosongkan driver_name saat driver_id dihapus")

    # =========================================================================
    # 8. PENGUJIAN IDEMPOTENCY & SERIALISASI NULL
    # =========================================================================

    def test_12_invoice_idempotency_replay(self):
        """Memverifikasi proteksi faktur ganda via header X-Idempotency-Key."""
        idemp_token = f"key_{uuid.uuid4().hex}"
        headers = {
            'X-IBNS-KEY': self.valid_token,
            'Content-Type': 'application/json',
            'X-Idempotency-Key': idemp_token
        }
        payload = {
            'partner_id': self.test_outlet.id,
            'amount': 300000.0,
            'description': 'Pengiriman Logistik Pagi Batch 1'
        }

        # Request 1 -> 201 Created
        res1 = self.url_open('/api/v1/sfa/invoices', data=json.dumps(payload), headers=headers)
        self.assertEqual(res1.status_code, 201)
        inv_id = res1.json().get('invoice_id')
        self.assertFalse(res1.json().get('idempotent_replay'))

        # Request 2 (Network Retry) -> 200 OK dengan invoice_id yang persis sama
        res2 = self.url_open('/api/v1/sfa/invoices', data=json.dumps(payload), headers=headers)
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.json().get('invoice_id'), inv_id)
        self.assertTrue(res2.json().get('idempotent_replay'))

        # Pastikan di database hanya ada 1 invoice
        total_inv = self.env['account.move'].search_count([('sfa_idempotency_key', '=', idemp_token)])
        self.assertEqual(total_inv, 1)

        # Verifikasi SQL constraint menolak duplikasi sfa_idempotency_key secara langsung
        with self.assertRaises(psycopg2.IntegrityError):
            with self.cr.savepoint():
                with mute_logger('odoo.sql_db'):
                    self.env['account.move'].create({
                        'move_type': 'out_invoice',
                        'partner_id': self.test_outlet.id,
                        'journal_id': self.sale_journal.id,
                        'sfa_idempotency_key': idemp_token,
                    })

    def test_13_empty_fields_serialized_as_null(self):
        """Memverifikasi field kosong diserialisasi sebagai null (bukan boolean false)."""
        headers = {'X-IBNS-KEY': self.valid_token}
        response = self.url_open(self.endpoint, headers=headers)
        self.assertEqual(response.status_code, 200)
        outlets = response.json().get('outlets', [])
        empty_data = next((o for o in outlets if o['id'] == self.empty_fields_outlet.id), None)
        self.assertTrue(empty_data)
        self.assertIsNone(empty_data['street'])
        self.assertIsNone(empty_data['city'])
        self.assertNotEqual(empty_data['street'], False)
        self.assertIsNone(empty_data['partner_latitude'])
        self.assertIsNone(empty_data['partner_longitude'])
        self.assertNotEqual(empty_data['partner_latitude'], 0.0)
        self.assertNotEqual(empty_data['partner_longitude'], 0.0)

    def test_14_delivered_vs_invoiced_picking_audit(self):
        """Memverifikasi komputasi audit Delivered vs Invoiced pada stock.picking untuk seluruh 5 status:
        a) 'no_sale_order': Outgoing picking tanpa kaitan Sales Order
        b) 'not_delivered': Outgoing picking terkait SO yang belum dikirim & belum difakturkan
        c) 'invoiced_pending_delivery': Faktur SO sudah posted, tapi picking belum done
        d) 'delivered_pending_invoice': Picking sudah done, tapi faktur belum dibuat/posted
        e) 'fully_reconciled': Picking sudah done DAN faktur SO sudah posted penuh
        Serta memastikan incoming picking (picking_type_id.code == 'incoming') TIDAK mendapat status no_sale_order (harus False)."""
        picking_type = self.env['stock.picking.type'].search([('code', '=', 'outgoing')], limit=1)
        self.assertTrue(picking_type, "Outgoing picking type harus tersedia di sistem")

        # Setup produk dan Sales Order untuk pengujian audit siklus penjualan
        audit_product = self.env['product.product'].create({
            'name': 'Produk Audit Logistik TMS',
            'type': 'consu',
            'list_price': 100000.0,
        })
        sale_order = self.env['sale.order'].create({
            'partner_id': self.test_outlet.id,
            'order_line': [(0, 0, {
                'product_id': audit_product.id,
                'product_uom_qty': 10.0,
                'price_unit': 100000.0,
            })]
        })

        # a) Verifikasi status 'no_sale_order': Outgoing picking tanpa kaitan sale_id
        picking_no_so = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'partner_id': self.test_outlet.id,
            'location_id': picking_type.default_location_src_id.id,
            'location_dest_id': self.test_outlet.property_stock_customer.id,
            'delivery_status': 'unassigned',
        })
        self.assertEqual(picking_no_so.delivered_vs_invoiced_status, 'no_sale_order',
                         "Outgoing picking tanpa sale_id harus berstatus 'no_sale_order'")

        # b) Verifikasi status 'not_delivered': Outgoing picking terkait SO yang belum dikirim & belum difakturkan
        picking_so = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'partner_id': self.test_outlet.id,
            'location_id': picking_type.default_location_src_id.id,
            'location_dest_id': self.test_outlet.property_stock_customer.id,
            'sale_id': sale_order.id,
            'delivery_status': 'scheduled',
            'state': 'assigned',
        })
        self.assertEqual(picking_so.delivered_vs_invoiced_status, 'not_delivered',
                         "Outgoing picking terkait SO yang belum dikirim & belum difaktur harus berstatus 'not_delivered'")

        # d) Verifikasi status 'delivered_pending_invoice': Picking sudah done/delivered, tapi faktur belum dibuat/posted
        picking_so.write({'delivery_status': 'delivered'})
        self.assertEqual(picking_so.delivered_vs_invoiced_status, 'delivered_pending_invoice',
                         "Picking sudah delivered tapi faktur belum dibuat/posted harus berstatus 'delivered_pending_invoice'")

        # c) Verifikasi status 'invoiced_pending_delivery': Faktur SO sudah posted, tapi picking belum done
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.test_outlet.id,
            'journal_id': self.sale_journal.id,
            'invoice_line_ids': [(0, 0, {
                'product_id': audit_product.id,
                'quantity': 10.0,
                'price_unit': 100000.0,
                'sale_line_ids': [(6, 0, sale_order.order_line.ids)],
            })]
        })
        invoice.action_post()
        # Kembalikan status picking ke scheduled/assigned (belum terkirim)
        picking_so.write({'delivery_status': 'scheduled', 'state': 'assigned'})
        self.assertEqual(picking_so.delivered_vs_invoiced_status, 'invoiced_pending_delivery',
                         "Faktur SO sudah posted tapi picking belum done harus berstatus 'invoiced_pending_delivery'")

        # e) Verifikasi status 'fully_reconciled': Picking sudah done DAN faktur SO sudah posted penuh
        sale_order.order_line.write({'qty_delivered': 10.0})
        picking_so.write({'delivery_status': 'delivered', 'state': 'done'})
        self.assertEqual(picking_so.delivered_vs_invoiced_status, 'fully_reconciled',
                         "Picking sudah done dan faktur SO sudah posted penuh harus berstatus 'fully_reconciled'")

        # Verifikasi batas: Operasi penerimaan barang (incoming picking) TIDAK mendapat status 'no_sale_order' (harus False)
        incoming_type = self.env['stock.picking.type'].search([('code', '=', 'incoming')], limit=1)
        self.assertTrue(incoming_type, "Incoming picking type harus tersedia di sistem")
        incoming_picking = self.env['stock.picking'].create({
            'picking_type_id': incoming_type.id,
            'partner_id': self.test_outlet.id,
            'location_id': self.test_outlet.property_stock_customer.id,
            'location_dest_id': incoming_type.default_location_dest_id.id,
        })
        self.assertFalse(incoming_picking.delivered_vs_invoiced_status,
                         "Incoming picking tidak boleh berstatus 'no_sale_order' (harus False)")

    # =========================================================================
    # 9. PENGUJIAN TAHAP 2: FALLBACK EMAIL CHATTER, RATE GUARD CLEANUP, SQL CONSTRAINT
    # =========================================================================

    def test_15_sync_visit_fallback_without_user_email(self):
        """Memverifikasi bahwa user tanpa alamat email (email=False) tetap dapat submit
        kunjungan lapangan tanpa melempar UserError pada chatter Odoo 18 berkat fallback email_from."""
        no_email_user = self.env['res.users'].create({
            'name': 'Driver Tanpa Email',
            'login': f'driver_no_email_{uuid.uuid4().hex[:8]}',
            'email': False,
            'groups_id': [
                (6, 0, [
                    self.env.ref('base.group_user').id,
                    self.env.ref('ibns_smart_ops.group_sfa_user').id,
                ])
            ]
        })
        # Pastikan email benar-benar kosong
        no_email_user.partner_id.email = False

        no_email_token = f"test_suite_no_email_dummy_key_do_not_use_in_prod_{uuid.uuid4().hex}"
        self.env['ir.config_parameter'].sudo().set_param('ibns_smart_ops.sfa_api_key', no_email_token)
        self.env['ir.config_parameter'].sudo().set_param('ibns_smart_ops.sfa_api_user_id', str(no_email_user.id))

        headers = {
            'X-IBNS-KEY': no_email_token,
            'Content-Type': 'application/json'
        }
        payload = {
            'outlet_id': self.test_outlet.id,
            'notes': 'Visit note dari user tanpa email'
        }
        res = self.url_open('/api/v1/sfa/sync-visit', data=json.dumps(payload), headers=headers)
        self.assertEqual(res.status_code, 200, "User tanpa email harus sukses berkat fallback email_from")
        self.assertEqual(res.json().get('status'), 'success')

        latest_msg = self.test_outlet.message_ids[0]
        self.assertIn('Visit note dari user tanpa email', latest_msg.body)
        self.assertIn('noreply@ibns.local', latest_msg.email_from)

    def test_16_rate_guard_memory_leak_cleanup(self):
        """Memverifikasi pembersihan in-memory Rate Guard:
        Timestamp yang sudah kedaluwarsa (> 60 detik) otomatis dibersihkan setiap pengecekan
        sehingga tidak terjadi kebocoran memori (memory leak)."""
        stale_ident = "192.168.99.99"
        now = time.time()
        # Simulasikan timestamp kedaluwarsa 90 detik yang lalu
        _rate_limit_records[stale_ident] = [now - 90.0, now - 80.0]

        # Simulasikan identitas aktif dengan campuran timestamp kedaluwarsa dan baru
        active_ident = "192.168.99.100"
        _rate_limit_records[active_ident] = [now - 120.0, now - 10.0]

        # Panggil pembersihan melalui request HTTP normal
        headers = {'X-IBNS-KEY': self.valid_token}
        self.url_open(self.endpoint, headers=headers)

        # stale_ident harus sudah dihapus sepenuhnya dari dictionary
        self.assertNotIn(stale_ident, _rate_limit_records, "Identifier kadaluarsa harus dibersihkan dari memori")

        # active_ident hanya boleh menyisakan timestamp yang masih valid
        self.assertIn(active_ident, _rate_limit_records)
        self.assertEqual(len(_rate_limit_records[active_ident]), 1)
        self.assertAlmostEqual(_rate_limit_records[active_ident][0], now - 10.0, delta=5.0)

        # Bersihkan mock data
        _rate_limit_records.pop(active_ident, None)

    def test_17_invoice_idempotency_sql_constraint(self):
        """Memverifikasi bahwa database PostgreSQL menolak duplikasi sfa_idempotency_key
        pada level SQL constraint (tahan terhadap race condition)."""
        dup_key = f"unique_test_key_{uuid.uuid4().hex}"
        self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.test_outlet.id,
            'journal_id': self.sale_journal.id,
            'sfa_idempotency_key': dup_key,
        })

        with self.assertRaises(psycopg2.IntegrityError):
            with self.cr.savepoint():
                with mute_logger('odoo.sql_db'):
                    self.env['account.move'].create({
                        'move_type': 'out_invoice',
                        'partner_id': self.test_outlet.id,
                        'journal_id': self.sale_journal.id,
                        'sfa_idempotency_key': dup_key,
                    })

    def test_18_outlets_pagination(self):
        """Memverifikasi query pagination limit dan offset/page pada GET /api/v1/sfa/outlets."""
        headers = {'X-IBNS-KEY': self.valid_token}
        # 1. Limit = 1
        res_limit1 = self.url_open(f"{self.endpoint}?limit=1", headers=headers)
        self.assertEqual(res_limit1.status_code, 200)
        data1 = res_limit1.json()
        self.assertEqual(data1.get('limit'), 1)
        self.assertEqual(data1.get('count'), 1)
        self.assertEqual(len(data1.get('outlets', [])), 1)
        self.assertTrue(data1.get('total') >= 2)

        # 2. Offset = 1
        res_offset1 = self.url_open(f"{self.endpoint}?limit=1&offset=1", headers=headers)
        self.assertEqual(res_offset1.status_code, 200)
        data2 = res_offset1.json()
        self.assertEqual(data2.get('offset'), 1)
        self.assertEqual(data2.get('count'), 1)
        self.assertNotEqual(data1['outlets'][0]['id'], data2['outlets'][0]['id'])

    def test_19_default_zone_is_not_forced_east(self):
        """Memverifikasi bahwa partner baru tanpa outlet_zone tidak otomatis dipaksa menjadi 'east'."""
        new_partner = self.env['res.partner'].create({
            'name': 'Partner Netral Tanpa Zona',
            'customer_rank': 1,
        })
        self.assertFalse(new_partner.outlet_zone, "Zona outlet partner baru harus kosong (False), bukan default 'east'")

    def test_20_rate_limit_429_too_many_requests(self):
        """Memverifikasi penolakan HTTP 429 Too Many Requests saat ambang batas Rate Guard terlampaui:
        Benar-benar menembak endpoint hingga melewati batas 60 request/menit secara nyata,
        kemudian memverifikasi status code 429 beserta keberadaan header Retry-After."""
        _rate_limit_records.clear()
        headers = {'X-IBNS-KEY': self.valid_token}

        try:
            # 1. Tembak endpoint sebanyak 60 request yang sah secara beruntun (mencapai batas kuota)
            for i in range(60):
                res = self.url_open(self.endpoint, headers=headers)
                self.assertEqual(res.status_code, 200, f"Request ke-{i + 1} harus berhasil (HTTP 200)")

            # 2. Request ke-61 menembus batas kuota dalam jendela 60 detik -> Ditolak dengan HTTP 429
            response = self.url_open(self.endpoint, headers=headers)
            self.assertEqual(response.status_code, 429, "Request ke-61 harus ditolak dengan HTTP 429 Too Many Requests")

            payload = response.json()
            self.assertEqual(payload.get('status'), 'error')
            self.assertIn('Too Many Requests', payload.get('message', ''))
            self.assertIn('Retry after', payload.get('message', ''))

            # 3. Verifikasi header respons Rate Limiter
            self.assertIn('Retry-After', response.headers, "Header 'Retry-After' harus ada pada respons HTTP 429")
            self.assertEqual(response.headers.get('X-RateLimit-Limit'), '60')
            self.assertEqual(response.headers.get('X-RateLimit-Remaining'), '0')
        finally:
            # Bersihkan cache rate limiter agar tidak mempengaruhi pengujian lain
            _rate_limit_records.clear()

    def test_21_invoice_idempotency_concurrent_race_condition(self):
        """Memverifikasi penanganan race condition saat dua request konkurensi tiba bersamaan:
        Jika query search() awal belum melihat record karena belum commit, lalu create()
        menabrak unique constraint (IntegrityError), kontroler menangkap eksepsi,
        melakukan rollback, mengambil record yang tersimpan, dan mengembalikan HTTP 200 replay."""
        from unittest.mock import patch

        race_key = f"race_key_{uuid.uuid4().hex}"
        # 1. Buat invoice awal yang sudah ada di database
        first_invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.test_outlet.id,
            'journal_id': self.sale_journal.id,
            'sfa_idempotency_key': race_key,
            'invoice_line_ids': [
                (0, 0, {
                    'name': 'Invoice Pertama Berhasil Terbit',
                    'quantity': 1.0,
                    'price_unit': 250000.0,
                })
            ]
        })
        first_invoice.action_post()

        headers = {
            'X-IBNS-KEY': self.valid_token,
            'Content-Type': 'application/json',
            'X-Idempotency-Key': race_key,
        }
        payload = {
            'partner_id': self.test_outlet.id,
            'amount': 250000.0,
            'description': 'Simulasi Race Condition Invoice'
        }

        # 2. Simulasikan search() awal mengembalikan kosong (seperti kondisi transaksi belum commit saat dibaca)
        call_count = [0]
        orig_search = type(self.env['account.move']).search

        def mock_search(self_model, domain, *args, **kwargs):
            if any(isinstance(cond, (list, tuple)) and cond[0] == 'sfa_idempotency_key' for cond in domain):
                call_count[0] += 1
                if call_count[0] == 1:
                    # Panggilan pertama (search pra-create): simulasikan belum melihat record
                    return self_model.browse()
            return orig_search(self_model, domain, *args, **kwargs)

        with patch.object(type(self.env['account.move']), 'search', side_effect=mock_search, autospec=True):
            with mute_logger('odoo.sql_db'):
                response = self.url_open('/api/v1/sfa/invoices', data=json.dumps(payload), headers=headers)

        self.assertEqual(response.status_code, 200, "Request harus berhasil dengan HTTP 200 (Replay), bukan HTTP 500")
        res_data = response.json()
        self.assertEqual(res_data.get('status'), 'success')
        self.assertEqual(res_data.get('invoice_id'), first_invoice.id)
        self.assertTrue(res_data.get('idempotent_replay'))

    def test_22_multi_company_allowed_company_ids_invoice_creation(self):
        """Memverifikasi penanganan multi-company dan allowed_company_ids:
        1. get_outlets() membaca partner milik Perusahaan B jika user memiliki hak ke Perusahaan B.
        2. sync_visit_data() mencatat kunjungan pada partner milik Perusahaan B tanpa AccessError.
        3. create_invoice() berhasil menerbitkan faktur untuk partner milik Perusahaan B dengan
           company_id dan jurnal Perusahaan B yang sesuai (bebas dari error Incompatible companies).
        4. create_invoice() menolak pembuatan faktur dengan HTTP 400 jika target_company belum memiliki
           jurnal penjualan (mencegah fallback ke jurnal perusahaan lain yang memicu Incompatible companies)."""
        # 1. Setup Perusahaan B
        company_b = self.env['res.company'].create({
            'name': 'IBNS Distribusi Cabang B',
            'currency_id': self.env.ref('base.IDR').id if self.env.ref('base.IDR', raise_if_not_found=False) else self.env.company.currency_id.id,
        })

        # Setup Akun Penjualan & Piutang untuk Perusahaan B (kode alfanumerik tanpa underscore)
        income_account_b = self.env['account.account'].create({
            'name': 'Pendapatan Penjualan Cabang B',
            'code': f'499{uuid.uuid4().hex[:4]}',
            'account_type': 'income',
            'company_ids': [(6, 0, [company_b.id])],
        })
        receivable_account_b = self.env['account.account'].create({
            'name': 'Piutang Usaha Cabang B',
            'code': f'111{uuid.uuid4().hex[:4]}',
            'account_type': 'asset_receivable',
            'company_ids': [(6, 0, [company_b.id])],
            'reconcile': True,
        })

        # Setup Sale Journal untuk Perusahaan B
        journal_b = self.env['account.journal'].create({
            'name': 'Penjualan Cabang B',
            'type': 'sale',
            'code': 'SALB',
            'company_id': company_b.id,
            'default_account_id': income_account_b.id,
        })

        # Berikan akses ke Perusahaan B pada sfa_manager
        self.sfa_manager.write({
            'company_ids': [(4, company_b.id)]
        })

        # Buat Partner milik Perusahaan B
        partner_b = self.env['res.partner'].create({
            'name': 'Outlet Eksklusif Cabang B',
            'customer_rank': 1,
            'company_id': company_b.id,
            'street': 'Jl. Diponegoro No. 25',
            'city': 'Surabaya',
            'partner_latitude': -7.257472,
            'partner_longitude': 112.752088,
            'geofence_radius_meters': 200.0,
            'property_account_receivable_id': receivable_account_b.id,
        })

        headers = {
            'X-IBNS-KEY': self.valid_token,
            'Content-Type': 'application/json',
        }

        # 1. GET /api/v1/sfa/outlets harus menyertakan partner_b
        res_outlets = self.url_open('/api/v1/sfa/outlets', headers=headers)
        self.assertEqual(res_outlets.status_code, 200)
        outlet_ids = [o['id'] for o in res_outlets.json().get('outlets', [])]
        self.assertIn(partner_b.id, outlet_ids, "Partner milik Cabang B harus terbaca dalam daftar outlet")

        # 2. POST /api/v1/sfa/sync-visit harus berhasil mencatat check-in pada partner_b tanpa AccessError
        payload_visit = {
            'outlet_id': partner_b.id,
            'checkin_latitude': -7.257500,
            'checkin_longitude': 112.752100,
            'notes': 'Salesman check-in di outlet Cabang B'
        }
        res_visit = self.url_open('/api/v1/sfa/sync-visit', data=json.dumps(payload_visit), headers=headers)
        self.assertEqual(res_visit.status_code, 200, "Sync visit pada partner Cabang B harus berhasil (HTTP 200)")
        self.assertEqual(res_visit.json().get('status'), 'success')

        # 3. POST /api/v1/sfa/invoices harus berhasil membuat dan mem-posting faktur untuk partner_b di Cabang B
        payload_inv = {
            'partner_id': partner_b.id,
            'amount': 750000.0,
            'description': 'Penagihan Multi-Company Cabang B'
        }
        res_inv = self.url_open('/api/v1/sfa/invoices', data=json.dumps(payload_inv), headers=headers)
        self.assertEqual(res_inv.status_code, 201, "Faktur multi-company harus berhasil terbuat (HTTP 201)")
        inv_data = res_inv.json()
        invoice_id = inv_data.get('invoice_id')
        self.assertTrue(invoice_id)

        created_invoice = self.env['account.move'].browse(invoice_id)
        self.assertEqual(created_invoice.company_id.id, company_b.id, "Faktur harus terbit di bawah Perusahaan B")
        self.assertEqual(created_invoice.journal_id.id, journal_b.id, "Faktur harus menggunakan jurnal Perusahaan B")
        self.assertEqual(created_invoice.state, 'posted', "Faktur harus sukses terposting (posted)")

        # 4. Validasi: Penolakan pembuatan faktur jika target company tidak memiliki jurnal penjualan (tanpa fallback)
        company_no_journal = self.env['res.company'].create({
            'name': 'IBNS Distribusi Cabang Tanpa Jurnal',
            'currency_id': self.env.company.currency_id.id,
        })
        self.sfa_manager.write({
            'company_ids': [(4, company_no_journal.id)]
        })
        partner_no_journal = self.env['res.partner'].create({
            'name': 'Outlet Cabang Tanpa Jurnal',
            'company_id': company_no_journal.id,
            'customer_rank': 1,
        })
        payload_no_journal = {
            'partner_id': partner_no_journal.id,
            'amount': 250000.0,
            'description': 'Penagihan Cabang Tanpa Jurnal'
        }
        res_no_journal = self.url_open('/api/v1/sfa/invoices', data=json.dumps(payload_no_journal), headers=headers)
        self.assertEqual(res_no_journal.status_code, 400, "Pembuatan faktur harus ditolak HTTP 400 jika target company tidak memiliki jurnal penjualan")
        self.assertIn('not found', res_no_journal.json().get('message', '').lower())
        self.assertIn('sale journal', res_no_journal.json().get('message', '').lower())

    def test_23_boolean_type_coercion_rejection(self):
        """Memverifikasi bahwa payload dengan tipe data boolean (true/false) ditolak dengan HTTP 400
        pada field yang memerlukan tipe integer atau float (outlet_id, partner_id, amount).
        Mencegah kerentanan Python bool inheritance (int(True) == 1, float(True) == 1.0)."""
        headers = {
            'X-IBNS-KEY': self.valid_token,
            'Content-Type': 'application/json',
        }

        # 1. POST /api/v1/sfa/sync-visit dengan outlet_id = True / False
        res_visit_bool_true = self.url_open('/api/v1/sfa/sync-visit',
                                            data=json.dumps({'outlet_id': True, 'notes': 'Test'}),
                                            headers=headers)
        self.assertEqual(res_visit_bool_true.status_code, 400, "outlet_id boolean true harus ditolak dengan HTTP 400")
        self.assertIn('outlet_id format', res_visit_bool_true.json().get('message', '').lower())

        res_visit_bool_false = self.url_open('/api/v1/sfa/sync-visit',
                                             data=json.dumps({'outlet_id': False, 'notes': 'Test'}),
                                             headers=headers)
        self.assertEqual(res_visit_bool_false.status_code, 400, "outlet_id boolean false harus ditolak dengan HTTP 400")

        # 2. POST /api/v1/sfa/invoices dengan partner_id = True / False
        res_inv_partner_bool = self.url_open('/api/v1/sfa/invoices',
                                             data=json.dumps({'partner_id': True, 'amount': 100000.0}),
                                             headers=headers)
        self.assertEqual(res_inv_partner_bool.status_code, 400, "partner_id boolean true harus ditolak dengan HTTP 400")
        self.assertIn('partner_id format', res_inv_partner_bool.json().get('message', '').lower())

        # 3. POST /api/v1/sfa/invoices dengan amount = True / False
        res_inv_amount_bool_true = self.url_open('/api/v1/sfa/invoices',
                                                 data=json.dumps({'partner_id': self.test_outlet.id, 'amount': True}),
                                                 headers=headers)
        self.assertEqual(res_inv_amount_bool_true.status_code, 400, "amount boolean true harus ditolak dengan HTTP 400")
        self.assertIn('greater than zero', res_inv_amount_bool_true.json().get('message', '').lower())

        res_inv_amount_bool_false = self.url_open('/api/v1/sfa/invoices',
                                                  data=json.dumps({'partner_id': self.test_outlet.id, 'amount': False}),
                                                  headers=headers)
        self.assertEqual(res_inv_amount_bool_false.status_code, 400, "amount boolean false harus ditolak dengan HTTP 400")

    def test_24_default_tax_exclusion_and_amount_total_guarantee(self):
        """Memverifikasi bahwa baris faktur SFA yang dibuat via API mengeksklusikan pajak bawaan
        ('tax_ids': [(5, 0, 0)]), sehingga nominal tagihan yang dikirim klien (amount) tetap bersih
        dan tidak digelembungkan oleh pajak penjualan otomatis (PPN) dari akun pendapatan."""
        # 1. Cari atau buat pajak penjualan (misal PPN 11%)
        sale_tax = self.env['account.tax'].search([
            ('type_tax_use', '=', 'sale'),
            ('company_id', '=', self.env.company.id)
        ], limit=1)
        if not sale_tax:
            sale_tax = self.env['account.tax'].create({
                'name': 'PPN Penjualan 11%',
                'amount_type': 'percent',
                'amount': 11.0,
                'type_tax_use': 'sale',
                'company_id': self.env.company.id,
            })

        # 2. Pasang pajak penjualan tersebut pada akun pendapatan default
        income_acc = self.sale_journal.default_account_id
        if not income_acc:
            income_acc = self.env['account.account'].search([
                ('account_type', 'in', ('income', 'income_other')),
                ('company_ids', 'in', [self.env.company.id])
            ], limit=1)

        orig_taxes = False
        if income_acc:
            orig_taxes = income_acc.tax_ids.ids
            income_acc.write({'tax_ids': [(4, sale_tax.id)]})

        try:
            headers = {
                'X-IBNS-KEY': self.valid_token,
                'Content-Type': 'application/json',
            }
            target_amount = 500000.0
            payload = {
                'partner_id': self.test_outlet.id,
                'amount': target_amount,
                'description': 'Tagihan Lapangan Bersih Tanpa Inflasi Pajak'
            }
            res = self.url_open('/api/v1/sfa/invoices', data=json.dumps(payload), headers=headers)
            self.assertEqual(res.status_code, 201)

            invoice_id = res.json().get('invoice_id')
            self.assertTrue(invoice_id)
            inv = self.env['account.move'].browse(invoice_id)

            # Verifikasi nominal faktur harus tepat sama dengan target_amount (500.000), BUKAN 555.000
            self.assertEqual(inv.amount_total, target_amount, "amount_total faktur tidak boleh terinflasi oleh pajak otomatis")
            self.assertEqual(inv.amount_untaxed, target_amount)
            self.assertEqual(inv.amount_tax, 0.0, "amount_tax harus bernilai 0.0")

            # Verifikasi baris faktur tidak mengikat pajak
            self.assertEqual(len(inv.invoice_line_ids[0].tax_ids), 0, "Line invoice tidak boleh memiliki tax_ids")
        finally:
            if income_acc and orig_taxes is not False:
                income_acc.write({'tax_ids': [(6, 0, orig_taxes)]})

    def test_25_invalid_checkin_coordinates_rejection(self):
        """Memverifikasi bahwa koordinat check-in kunjungan lapangan yang tidak valid ditolak dengan HTTP 400
        alih-alih diabaikan secara diam-diam (silent bypass).
        Menguji string non-angka ('abc'), nilai non-finite ('NaN', 'inf'), boolean, di luar rentang WGS84,
        serta koordinat yang hanya dikirim sebagian (latitude tanpa longitude)."""
        headers = {
            'X-IBNS-KEY': self.valid_token,
            'Content-Type': 'application/json',
        }

        # 1. Koordinat string non-angka ('abc')
        res_abc = self.url_open('/api/v1/sfa/sync-visit',
                                data=json.dumps({'outlet_id': self.test_outlet.id, 'checkin_latitude': 'abc', 'checkin_longitude': 107.6}),
                                headers=headers)
        self.assertEqual(res_abc.status_code, 400, "Latitude 'abc' harus ditolak dengan HTTP 400")
        self.assertIn('coordinates format', res_abc.json().get('message', '').lower())

        # 2. Koordinat 'NaN' dan 'Infinity'
        for bad_coord in ['NaN', 'nan', 'inf', '-inf', 'Infinity', '-Infinity']:
            res_bad = self.url_open('/api/v1/sfa/sync-visit',
                                    data=json.dumps({'outlet_id': self.test_outlet.id, 'checkin_latitude': bad_coord, 'checkin_longitude': 107.6}),
                                    headers=headers)
            self.assertEqual(res_bad.status_code, 400, f"Latitude '{bad_coord}' harus ditolak dengan HTTP 400")

        # 3. Koordinat boolean (True/False)
        res_bool = self.url_open('/api/v1/sfa/sync-visit',
                                 data=json.dumps({'outlet_id': self.test_outlet.id, 'checkin_latitude': True, 'checkin_longitude': 107.6}),
                                 headers=headers)
        self.assertEqual(res_bool.status_code, 400, "Latitude boolean harus ditolak dengan HTTP 400")

        # 4. Koordinat di luar batas WGS84 (Lat > 90 atau Lon > 180)
        res_out_of_bounds = self.url_open('/api/v1/sfa/sync-visit',
                                          data=json.dumps({'outlet_id': self.test_outlet.id, 'checkin_latitude': 95.0, 'checkin_longitude': 107.6}),
                                          headers=headers)
        self.assertEqual(res_out_of_bounds.status_code, 400, "Latitude di luar rentang [-90, 90] harus ditolak dengan HTTP 400")

        # 5. Koordinat parsial (hanya latitude tanpa longitude)
        res_partial = self.url_open('/api/v1/sfa/sync-visit',
                                    data=json.dumps({'outlet_id': self.test_outlet.id, 'checkin_latitude': -6.917}),
                                    headers=headers)
        self.assertEqual(res_partial.status_code, 400, "Koordinat parsial harus ditolak dengan HTTP 400")
        self.assertIn('both checkin_latitude and checkin_longitude', res_partial.json().get('message', '').lower())

        # 6. Tanpa koordinat sama sekali (device tanpa GPS) tetap diizinkan (HTTP 200)
        res_no_coords = self.url_open('/api/v1/sfa/sync-visit',
                                      data=json.dumps({'outlet_id': self.test_outlet.id, 'notes': 'Kunjungan tanpa GPS'}),
                                      headers=headers)
        self.assertEqual(res_no_coords.status_code, 200, "Check-in tanpa koordinat GPS tetap diizinkan (HTTP 200)")

    def test_26_rbac_and_multicompany_idempotency_isolation(self):
        """Memverifikasi penghapusan .sudo() pada pemeriksaan idempotency:
        1. RBAC Enforced: Driver (group_sfa_user) tanpa hak akses billing tidak dapat membocorkan
           detail faktur (invoice_id, invoice_name, amount) dengan mengirimkan idempotency key yang sudah ada;
           harus ditolak dengan HTTP 403 Forbidden.
        2. Multi-Company Isolation: Jika idempotency key sudah digunakan oleh perusahaan lain (Perusahaan C)
           yang tidak diakses oleh pengguna, pencarian idempotency tidak boleh mengembalikan faktur perusahaan C.
           Saat request menabrak constraint unik di database, sistem harus mengembalikan HTTP 409 Conflict
           tanpa membocorkan detail faktur perusahaan C (tidak ada replay HTTP 200 lintas entitas)."""
        # --- Bagian 1: Pengujian Batasan RBAC Driver ---
        # 1. Buat faktur sah oleh manager dengan idempotency key rahasia
        secret_key = f"SECRET-INV-KEY-{uuid.uuid4().hex}"
        manager_headers = {
            'X-IBNS-KEY': self.valid_token,
            'Content-Type': 'application/json',
            'X-Idempotency-Key': secret_key,
        }
        res_create = self.url_open('/api/v1/sfa/invoices',
                                   data=json.dumps({'partner_id': self.test_outlet.id, 'amount': 150000.0}),
                                   headers=manager_headers)
        self.assertEqual(res_create.status_code, 201)
        invoice_id = res_create.json().get('invoice_id')
        self.assertTrue(invoice_id)

        # 2. Driver mencoba probing key faktur tersebut via endpoint /api/v1/sfa/invoices
        self.env['ir.config_parameter'].sudo().set_param('ibns_smart_ops.sfa_api_key', self.driver_token)
        self.env['ir.config_parameter'].sudo().set_param('ibns_smart_ops.sfa_api_user_id', str(self.sfa_driver.id))

        driver_headers = {
            'X-IBNS-KEY': self.driver_token,
            'Content-Type': 'application/json',
            'X-Idempotency-Key': secret_key,
        }
        try:
            res_driver_probe = self.url_open('/api/v1/sfa/invoices',
                                             data=json.dumps({'partner_id': self.test_outlet.id, 'amount': 150000.0}),
                                             headers=driver_headers)
            # Driver HARUS ditolak dengan HTTP 403, BUKAN HTTP 200 (yang akan membocorkan data faktur)
            self.assertEqual(res_driver_probe.status_code, 403, "Driver tanpa izin billing harus mendapat HTTP 403 saat replay key")
            self.assertIn("Insufficient permissions", res_driver_probe.json().get("message", ""))
        finally:
            # Kembalikan konfigurasi token ke manager
            self.env['ir.config_parameter'].sudo().set_param('ibns_smart_ops.sfa_api_key', self.valid_token)
            self.env['ir.config_parameter'].sudo().set_param('ibns_smart_ops.sfa_api_user_id', str(self.sfa_manager.id))

        # --- Bagian 2: Pengujian Multi-Company Data Leak Isolation ---
        # Buat Perusahaan C (terpisah sama sekali dari manager)
        company_c = self.env['res.company'].create({
            'name': 'Perusahaan C Terisolasi',
            'currency_id': self.env.ref('base.IDR').id if self.env.ref('base.IDR', raise_if_not_found=False) else self.env.company.currency_id.id,
        })
        income_acc_c = self.env['account.account'].create({
            'name': 'Pendapatan Perusahaan C',
            'code': f'498{uuid.uuid4().hex[:4]}',
            'account_type': 'income',
            'company_ids': [(6, 0, [company_c.id])],
        })
        receivable_acc_c = self.env['account.account'].create({
            'name': 'Piutang Perusahaan C',
            'code': f'112{uuid.uuid4().hex[:4]}',
            'account_type': 'asset_receivable',
            'company_ids': [(6, 0, [company_c.id])],
            'reconcile': True,
        })
        journal_c = self.env['account.journal'].create({
            'name': 'Penjualan Perusahaan C',
            'type': 'sale',
            'code': f'SC{uuid.uuid4().hex[:2]}',
            'company_id': company_c.id,
            'default_account_id': income_acc_c.id,
        })
        partner_c = self.env['res.partner'].create({
            'name': 'Pelanggan Rahasia Perusahaan C',
            'company_id': company_c.id,
            'property_account_receivable_id': receivable_acc_c.id,
        })

        cross_key = f"CROSS-COMPANY-KEY-{uuid.uuid4().hex}"
        # Buat faktur langsung di Perusahaan C dengan cross_key
        inv_company_c = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': partner_c.id,
            'company_id': company_c.id,
            'journal_id': journal_c.id,
            'invoice_date': self.env.cr.now().date(),
            'invoice_date_due': self.env.cr.now().date(),
            'sfa_idempotency_key': cross_key,
            'invoice_line_ids': [
                (0, 0, {
                    'name': 'Faktur Rahasia Perusahaan C',
                    'quantity': 1.0,
                    'price_unit': 999000.0,
                    'account_id': income_acc_c.id,
                    'tax_ids': [(5, 0, 0)],
                })
            ]
        })
        inv_company_c.action_post()

        # Pastikan sfa_manager TIDAK memiliki akses ke Perusahaan C
        self.assertNotIn(company_c.id, self.sfa_manager.company_ids.ids)

        # Manager di Perusahaan A mengirim request invoice baru dengan cross_key milik Perusahaan C
        conflict_headers = {
            'X-IBNS-KEY': self.valid_token,
            'Content-Type': 'application/json',
            'X-Idempotency-Key': cross_key,
        }
        with mute_logger('odoo.sql_db'):
            res_cross = self.url_open('/api/v1/sfa/invoices',
                                      data=json.dumps({'partner_id': self.test_outlet.id, 'amount': 200000.0}),
                                      headers=conflict_headers)

        # Harus mengembalikan HTTP 409 Conflict karena tabrakan database constraint unik,
        # BUKAN HTTP 200 replay yang membocorkan isi data inv_company_c!
        self.assertEqual(res_cross.status_code, 409, "Cross-company idempotency collision harus mengembalikan HTTP 409")
        self.assertNotIn(inv_company_c.name, str(res_cross.content))
        self.assertNotIn('Faktur Rahasia', str(res_cross.content))

    def test_27_journal_default_account_used_and_no_search_accesserror(self):
        """Memverifikasi bahwa pembuatan faktur SFA menggunakan default_account_id dari jurnal penjualan
        tanpa melakukan pencarian manual pada model account.account:
        1. Menjamin baris faktur terikat pada default_account_id jurnal yang sah (bukan akun acak/income_other).
        2. Menjamin bahwa jika model account.account memicu AccessError saat di-search, endpoint tetap sukses (HTTP 201)."""
        from unittest.mock import patch
        from odoo.exceptions import AccessError

        headers = {
            'X-IBNS-KEY': self.valid_token,
            'Content-Type': 'application/json',
        }
        payload = {
            'partner_id': self.test_outlet.id,
            'amount': 320000.0,
            'description': 'Pengujian Akun Pendapatan Jurnal Sah'
        }

        # Simulasikan jika hak akses pencarian account.account dibatasi (memicu AccessError)
        def mock_account_search(self_model, domain, *args, **kwargs):
            raise AccessError("Simulasi hak akses baca account.account dibatasi")

        with patch.object(type(self.env['account.account']), 'search', side_effect=mock_account_search, autospec=True):
            res = self.url_open('/api/v1/sfa/invoices', data=json.dumps(payload), headers=headers)

        self.assertEqual(res.status_code, 201, "Pembuatan faktur harus berhasil (HTTP 201) tanpa bergantung pada search account.account")
        invoice_id = res.json().get('invoice_id')
        self.assertTrue(invoice_id)

        inv = self.env['account.move'].browse(invoice_id)
        # Baris faktur harus menggunakan default_account_id jurnal penjualan yang sesuai
        if self.sale_journal.default_account_id:
            self.assertEqual(inv.invoice_line_ids[0].account_id.id, self.sale_journal.default_account_id.id)
        self.assertEqual(inv.invoice_line_ids[0].account_id.account_type, 'income')

    def test_28_sync_visit_rejects_archived_outlet(self):
        """Memverifikasi bahwa POST /api/v1/sfa/sync-visit menolak pencatatan kunjungan (check-in)
        ke outlet yang telah dinonaktifkan atau diarsipkan (active=False) dengan HTTP 404."""
        # 1. Buat outlet baru khusus dan nonaktifkan (arsip)
        archived_outlet = self.env['res.partner'].create({
            'name': 'Outlet Sudah Tutup / Diarsipkan',
            'customer_rank': 1,
            'active': False,
        })
        self.assertFalse(archived_outlet.active)
        initial_msg_count = len(archived_outlet.message_ids)

        headers = {
            'X-IBNS-KEY': self.valid_token,
            'Content-Type': 'application/json',
        }
        payload = {
            'outlet_id': archived_outlet.id,
            'notes': 'Sales mencoba check-in ke outlet yang sudah diarsipkan.'
        }

        # 2. Kirim request sync-visit ke outlet yang diarsipkan
        res = self.url_open('/api/v1/sfa/sync-visit', data=json.dumps(payload), headers=headers)

        # Harus ditolak dengan status HTTP 404
        self.assertEqual(res.status_code, 404, "Kunjungan ke outlet non-aktif/diarsipkan harus ditolak dengan HTTP 404")
        self.assertIn('Outlet not found', res.json().get('message', ''))

        # Verifikasi tidak ada pesan tambahan yang terposting pada chatter outlet yang diarsipkan
        self.assertEqual(len(archived_outlet.message_ids), initial_msg_count, "Chatter outlet tidak boleh mencatat kunjungan saat non-aktif")

        # 3. Aktifkan kembali outlet dan pastikan check-in berhasil
        archived_outlet.write({'active': True})
        res_active = self.url_open('/api/v1/sfa/sync-visit', data=json.dumps(payload), headers=headers)
        self.assertEqual(res_active.status_code, 200, "Check-in harus berhasil (HTTP 200) setelah outlet aktif kembali")
        self.assertEqual(len(archived_outlet.message_ids), initial_msg_count + 1, "Chatter outlet harus bertambah 1 pesan kunjungan saat aktif")

    def test_29_res_partner_crud_isolation_driver_vs_manager(self):
        """Memverifikasi hak akses dan record rules pada res.partner antara Driver dan Manager:
        1. SFA Manager memiliki hak penuh (create, read, write, unlink) pada partner di perusahaannya.
        2. SFA Driver hanya memiliki hak baca dan tulis operasional, namun dilarang membuat atau menghapus partner."""
        from odoo.exceptions import AccessError

        # 1. Manager membuat outlet baru
        mgr_partner = self.env['res.partner'].with_user(self.sfa_manager).create({
            'name': 'Outlet Baru Dibuat Manager SFA',
            'customer_rank': 1,
            'company_id': self.env.company.id,
        })
        self.assertTrue(mgr_partner.id)

        # 2. Driver mencoba menghapus outlet -> DITOLAK (AccessError)
        with self.assertRaises(AccessError):
            mgr_partner.with_user(self.sfa_driver).unlink()

        # 3. Driver mencoba membuat outlet baru -> DITOLAK (AccessError)
        with self.assertRaises(AccessError):
            self.env['res.partner'].with_user(self.sfa_driver).create({
                'name': 'Outlet Liar oleh Driver',
                'customer_rank': 1,
            })

        # 4. Driver boleh memperbarui data operasional (misal alamat)
        mgr_partner.with_user(self.sfa_driver).write({
            'street': 'Jl. Riau No. 45'
        })
        self.assertEqual(mgr_partner.street, 'Jl. Riau No. 45')

        # 5. Manager menghapus outlet -> BERHASIL
        mgr_partner.with_user(self.sfa_manager).unlink()
        self.assertFalse(mgr_partner.exists(), "Manager harus sukses menghapus outlet")

    def test_30_driver_name_spoofing_prevention(self):
        """Memverifikasi penghapusan fallback teks driver_name pada rule_stock_picking_sfa_user:
        1. Pengiriman yang memiliki driver_name teks tetapi driver_id = False TIDAK BOLEH terlihat oleh driver,
           mencegah klaim pengiriman tidak sah.
        2. Pengguna Driver A yang mengubah nama profilnya agar sama persis dengan Driver B TETAP TIDAK BISA
           melihat pengiriman Driver B (karena Record Rule sepenuhnya mengunci pada kunci asing driver_id = user.id)."""
        picking_type = self.env['stock.picking.type'].search([('code', '=', 'outgoing')], limit=1)
        if not picking_type:
            return

        # 1. Buat pengiriman unassigned dengan teks driver_name = sfa_driver.name tetapi driver_id = False
        picking_spoof_target = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'partner_id': self.test_outlet.id,
            'location_id': picking_type.default_location_src_id.id,
            'location_dest_id': self.test_outlet.property_stock_customer.id,
            'driver_id': False,
            'driver_name': self.sfa_driver.name,
        })
        self.assertFalse(picking_spoof_target.driver_id)

        # Driver A mencari pengiriman -> TIDAK BOLEH menemukan picking_spoof_target
        driver_a_pickings = self.env['stock.picking'].with_user(self.sfa_driver).search([
            ('id', '=', picking_spoof_target.id)
        ])
        self.assertNotIn(picking_spoof_target, driver_a_pickings,
                         "Driver tidak boleh melihat pengiriman tanpa relasi driver_id yang valid meski driver_name cocok")

        # 2. Buat pengiriman sah milik Driver B
        picking_driver_b = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'partner_id': self.test_outlet.id,
            'location_id': picking_type.default_location_src_id.id,
            'location_dest_id': self.test_outlet.property_stock_customer.id,
            'driver_id': self.sfa_driver_b.id,
            'driver_name': self.sfa_driver_b.name,
        })

        # Simulasikan Driver A mengubah namanya menjadi nama Driver B di profil / Preferences
        original_name_a = self.sfa_driver.name
        try:
            self.sfa_driver.write({'name': self.sfa_driver_b.name})
            self.assertEqual(self.sfa_driver.name, self.sfa_driver_b.name)

            # Driver A yang sudah berganti nama mencari tugas Driver B -> TETAP TERBLOKIR
            driver_a_spoofed_view = self.env['stock.picking'].with_user(self.sfa_driver).search([
                ('id', '=', picking_driver_b.id)
            ])
            self.assertNotIn(picking_driver_b, driver_a_spoofed_view,
                             "Spoofing nama profil user tidak boleh membocorkan pengiriman driver lain")
        finally:
            self.sfa_driver.write({'name': original_name_a})

    def test_31_security_rules_noupdate_flag_verification(self):
        """Memverifikasi bahwa seluruh aturan keamanan ir.rule milik modul ibns_smart_ops
        berada dalam status noupdate=False di ir.model.data sehingga dapat selalu diperbarui
        saat upgrade modul (-u ibns_smart_ops), sedangkan akun integrasi sistem tetap noupdate=True."""
        rules_data = self.env['ir.model.data'].search([
            ('module', '=', 'ibns_smart_ops'),
            ('model', '=', 'ir.rule')
        ])
        self.assertTrue(len(rules_data) >= 6, "Seluruh ir.rule harus terdaftar dalam ir.model.data")
        for r in rules_data:
            self.assertFalse(r.noupdate, f"Rule {r.name} tidak boleh terkunci dengan noupdate=True")

        # Akun integrasi service tetap terlindungi dengan noupdate=True
        user_data = self.env['ir.model.data'].search([
            ('module', '=', 'ibns_smart_ops'),
            ('name', 'in', ['partner_sfa_api_service', 'user_sfa_api_service'])
        ])
        for u in user_data:
            self.assertTrue(u.noupdate, f"Akun bawaan {u.name} harus memiliki noupdate=True")

    def test_32_create_invoice_via_sale_order_and_fully_reconciled(self):
        """Memverifikasi pembuatan faktur penjualan via endpoint /api/v1/sfa/invoices menggunakan sale_order_id:
        1. Validasi penolakan sale_order_id bertipe boolean, non-angka, tidak ditemukan, dan milik partner lain (HTTP 400).
        2. Pembuatan faktur otomatis via sale_order._create_invoices() terikat pada sale.order terkait.
        3. Penyimpanan sfa_idempotency_key dan auto-post faktur (state='posted').
        4. Replay idempotency dengan key yang sama mengembalikan HTTP 200.
        5. Perubahan komputasi delivered_vs_invoiced_status pada stock.picking menjadi 'fully_reconciled'."""
        picking_type = self.env['stock.picking.type'].search([('code', '=', 'outgoing')], limit=1)
        self.assertTrue(picking_type, "Outgoing picking type harus tersedia")

        # 1. Setup produk dan Sales Order
        test_product = self.env['product.product'].create({
            'name': 'Produk SFA SO Invoicing Test',
            'type': 'consu',
            'list_price': 150000.0,
            'taxes_id': False,
        })
        sale_order = self.env['sale.order'].create({
            'partner_id': self.test_outlet.id,
            'order_line': [(0, 0, {
                'product_id': test_product.id,
                'product_uom_qty': 5.0,
                'price_unit': 150000.0,
                'tax_id': False,
            })]
        })
        sale_order.action_confirm()

        # 2. Setup picking berstatus delivered (kuantitas terkirim = 5.0)
        picking = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'partner_id': self.test_outlet.id,
            'location_id': picking_type.default_location_src_id.id,
            'location_dest_id': self.test_outlet.property_stock_customer.id,
            'sale_id': sale_order.id,
            'delivery_status': 'delivered',
            'state': 'done',
        })
        sale_order.order_line.write({'qty_delivered': 5.0})

        # Sebelum faktur dibuat/posted, status harus 'delivered_pending_invoice'
        self.assertEqual(
            picking.delivered_vs_invoiced_status,
            'delivered_pending_invoice',
            "Picking terkirim tanpa faktur harus berstatus 'delivered_pending_invoice'"
        )

        headers = {
            'X-IBNS-KEY': self.valid_token,
            'Content-Type': 'application/json',
        }

        # 3. Validasi: sale_order_id boolean (True/False) ditolak dengan HTTP 400
        res_bool = self.url_open('/api/v1/sfa/invoices',
                                 data=json.dumps({'partner_id': self.test_outlet.id, 'sale_order_id': True}),
                                 headers=headers)
        self.assertEqual(res_bool.status_code, 400, "sale_order_id boolean harus ditolak dengan HTTP 400")

        # 4. Validasi: sale_order_id format salah ditolak dengan HTTP 400
        res_bad_fmt = self.url_open('/api/v1/sfa/invoices',
                                    data=json.dumps({'partner_id': self.test_outlet.id, 'sale_order_id': 'so_satu'}),
                                    headers=headers)
        self.assertEqual(res_bad_fmt.status_code, 400, "sale_order_id non-angka harus ditolak dengan HTTP 400")

        # 5. Validasi: sale_order_id tidak ditemukan ditolak dengan HTTP 400
        res_not_found = self.url_open('/api/v1/sfa/invoices',
                                      data=json.dumps({'partner_id': self.test_outlet.id, 'sale_order_id': 999999}),
                                      headers=headers)
        self.assertEqual(res_not_found.status_code, 400, "sale_order_id tidak ditemukan harus ditolak dengan HTTP 400")

        # 6. Validasi: sale_order_id milik partner lain ditolak dengan HTTP 400
        other_partner = self.env['res.partner'].create({
            'name': 'Partner Lain Non-Apex',
            'customer_rank': 1,
        })
        other_so = self.env['sale.order'].create({
            'partner_id': other_partner.id,
            'order_line': [(0, 0, {
                'product_id': test_product.id,
                'product_uom_qty': 2.0,
                'price_unit': 100000.0,
            })]
        })
        other_so.action_confirm()

        res_wrong_partner = self.url_open('/api/v1/sfa/invoices',
                                          data=json.dumps({'partner_id': self.test_outlet.id, 'sale_order_id': other_so.id}),
                                          headers=headers)
        self.assertEqual(res_wrong_partner.status_code, 400, "SO milik partner lain harus ditolak dengan HTTP 400")
        self.assertIn('does not belong', res_wrong_partner.json().get('message', '').lower())

        # 7. Pembuatan faktur sah via sale_order_id dengan Idempotency Key
        idemp_token = f"so_idemp_key_{uuid.uuid4().hex}"
        inv_headers = {
            'X-IBNS-KEY': self.valid_token,
            'Content-Type': 'application/json',
            'X-Idempotency-Key': idemp_token,
        }
        inv_payload = {
            'partner_id': self.test_outlet.id,
            'sale_order_id': sale_order.id,
        }
        res_inv = self.url_open('/api/v1/sfa/invoices', data=json.dumps(inv_payload), headers=inv_headers)
        self.assertEqual(res_inv.status_code, 201, "Pembuatan faktur via sale_order_id harus mengembalikan HTTP 201")

        data_inv = res_inv.json()
        self.assertEqual(data_inv.get('status'), 'success')
        self.assertFalse(data_inv.get('idempotent_replay'))
        invoice_id = data_inv.get('invoice_id')
        self.assertTrue(invoice_id)

        # 8. Verifikasi faktur di database
        created_invoice = self.env['account.move'].browse(invoice_id)
        self.assertTrue(created_invoice.exists())
        self.assertEqual(created_invoice.state, 'posted', "Faktur dari SO harus berstatus posted")
        self.assertEqual(created_invoice.sfa_idempotency_key, idemp_token, "Idempotency key harus tersimpan di faktur")
        self.assertIn(created_invoice, sale_order.invoice_ids, "Faktur harus terhubung pada invoice_ids Sales Order")
        self.assertEqual(created_invoice.amount_total, 750000.0, "Total faktur harus Rp 750.000 (5 x Rp 150.000)")

        # 9. Verifikasi perubahan status delivered_vs_invoiced_status menjadi 'fully_reconciled'
        self.assertEqual(
            picking.delivered_vs_invoiced_status,
            'fully_reconciled',
            "Status audit Delivered vs Invoiced harus berubah menjadi 'fully_reconciled' setelah faktur terbit dan terposting"
        )

        # 10. Verifikasi Idempotent Replay pada faktur SO
        res_replay = self.url_open('/api/v1/sfa/invoices', data=json.dumps(inv_payload), headers=inv_headers)
        self.assertEqual(res_replay.status_code, 200, "Replay idempotency faktur SO harus mengembalikan HTTP 200")
        self.assertTrue(res_replay.json().get('idempotent_replay'))
        self.assertEqual(res_replay.json().get('invoice_id'), invoice_id)

    def test_33_x_company_id_header_validation(self):
        """Memverifikasi validasi ketat header 'X-Company-Id' pada _get_user_env():
        1. X-Company-Id format salah ("abc", "", "-1", "1.5") menghasilkan HTTP 400.
        2. X-Company-Id di luar user.company_ids (company lain & ID tidak ada) menghasilkan HTTP 403.
        3. X-Company-Id valid milik user tetap menghasilkan HTTP 200.
        """
        # 1. Format salah: "abc", "", "-1", "1.5" -> HTTP 400
        invalid_formats = ["abc", "", "-1", "1.5"]
        for bad_val in invalid_formats:
            headers = {
                'X-IBNS-KEY': self.valid_token,
                'X-Company-Id': bad_val,
            }
            res = self.url_open('/api/v1/sfa/outlets', headers=headers)
            self.assertEqual(res.status_code, 400, f"X-Company-Id '{bad_val}' harus ditolak dengan HTTP 400")
            res_json = res.json()
            self.assertEqual(res_json.get('status'), 'error')
            self.assertEqual(res_json.get('message'), "Invalid X-Company-Id header: must be a positive integer")

        # 2. X-Company-Id di luar user.company_ids (company lain & ID tidak ada) -> HTTP 403
        unauthorized_company = self.env['res.company'].create({
            'name': 'Perusahaan Tanpa Izin SFA',
            'currency_id': self.env.company.currency_id.id,
        })
        headers_other = {
            'X-IBNS-KEY': self.valid_token,
            'X-Company-Id': str(unauthorized_company.id),
        }
        res_other = self.url_open('/api/v1/sfa/outlets', headers=headers_other)
        self.assertEqual(res_other.status_code, 403, "X-Company-Id milik company lain harus ditolak dengan HTTP 403")
        res_other_json = res_other.json()
        self.assertEqual(res_other_json.get('status'), 'error')
        self.assertEqual(res_other_json.get('message'), "Forbidden: You do not have access to the requested company")

        # ID tidak ada di database
        headers_nonexistent = {
            'X-IBNS-KEY': self.valid_token,
            'X-Company-Id': '999999',
        }
        res_nonexistent = self.url_open('/api/v1/sfa/outlets', headers=headers_nonexistent)
        self.assertEqual(res_nonexistent.status_code, 403, "X-Company-Id non-existent ID harus ditolak dengan HTTP 403")
        res_nonexistent_json = res_nonexistent.json()
        self.assertEqual(res_nonexistent_json.get('status'), 'error')
        self.assertEqual(res_nonexistent_json.get('message'), "Forbidden: You do not have access to the requested company")

        # 3. X-Company-Id valid milik user -> HTTP 200
        headers_valid = {
            'X-IBNS-KEY': self.valid_token,
            'X-Company-Id': str(self.env.company.id),
        }
        res_valid = self.url_open('/api/v1/sfa/outlets', headers=headers_valid)
        self.assertEqual(res_valid.status_code, 200, "X-Company-Id valid milik user harus diterima dengan HTTP 200")
        self.assertEqual(res_valid.json().get('status'), 'success')

        # Verifikasi konsistensi validasi header pada endpoint POST sync-visit dan invoices
        res_post_bad = self.url_open(
            '/api/v1/sfa/sync-visit',
            data=json.dumps({'outlet_id': self.test_outlet.id}),
            headers={'X-IBNS-KEY': self.valid_token, 'Content-Type': 'application/json', 'X-Company-Id': 'abc'}
        )
        self.assertEqual(res_post_bad.status_code, 400)
        self.assertEqual(res_post_bad.json().get('message'), "Invalid X-Company-Id header: must be a positive integer")

        res_inv_forbidden = self.url_open(
            '/api/v1/sfa/invoices',
            data=json.dumps({'partner_id': self.test_outlet.id, 'amount': 100000.0}),
            headers={'X-IBNS-KEY': self.valid_token, 'Content-Type': 'application/json', 'X-Company-Id': '999999'}
        )
        self.assertEqual(res_inv_forbidden.status_code, 403)
        self.assertEqual(res_inv_forbidden.json().get('message'), "Forbidden: You do not have access to the requested company")

    def test_34_sale_order_multi_company_and_shared_partner_invoicing(self):
        """Memverifikasi penanganan mode sale_order_id pada multi-company:
        1. Partner shared (company_id=False) dengan sale.order milik Company B berhasil di-invoice
           tanpa error Incompatible companies dan invoice yang terbentuk memakai company_id == company_b.id.
        2. User tanpa akses ke company milik sale.order ditolak dengan HTTP 403.
        3. Penolakan ketika SO menghasilkan multiple invoices (len > 1) dengan HTTP 400 dan rollback.
        """
        # 1. Setup Perusahaan B
        company_b = self.env['res.company'].create({
            'name': 'IBNS Distribusi Cabang B SO',
            'currency_id': self.env.ref('base.IDR').id if self.env.ref('base.IDR', raise_if_not_found=False) else self.env.company.currency_id.id,
        })
        income_account_b = self.env['account.account'].create({
            'name': 'Pendapatan Cabang B SO',
            'code': f'498{uuid.uuid4().hex[:4]}',
            'account_type': 'income',
            'company_ids': [(6, 0, [company_b.id])],
        })
        receivable_account_b = self.env['account.account'].create({
            'name': 'Piutang Cabang B SO',
            'code': f'112{uuid.uuid4().hex[:4]}',
            'account_type': 'asset_receivable',
            'company_ids': [(6, 0, [company_b.id])],
            'reconcile': True,
        })
        journal_b = self.env['account.journal'].create({
            'name': 'Penjualan Cabang B SO',
            'type': 'sale',
            'code': f'S{uuid.uuid4().hex[:3]}',
            'company_id': company_b.id,
            'default_account_id': income_account_b.id,
        })

        # Berikan hak akses Perusahaan B ke sfa_manager
        self.sfa_manager.write({
            'company_ids': [(4, company_b.id)]
        })

        # Partner bersifat shared (company_id=False)
        shared_partner = self.env['res.partner'].create({
            'name': 'Pelanggan Shared Nasional',
            'company_id': False,
            'customer_rank': 1,
            'property_account_receivable_id': receivable_account_b.id,
        })

        test_product = self.env['product.product'].create({
            'name': 'Produk Multi-Company SO',
            'type': 'consu',
            'list_price': 200000.0,
            'taxes_id': False,
        })

        # Sale Order milik Perusahaan B
        so_b = self.env['sale.order'].with_company(company_b).create({
            'partner_id': shared_partner.id,
            'company_id': company_b.id,
            'order_line': [(0, 0, {
                'product_id': test_product.id,
                'product_uom_qty': 2.0,
                'price_unit': 200000.0,
                'tax_id': False,
            })],
        })
        so_b.action_confirm()

        headers = {
            'X-IBNS-KEY': self.valid_token,
            'Content-Type': 'application/json',
        }
        payload_b = {
            'partner_id': shared_partner.id,
            'sale_order_id': so_b.id,
        }

        # Request default tanpa header X-Company-Id (active user company adalah Company A / main company)
        res_b = self.url_open('/api/v1/sfa/invoices', data=json.dumps(payload_b), headers=headers)
        self.assertEqual(res_b.status_code, 201, "Shared partner dengan SO milik Perusahaan B harus berhasil dibuatkan faktur")
        data_b = res_b.json()
        self.assertEqual(data_b.get('status'), 'success')
        invoice_id_b = data_b.get('invoice_id')
        self.assertTrue(invoice_id_b)

        invoice_b = self.env['account.move'].browse(invoice_id_b)
        self.assertTrue(invoice_b.exists())
        self.assertEqual(invoice_b.company_id.id, company_b.id, "Invoice yang terbentuk harus memakai company_id == company_b.id")
        self.assertEqual(invoice_b.journal_id.id, journal_b.id, "Invoice harus memakai jurnal Perusahaan B")
        self.assertEqual(invoice_b.state, 'posted', "Invoice harus berstatus posted")

        # 2. User tanpa akses ke company milik sale.order ditolak dengan HTTP 403
        company_c = self.env['res.company'].create({
            'name': 'Perusahaan C Tanpa Akses SO',
            'currency_id': self.env.company.currency_id.id,
        })
        self.assertNotIn(company_c.id, self.sfa_manager.company_ids.ids)

        so_c = self.env['sale.order'].sudo().with_company(company_c).create({
            'partner_id': shared_partner.id,
            'company_id': company_c.id,
            'order_line': [(0, 0, {
                'product_id': test_product.id,
                'product_uom_qty': 1.0,
                'price_unit': 100000.0,
                'tax_id': False,
            })],
        })
        so_c.sudo().action_confirm()

        payload_c = {
            'partner_id': shared_partner.id,
            'sale_order_id': so_c.id,
        }
        res_c = self.url_open('/api/v1/sfa/invoices', data=json.dumps(payload_c), headers=headers)
        self.assertEqual(res_c.status_code, 403, "User tanpa akses ke company milik SO harus ditolak HTTP 403")
        res_c_json = res_c.json()
        self.assertEqual(res_c_json.get('status'), 'error')
        self.assertIn("Forbidden", res_c_json.get('message', ''))

        # 3. Penolakan ketika SO menghasilkan multiple invoices (len > 1) dengan HTTP 400
        from unittest.mock import patch
        dummy_inv_1 = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.test_outlet.id,
            'company_id': self.env.company.id,
            'journal_id': self.sale_journal.id,
            'invoice_line_ids': [(0, 0, {'name': 'Split Line 1', 'price_unit': 150000.0})],
        })
        dummy_inv_2 = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.test_outlet.id,
            'company_id': self.env.company.id,
            'journal_id': self.sale_journal.id,
            'invoice_line_ids': [(0, 0, {'name': 'Split Line 2', 'price_unit': 150000.0})],
        })
        with patch.object(type(self.env['sale.order']), '_create_invoices', return_value=dummy_inv_1 + dummy_inv_2):
            res_split = self.url_open('/api/v1/sfa/invoices', data=json.dumps(payload_b), headers=headers)
            self.assertEqual(res_split.status_code, 400, "Split invoices harus ditolak HTTP 400")
            res_split_json = res_split.json()
            self.assertEqual(res_split_json.get('status'), 'error')
            self.assertEqual(
                res_split_json.get('message'),
                "Sale order generated multiple invoices; single-invoice SFA endpoint cannot process split invoices"
            )

    def test_35_idempotency_payload_mismatch_and_access_error_masking(self):
        """Memverifikasi:
        1. Reuse X-Idempotency-Key dengan payload identik tetap mengembalikan HTTP 200 (idempotent_replay: True).
        2. Reuse X-Idempotency-Key yang sama dengan amount berbeda atau partner_id berbeda ditolak dengan HTTP 409.
        3. Respons HTTP 403 dari pelanggaran akses tidak mengandung string teknis ORM seperti
           'res.partner', 'sale.order', 'account.move', atau 'record rules'.
        """
        # 1. Setup Idempotency Key dan Payload Awal
        idem_key = f"idem_test35_{uuid.uuid4().hex}"
        headers = {
            'X-IBNS-KEY': self.valid_token,
            'Content-Type': 'application/json',
            'X-Idempotency-Key': idem_key,
        }
        payload_orig = {
            'partner_id': self.test_outlet.id,
            'amount': 450000.0,
            'description': 'Idempotency Payload Validation Test',
        }

        # Request 1 -> 201 Created
        res1 = self.url_open('/api/v1/sfa/invoices', data=json.dumps(payload_orig), headers=headers)
        self.assertEqual(res1.status_code, 201, "Faktur pertama harus berhasil terbuat (HTTP 201)")
        inv1_id = res1.json().get('invoice_id')
        self.assertTrue(inv1_id)
        self.assertFalse(res1.json().get('idempotent_replay'))

        # Request 2 (Payload identik) -> 200 OK dengan idempotent_replay: True
        res2 = self.url_open('/api/v1/sfa/invoices', data=json.dumps(payload_orig), headers=headers)
        self.assertEqual(res2.status_code, 200, "Replay payload identik harus mengembalikan HTTP 200")
        self.assertTrue(res2.json().get('idempotent_replay'))
        self.assertEqual(res2.json().get('invoice_id'), inv1_id)

        # 2. Reuse Idempotency Key dengan payload berbeda -> HTTP 409 Mismatch
        # 2a. Amount berbeda (500.000 vs 450.000)
        payload_diff_amount = {
            'partner_id': self.test_outlet.id,
            'amount': 500000.0,
            'description': 'Idempotency Payload Validation Test',
        }
        res_diff_amt = self.url_open('/api/v1/sfa/invoices', data=json.dumps(payload_diff_amount), headers=headers)
        self.assertEqual(res_diff_amt.status_code, 409, "Replay dengan amount berbeda harus ditolak dengan HTTP 409")
        self.assertEqual(
            res_diff_amt.json().get('message'),
            "Idempotency key already used with different request parameters"
        )

        # 2b. Partner berbeda
        other_partner = self.env['res.partner'].create({
            'name': 'Partner Mismatch Test 35',
            'customer_rank': 1,
        })
        payload_diff_partner = {
            'partner_id': other_partner.id,
            'amount': 450000.0,
            'description': 'Idempotency Payload Validation Test',
        }
        res_diff_partner = self.url_open('/api/v1/sfa/invoices', data=json.dumps(payload_diff_partner), headers=headers)
        self.assertEqual(res_diff_partner.status_code, 409, "Replay dengan partner berbeda harus ditolak dengan HTTP 409")
        self.assertEqual(
            res_diff_partner.json().get('message'),
            "Idempotency key already used with different request parameters"
        )

        # 3. Masking Pesan AccessError: Tidak mengandung string teknis ORM internal
        # 3a. Pengujian menggunakan akun Driver (tanpa izin pembuatan faktur / account.move)
        self.env['ir.config_parameter'].sudo().set_param('ibns_smart_ops.sfa_api_key', self.driver_token)
        self.env['ir.config_parameter'].sudo().set_param('ibns_smart_ops.sfa_api_user_id', str(self.sfa_driver.id))
        driver_headers = {
            'X-IBNS-KEY': self.driver_token,
            'Content-Type': 'application/json',
        }
        res_driver_inv = self.url_open(
            '/api/v1/sfa/invoices',
            data=json.dumps({'partner_id': self.test_outlet.id, 'amount': 100000.0}),
            headers=driver_headers
        )
        self.assertEqual(res_driver_inv.status_code, 403, "Driver harus ditolak dengan HTTP 403")
        msg_driver = res_driver_inv.json().get('message', '')
        self.assertEqual(msg_driver, "Forbidden: Insufficient permissions to create or post invoices")
        for technical_str in ['res.partner', 'sale.order', 'account.move', 'record rules']:
            self.assertNotIn(technical_str, msg_driver.lower(), f"Pesan error tidak boleh membocorkan string teknis '{technical_str}'")

        # 3b. Pengujian simulasi AccessError saat message_post di sync_visit_data
        self.env['ir.config_parameter'].sudo().set_param('ibns_smart_ops.sfa_api_key', self.valid_token)
        self.env['ir.config_parameter'].sudo().set_param('ibns_smart_ops.sfa_api_user_id', str(self.sfa_manager.id))
        from unittest.mock import patch
        with patch.object(type(self.env['res.partner']), 'message_post', side_effect=AccessError("Document model res.partner restricted by record rules")):
            res_visit_err = self.url_open(
                '/api/v1/sfa/sync-visit',
                data=json.dumps({'outlet_id': self.test_outlet.id, 'notes': 'Check-in catatan kunjungan'}),
                headers={'X-IBNS-KEY': self.valid_token, 'Content-Type': 'application/json'}
            )
            self.assertEqual(res_visit_err.status_code, 403)
            msg_visit = res_visit_err.json().get('message', '')
            self.assertEqual(msg_visit, "Forbidden: Insufficient permissions to log field visit")
            for technical_str in ['res.partner', 'sale.order', 'account.move', 'record rules']:
                self.assertNotIn(technical_str, msg_visit.lower(), f"Pesan error tidak boleh membocorkan string teknis '{technical_str}'")

    def test_36_non_sfa_user_api_key_rejected_with_403(self):
        """Memverifikasi otorisasi grup SFA pada _authenticate_request():
        1. User internal biasa (base.group_user saja, bukan anggota ibns_smart_ops.group_sfa_user,
           ibns_smart_ops.group_sfa_manager, maupun base.group_system) dengan Native API Key valid
           ditolak dengan HTTP 403 pada:
           - GET /api/v1/sfa/outlets
           - POST /api/v1/sfa/sync-visit
           - POST /api/v1/sfa/invoices
        2. Pesan respons 403 tidak membocorkan nama grup atau modul internal (group_sfa_user,
           group_sfa_manager, ibns_smart_ops).
        3. User anggota ibns_smart_ops.group_sfa_user (driver) dan user base.group_system (administrator)
           tetap lolos autentikasi dan menerima HTTP 200 pada GET /api/v1/sfa/outlets.
        """
        # 1. Buat user internal biasa (hanya base.group_user)
        non_sfa_user = self.env['res.users'].create({
            'name': 'Non SFA Regular User',
            'login': f'non_sfa_user_{uuid.uuid4().hex[:8]}',
            'email': 'non_sfa@ibns.local',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })

        # Pastikan user bukan anggota grup SFA maupun Administrator
        self.assertFalse(non_sfa_user.has_group('ibns_smart_ops.group_sfa_user'))
        self.assertFalse(non_sfa_user.has_group('ibns_smart_ops.group_sfa_manager'))
        self.assertFalse(non_sfa_user.has_group('base.group_system'))

        # Buat Native API Key (res.users.apikeys) untuk non_sfa_user
        non_sfa_api_key = self.env['res.users.apikeys'].with_user(non_sfa_user).sudo()._generate(
            scope='rpc', name='non_sfa_key', expiration_date=None
        )

        headers_get = {'X-IBNS-KEY': non_sfa_api_key}
        headers_post = {
            'X-IBNS-KEY': non_sfa_api_key,
            'Content-Type': 'application/json',
        }

        # 1a. GET /api/v1/sfa/outlets -> Ditolak HTTP 403
        res_outlets = self.url_open('/api/v1/sfa/outlets', headers=headers_get)
        self.assertEqual(res_outlets.status_code, 403, "Non-SFA user harus ditolak dengan HTTP 403 pada GET outlets")
        data_outlets = res_outlets.json()
        self.assertEqual(data_outlets.get('status'), 'error')
        self.assertEqual(data_outlets.get('message'), "Forbidden: User does not have SFA API access")

        # 1b. POST /api/v1/sfa/sync-visit -> Ditolak HTTP 403
        res_visit = self.url_open(
            '/api/v1/sfa/sync-visit',
            data=json.dumps({'outlet_id': self.test_outlet.id, 'notes': 'Field visit attempt by non-SFA user'}),
            headers=headers_post,
        )
        self.assertEqual(res_visit.status_code, 403, "Non-SFA user harus ditolak dengan HTTP 403 pada POST sync-visit")
        data_visit = res_visit.json()
        self.assertEqual(data_visit.get('status'), 'error')
        self.assertEqual(data_visit.get('message'), "Forbidden: User does not have SFA API access")

        # 1c. POST /api/v1/sfa/invoices -> Ditolak HTTP 403
        res_inv = self.url_open(
            '/api/v1/sfa/invoices',
            data=json.dumps({'partner_id': self.test_outlet.id, 'amount': 150000.0}),
            headers=headers_post,
        )
        self.assertEqual(res_inv.status_code, 403, "Non-SFA user harus ditolak dengan HTTP 403 pada POST invoices")
        data_inv = res_inv.json()
        self.assertEqual(data_inv.get('status'), 'error')
        self.assertEqual(data_inv.get('message'), "Forbidden: User does not have SFA API access")

        # 2. Verifikasi pesan respons 403 tidak membocorkan nama grup atau modul internal
        for res in [res_outlets, res_visit, res_inv]:
            msg = res.json().get('message', '')
            for forbidden_str in ['group_sfa_user', 'group_sfa_manager', 'ibns_smart_ops']:
                self.assertNotIn(
                    forbidden_str,
                    msg.lower(),
                    f"Pesan respons 403 tidak boleh membocorkan string '{forbidden_str}'"
                )

        # 3. User anggota ibns_smart_ops.group_sfa_user (driver) tetap lolos HTTP 200 pada GET outlets
        driver_api_key = self.env['res.users.apikeys'].with_user(self.sfa_driver).sudo()._generate(
            scope='rpc', name='driver_native_key', expiration_date=None
        )
        res_driver = self.url_open('/api/v1/sfa/outlets', headers={'X-IBNS-KEY': driver_api_key})
        self.assertEqual(res_driver.status_code, 200, "Driver anggota SFA user harus lolos autentikasi (HTTP 200)")
        self.assertEqual(res_driver.json().get('status'), 'success')
        self.assertEqual(res_driver.json().get('authenticated_user'), self.sfa_driver.name)

        # 4. User base.group_system (administrator) tetap lolos HTTP 200 pada GET outlets
        admin_user = self.env.ref('base.user_admin')
        admin_api_key = self.env['res.users.apikeys'].with_user(admin_user).sudo()._generate(
            scope='rpc', name='admin_native_key', expiration_date=None
        )
        res_admin = self.url_open('/api/v1/sfa/outlets', headers={'X-IBNS-KEY': admin_api_key})
        self.assertEqual(res_admin.status_code, 200, "Administrator (base.group_system) harus lolos autentikasi (HTTP 200)")
        self.assertEqual(res_admin.json().get('status'), 'success')
        self.assertEqual(res_admin.json().get('authenticated_user'), admin_user.name)

    def test_37_system_key_fallback_to_service_user(self):
        """Memverifikasi perbaikan bug AccessError pada fallback autentikasi service_user:
        1. Konfigurasi ir.config_parameter dengan sfa_api_key terisi sebuah kunci valid,
           sedangkan sfa_api_user_id kosong/dihapus.
        2. Request stateless ke GET /api/v1/sfa/outlets dengan header X-IBNS-KEY berisi
           kunci tersebut berhasil mendapatkan HTTP 200, status == 'success',
           dan authenticated_user == 'SFA API System Service'.
        3. Jika ibns_smart_ops.user_sfa_api_service dinonaktifkan sementara (active = False),
           request yang sama ditolak bersih dengan HTTP 401.
        """
        fallback_key = f"system_fallback_key_{uuid.uuid4().hex}"
        self.env['ir.config_parameter'].sudo().set_param('ibns_smart_ops.sfa_api_key', fallback_key)
        self.env['ir.config_parameter'].sudo().set_param('ibns_smart_ops.sfa_api_user_id', False)

        service_user = self.env.ref('ibns_smart_ops.user_sfa_api_service')
        self.assertTrue(service_user.exists(), "User service SFA API harus ada")
        self.assertTrue(service_user.active, "User service SFA API harus aktif secara default")

        headers = {'X-IBNS-KEY': fallback_key}

        try:
            # 1. Request saat user_sfa_api_service aktif -> Berhasil HTTP 200
            res = self.url_open('/api/v1/sfa/outlets', headers=headers)
            self.assertEqual(res.status_code, 200, "Fallback ke service_user harus menghasilkan HTTP 200")
            payload = res.json()
            self.assertEqual(payload.get('status'), 'success')
            self.assertEqual(payload.get('authenticated_user'), 'SFA API System Service')

            # 2. Nonaktifkan service_user sementara -> Ditolak bersih dengan HTTP 401
            service_user.sudo().write({'active': False})
            res_inactive = self.url_open('/api/v1/sfa/outlets', headers=headers)
            self.assertEqual(res_inactive.status_code, 401, "Service user nonaktif harus ditolak dengan HTTP 401")
            payload_inactive = res_inactive.json()
            self.assertEqual(payload_inactive.get('status'), 'error')
            self.assertEqual(payload_inactive.get('message'), "Unauthorized: Invalid or inactive API key")
        finally:
            # Kembalikan state service_user dan parameter konfigurasi sistem
            service_user.sudo().write({'active': True})
            self.env['ir.config_parameter'].sudo().set_param('ibns_smart_ops.sfa_api_key', self.valid_token)
            self.env['ir.config_parameter'].sudo().set_param('ibns_smart_ops.sfa_api_user_id', str(self.sfa_manager.id))






