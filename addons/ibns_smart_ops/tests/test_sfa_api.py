# -*- coding: utf-8 -*-
import json
import time
import uuid
import psycopg2
from odoo.exceptions import ValidationError
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

        # 3. Query dengan konteks sfa_driver: Hanya picking_a yang boleh terlihat
        driver_visible_pickings = self.env['stock.picking'].with_user(self.sfa_driver).search([
            ('id', 'in', [picking_a.id, picking_b.id])
        ])
        self.assertIn(picking_a, driver_visible_pickings, "Driver harus melihat order tugasnya sendiri")
        self.assertNotIn(picking_b, driver_visible_pickings, "Record rule harus menyembunyikan tugas driver lain")

        # 4. Query dengan konteks Manager: Kedua picking harus terlihat
        manager_visible_pickings = self.env['stock.picking'].with_user(self.sfa_manager).search([
            ('id', 'in', [picking_a.id, picking_b.id])
        ])
        self.assertIn(picking_a, manager_visible_pickings)
        self.assertIn(picking_b, manager_visible_pickings)

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
