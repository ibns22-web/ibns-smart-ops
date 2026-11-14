# -*- coding: utf-8 -*-
import hmac
import json
import logging
import math
import os
import time
from collections import defaultdict

try:
    from markupsafe import Markup, escape
except ImportError:
    from odoo.tools import Markup, html_escape as escape

from odoo import http, fields
from odoo.http import request, Response
from odoo.exceptions import AccessDenied, AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)

# -------------------------------------------------------------------------
# RATE GUARD (Sliding Window In-Memory Rate Limiter)
# -------------------------------------------------------------------------
RATE_LIMIT_WINDOW = 60  # Jendela waktu dalam detik
RATE_LIMIT_MAX_REQUESTS = 60  # Maksimum request per jendela waktu
_rate_limit_records = defaultdict(list)


def _calculate_haversine_distance(lat1, lon1, lat2, lon2):
    """Menghitung jarak dalam satuan meter antara dua koordinat GPS menggunakan formula Haversine."""
    R = 6371000.0  # Radius bumi dalam meter
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi / 2.0) ** 2.0 +
         math.cos(phi1) * math.cos(phi2) *
         math.sin(delta_lambda / 2.0) ** 2.0)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return round(R * c, 1)


class SfaApiController(http.Controller):

    def _check_rate_guard(self):
        """Memeriksa ambang batas frekuensi request (Rate Guard).
        Membersihkan timestamp yang sudah kedaluwarsa (> 60 detik) untuk mencegah memory leak.
        Mengembalikan Response HTTP 429 jika melebihi batas (60 req/menit), atau None jika diizinkan.
        """
        now = time.time()
        window_start = now - RATE_LIMIT_WINDOW

        # Bersihkan timestamp kedaluwarsa (> 60 detik) dari seluruh record agar tidak terjadi memory leak
        stale_identifiers = []
        for ident, record_times in list(_rate_limit_records.items()):
            valid_times = [t for t in record_times if t > window_start]
            if valid_times:
                _rate_limit_records[ident] = valid_times
            else:
                stale_identifiers.append(ident)
        for ident in stale_identifiers:
            _rate_limit_records.pop(ident, None)

        # Identifikasi klien berdasarkan IP
        client_ip = request.httprequest.remote_addr or 'unknown_ip'

        valid_timestamps = _rate_limit_records[client_ip]

        if len(valid_timestamps) >= RATE_LIMIT_MAX_REQUESTS:
            oldest = valid_timestamps[0]
            retry_after = max(1, int(RATE_LIMIT_WINDOW - (now - oldest)) + 1)
            _logger.warning("Rate limit exceeded for IP %s (%d requests in 60s)", client_ip, len(valid_timestamps))
            return Response(
                json.dumps({
                    "status": "error",
                    "message": f"Too Many Requests: Rate limit of {RATE_LIMIT_MAX_REQUESTS} req/min exceeded. Retry after {retry_after}s."
                }),
                content_type="application/json;charset=utf-8",
                status=429,
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(RATE_LIMIT_MAX_REQUESTS),
                    "X-RateLimit-Remaining": "0"
                }
            )

        _rate_limit_records[client_ip].append(now)
        return None

    def _sanitize_record(self, data):
        """Mengubah representasi nilai False internal Odoo dan koordinat kosong menjadi None (null di JSON).
        Koordinat bernilai 0.0, False, atau None dikonversi menjadi null agar tidak disalahartikan
        sebagai koordinat valid (Null Island).
        Mencegah error parsing tipe data pada klien mobile akibat field kosong terkirim sebagai boolean false.
        """
        if isinstance(data, dict):
            res = {}
            for k, v in data.items():
                if k in ('partner_latitude', 'partner_longitude') and (v is False or v is None or v == 0.0):
                    res[k] = None
                else:
                    res[k] = self._sanitize_record(v)
            return res
        elif isinstance(data, list):
            return [self._sanitize_record(v) for v in data]
        elif data is False:
            return None
        return data

    def _parse_json_payload(self):
        """Mem-parsing body request JSON secara aman dan eksplisit.
        Mengembalikan tuple (payload_dict, error_response).
        """
        raw_data = request.httprequest.data
        if not raw_data:
            return {}, None
        try:
            body_str = raw_data.decode('utf-8')
            if not body_str.strip():
                return {}, None
            payload = json.loads(body_str)
            if not isinstance(payload, dict):
                return None, Response(
                    json.dumps({"status": "error", "message": "Request body must be a valid JSON object"}),
                    content_type="application/json;charset=utf-8",
                    status=400
                )
            return payload, None
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            _logger.warning("Malformed JSON received in SFA API request: %s", str(e))
            return None, Response(
                json.dumps({"status": "error", "message": "Malformed JSON in request body"}),
                content_type="application/json;charset=utf-8",
                status=400
            )

    def _authenticate_request(self):
        """Autentikasi request via Odoo User API Key atau System Integration Key.
        Mengembalikan tuple (res.users record, None) jika valid,
        atau (None, Response) jika token tidak valid/absen.
        Semua error encoding/decoding pada header autentikasi ditangkap secara aman (HTTP 401).
        """
        try:
            token = request.httprequest.headers.get('X-IBNS-KEY')
            if not token:
                auth_header = request.httprequest.headers.get('Authorization', '')
                if auth_header.startswith('Bearer '):
                    token = auth_header[7:].strip()
                else:
                    token = auth_header.strip()

            if not token:
                _logger.warning("SFA API request rejected: Missing API key header.")
                return None, Response(
                    json.dumps({"status": "error", "message": "Unauthorized: Missing API key"}),
                    content_type="application/json;charset=utf-8",
                    status=401
                )

            authenticated_user = None

            # 1. Verifikasi via Odoo Native API Keys (res.users.apikeys)
            try:
                if 'res.users.apikeys' in request.env:
                    uid = request.env['res.users.apikeys'].sudo()._check_credentials(scope='rpc', key=token)
                    if uid:
                        user_candidate = request.env['res.users'].sudo().browse(uid)
                        if user_candidate.exists() and user_candidate.active:
                            authenticated_user = user_candidate
            except (AccessDenied, AccessError) as e:
                _logger.warning("Odoo native API key verification failed: %s", str(e))
                authenticated_user = None
            except Exception as e:
                _logger.warning("Error during native API credentials verification: %s", str(e))
                authenticated_user = None

            # 2. Fallback: Verifikasi System Integration Key (disimpan di ir.config_parameter / .env)
            if not authenticated_user:
                configured_key = request.env['ir.config_parameter'].sudo().get_param('ibns_smart_ops.sfa_api_key')
                env_key = os.environ.get('SFA_API_SECRET_KEY')

                # Konversi selalu kedua sisi ke UTF-8 bytes untuk mencegah TypeError pada perbandingan non-ASCII
                try:
                    token_bytes = token.encode('utf-8') if isinstance(token, str) else (token or b'')
                except (UnicodeEncodeError, UnicodeDecodeError):
                    _logger.warning("Token decoding/encoding error in authentication header.")
                    return None, Response(
                        json.dumps({"status": "error", "message": "Unauthorized: Invalid API key format"}),
                        content_type="application/json;charset=utf-8",
                        status=401
                    )

                is_valid_sys_key = False
                if configured_key and isinstance(configured_key, str):
                    try:
                        is_valid_sys_key = is_valid_sys_key or hmac.compare_digest(token_bytes, configured_key.encode('utf-8'))
                    except Exception as e:
                        _logger.warning("Error comparing configured_key digest: %s", str(e))
                if env_key and isinstance(env_key, str):
                    try:
                        is_valid_sys_key = is_valid_sys_key or hmac.compare_digest(token_bytes, env_key.encode('utf-8'))
                    except Exception as e:
                        _logger.warning("Error comparing env_key digest: %s", str(e))

                if is_valid_sys_key:
                    user_id_param = request.env['ir.config_parameter'].sudo().get_param('ibns_smart_ops.sfa_api_user_id')
                    if user_id_param and user_id_param.isdigit():
                        sys_user = request.env['res.users'].sudo().browse(int(user_id_param))
                        if sys_user.exists() and sys_user.active:
                            authenticated_user = sys_user

                    if not authenticated_user:
                        service_user = request.env.ref('ibns_smart_ops.user_sfa_api_service', raise_if_not_found=False)
                        if service_user and service_user.exists() and service_user.active:
                            authenticated_user = service_user

                    if not authenticated_user:
                        _logger.warning("SFA API request rejected: Valid key but service user not found or inactive.")
                        return None, Response(
                            json.dumps({"status": "error", "message": "Unauthorized: Invalid or inactive API key"}),
                            content_type="application/json;charset=utf-8",
                            status=401
                        )

            if not authenticated_user or not authenticated_user.exists() or not authenticated_user.active:
                _logger.warning("SFA API request rejected: Invalid or inactive API key.")
                return None, Response(
                    json.dumps({"status": "error", "message": "Unauthorized: Invalid or inactive API key"}),
                    content_type="application/json;charset=utf-8",
                    status=401
                )

            return authenticated_user, None
        except Exception as e:
            _logger.warning("Unexpected error during authentication: %s", str(e))
            return None, Response(
                json.dumps({"status": "error", "message": "Unauthorized: Authentication error"}),
                content_type="application/json;charset=utf-8",
                status=401
            )

    @http.route('/api/v1/sfa/outlets', type='http', auth='public', methods=['GET'], csrf=False, save_session=False)
    def get_outlets(self, **kwargs):
        """Mengambil daftar outlet/pelanggan lengkap beserta koordinat GPS dengan dukungan pagination.
        Dijalankan dalam security context user terautentikasi (menerapkan ACL dan ir.rule).
        Field bernilai kosong dan koordinat 0.0 disanitasi menjadi null alih-alih boolean false atau 0.0.
        Mendukung query parameter 'limit', 'offset', dan 'page'.
        Stateless: save_session=False mencegah pembuatan session cookie.
        Dilindungi Rate Guard (ambang batas request).
        """
        rate_error = self._check_rate_guard()
        if rate_error:
            return rate_error

        user, auth_error = self._authenticate_request()
        if auth_error:
            return auth_error

        user_env = request.env(user=user)

        # Parsing parameter pagination
        try:
            limit = int(kwargs.get('limit', 50))
            if limit <= 0:
                limit = 50
            limit = min(limit, 200)
        except (ValueError, TypeError):
            limit = 50

        try:
            offset = int(kwargs.get('offset', 0))
            if offset < 0:
                offset = 0
        except (ValueError, TypeError):
            offset = 0

        page_param = kwargs.get('page')
        if page_param is not None:
            try:
                page = int(page_param)
                if page > 0:
                    offset = (page - 1) * limit
            except (ValueError, TypeError):
                pass

        try:
            domain = [('customer_rank', '>', 0)]
            if kwargs.get('outlet_zone'):
                domain.append(('outlet_zone', '=', kwargs.get('outlet_zone')))

            total_count = user_env['res.partner'].search_count(domain)
            outlets_raw = user_env['res.partner'].search_read(
                domain,
                ['id', 'name', 'street', 'city', 'partner_latitude', 'partner_longitude', 'outlet_zone', 'geofence_radius_meters'],
                limit=limit,
                offset=offset,
                order='id asc'
            )
            outlets_sanitized = self._sanitize_record(outlets_raw)

            data = {
                'status': 'success',
                'authenticated_user': user.name,
                'total': total_count,
                'count': len(outlets_sanitized),
                'limit': limit,
                'offset': offset,
                'page': (offset // limit) + 1 if limit > 0 else 1,
                'outlets': outlets_sanitized
            }
            return Response(
                json.dumps(data),
                content_type='application/json;charset=utf-8',
                status=200
            )
        except AccessError as ae:
            _logger.warning("AccessError in get_outlets for user %s: %s", user.login, str(ae))
            return Response(
                json.dumps({'status': 'error', 'message': f'Forbidden: {str(ae)}'}),
                content_type='application/json;charset=utf-8',
                status=403
            )
        except Exception as e:
            _logger.exception("Unexpected error in get_outlets: %s", str(e))
            return Response(
                json.dumps({'status': 'error', 'message': 'Internal server error'}),
                content_type='application/json;charset=utf-8',
                status=500
            )

    @http.route('/api/v1/sfa/sync-visit', type='http', auth='public', methods=['POST'], csrf=False, save_session=False)
    def sync_visit_data(self, **post):
        """Menerima data check-in kunjungan lapangan sales/driver.
        Mencatat pesan ke chatter partner sebagai Markup HTML yang valid.
        Dilengkapi validasi Geofencing: Menghitung jarak GPS check-in dengan koordinat outlet terdaftar.
        """
        rate_error = self._check_rate_guard()
        if rate_error:
            return rate_error

        user, auth_error = self._authenticate_request()
        if auth_error:
            return auth_error

        payload, json_error = self._parse_json_payload()
        if json_error:
            return json_error

        outlet_id = payload.get('outlet_id')
        if not outlet_id:
            return Response(
                json.dumps({"status": "error", "message": "outlet_id is required"}),
                content_type="application/json;charset=utf-8",
                status=400
            )

        user_env = request.env(user=user)

        try:
            partner = user_env['res.partner'].browse(int(outlet_id))
            if not partner.exists():
                return Response(
                    json.dumps({"status": "error", "message": "Outlet not found"}),
                    content_type="application/json;charset=utf-8",
                    status=404
                )
        except (ValueError, TypeError):
            return Response(
                json.dumps({"status": "error", "message": "Invalid outlet_id format"}),
                content_type="application/json;charset=utf-8",
                status=400
            )
        except AccessError as ae:
            _logger.warning("AccessError browsing partner %s by user %s: %s", outlet_id, user.login, str(ae))
            return Response(
                json.dumps({"status": "error", "message": f"Forbidden: {str(ae)}"}),
                content_type="application/json;charset=utf-8",
                status=403
            )

        # ---------------------------------------------------------------------
        # GEOFENCING VALIDATION
        # ---------------------------------------------------------------------
        raw_lat = payload.get('checkin_latitude', payload.get('latitude'))
        raw_lon = payload.get('checkin_longitude', payload.get('longitude'))
        geofence_info = None
        geofence_badge = ""

        if raw_lat is not None and raw_lon is not None:
            try:
                checkin_lat = float(raw_lat)
                checkin_lon = float(raw_lon)
                if math.isfinite(checkin_lat) and math.isfinite(checkin_lon):
                    if partner.partner_latitude and partner.partner_longitude:
                        dist_m = _calculate_haversine_distance(
                            checkin_lat, checkin_lon,
                            partner.partner_latitude, partner.partner_longitude
                        )
                        max_radius = partner.geofence_radius_meters or 200.0
                        is_inside = (dist_m <= max_radius)
                        geofence_info = {
                            "verified": is_inside,
                            "distance_meters": dist_m,
                            "allowed_radius_meters": max_radius,
                            "checkin_coordinates": [checkin_lat, checkin_lon]
                        }
                        if is_inside:
                            geofence_badge = f"<br/><span style='color: #28a745; font-weight: bold;'>&#10004; Geofence Verified ({dist_m}m dari outlet)</span>"
                        else:
                            geofence_badge = f"<br/><span style='color: #dc3545; font-weight: bold;'>&#9888; Geofence Mismatch: {dist_m}m dari outlet (Radius maks: {max_radius}m)</span>"
                    else:
                        geofence_info = {
                            "verified": False,
                            "reason": "Outlet does not have registered GPS coordinates"
                        }
            except (ValueError, TypeError):
                geofence_info = None

        notes = payload.get('notes', '')
        clean_notes = escape(str(notes)) if notes else ""
        formatted_notes = clean_notes.replace('\n', Markup('<br/>'))
        body_html = Markup("<b>[SFA Field Check-In]</b>") + Markup(geofence_badge) + Markup("<br/>") + formatted_notes

        # Fallback email_from dan author_id untuk kompatibilitas Odoo 18 chatter
        email_from = user.email_formatted or user.email or f'"{user.name}" <noreply@ibns.local>'
        author_id = user.partner_id.id if user.partner_id else False

        try:
            partner.message_post(
                body=body_html,
                message_type='comment',
                subtype_xmlid='mail.mt_note',
                email_from=email_from,
                author_id=author_id,
            )
            _logger.info("Field visit logged for outlet %s by user %s", partner.id, user.login)

            res_payload = {
                "status": "success",
                "message": "Visit logged to chatter",
                "outlet_id": partner.id,
                "logged_by": user.name
            }
            if geofence_info:
                res_payload["geofence"] = geofence_info

            return Response(
                json.dumps(res_payload),
                content_type="application/json;charset=utf-8",
                status=200
            )
        except AccessError as ae:
            user_env.cr.rollback()
            _logger.warning("AccessError posting chatter note by user %s: %s", user.login, str(ae))
            return Response(
                json.dumps({"status": "error", "message": f"Forbidden: {str(ae)}"}),
                content_type="application/json;charset=utf-8",
                status=403
            )
        except Exception as e:
            user_env.cr.rollback()
            _logger.exception("Unexpected error logging visit to chatter: %s", str(e))
            return Response(
                json.dumps({"status": "error", "message": "Internal server error"}),
                content_type="application/json;charset=utf-8",
                status=500
            )

    @http.route('/api/v1/sfa/invoices', type='http', auth='public', methods=['POST'], csrf=False, save_session=False)
    def create_invoice(self, **post):
        """Membuat dan memposting faktur penjualan (account.move) secara atomik.
        Dilindungi Rate Guard dan Idempotency Guard.
        """
        rate_error = self._check_rate_guard()
        if rate_error:
            return rate_error

        user, auth_error = self._authenticate_request()
        if auth_error:
            return auth_error

        payload, json_error = self._parse_json_payload()
        if json_error:
            return json_error

        partner_id = payload.get('partner_id')
        if not partner_id:
            return Response(
                json.dumps({"status": "error", "message": "partner_id is required"}),
                content_type="application/json;charset=utf-8",
                status=400
            )

        raw_amount = payload.get('amount')
        if raw_amount is None:
            return Response(
                json.dumps({"status": "error", "message": "amount is required"}),
                content_type="application/json;charset=utf-8",
                status=400
            )

        # Validasi ketat finite number: Tolak NaN, Infinity, -Infinity, dan <= 0
        try:
            amount = float(raw_amount)
            if not math.isfinite(amount) or amount <= 0:
                _logger.warning("Rejected non-finite or non-positive invoice amount: %s", raw_amount)
                return Response(
                    json.dumps({"status": "error", "message": "amount must be a finite number greater than zero"}),
                    content_type="application/json;charset=utf-8",
                    status=400
                )
        except (ValueError, TypeError):
            return Response(
                json.dumps({"status": "error", "message": "Invalid amount format"}),
                content_type="application/json;charset=utf-8",
                status=400
            )

        description = payload.get('description') or "SFA Field Invoice"

        # Ekstraksi Idempotency Key
        idempotency_key = (
            request.httprequest.headers.get('X-Idempotency-Key') or
            request.httprequest.headers.get('Idempotency-Key') or
            payload.get('idempotency_key')
        )
        if idempotency_key:
            idempotency_key = str(idempotency_key).strip()

        user_env = request.env(user=user)

        try:
            partner = user_env['res.partner'].browse(int(partner_id))
            if not partner.exists() or not partner.active:
                return Response(
                    json.dumps({"status": "error", "message": "Active partner not found"}),
                    content_type="application/json;charset=utf-8",
                    status=400
                )
        except (ValueError, TypeError):
            return Response(
                json.dumps({"status": "error", "message": "Invalid partner_id format"}),
                content_type="application/json;charset=utf-8",
                status=400
            )
        except AccessError as ae:
            _logger.warning("AccessError browsing partner %s by user %s: %s", partner_id, user.login, str(ae))
            return Response(
                json.dumps({"status": "error", "message": f"Forbidden: {str(ae)}"}),
                content_type="application/json;charset=utf-8",
                status=403
            )

        # Idempotency Check: Cegah duplikasi tagihan
        if idempotency_key:
            existing_move = user_env['account.move'].search([
                ('sfa_idempotency_key', '=', idempotency_key),
                ('company_id', '=', partner.company_id.id or user_env.company.id)
            ], limit=1)
            if existing_move:
                _logger.info("Idempotent replay detected for key '%s'. Returning existing invoice %s",
                             idempotency_key, existing_move.name)
                return Response(
                    json.dumps({
                        "status": "success",
                        "invoice_id": existing_move.id,
                        "invoice_name": existing_move.name,
                        "amount_total": existing_move.amount_total,
                        "state": existing_move.state,
                        "created_by": existing_move.create_uid.name or user.name,
                        "idempotent_replay": True
                    }),
                    content_type="application/json;charset=utf-8",
                    status=200
                )

        try:
            journal = user_env['account.journal'].search([
                ('type', '=', 'sale'),
                ('company_id', '=', partner.company_id.id or user_env.company.id)
            ], limit=1)
            if not journal:
                journal = user_env['account.journal'].search([('type', '=', 'sale')], limit=1)
            if not journal:
                return Response(
                    json.dumps({"status": "error", "message": "Default sale journal not found"}),
                    content_type="application/json;charset=utf-8",
                    status=400
                )

            move_vals = {
                'move_type': 'out_invoice',
                'partner_id': partner.id,
                'journal_id': journal.id,
                'invoice_date': fields.Date.context_today(partner),
                'invoice_line_ids': [
                    (0, 0, {
                        'name': description,
                        'quantity': 1.0,
                        'price_unit': amount,
                    })
                ],
            }
            if idempotency_key:
                move_vals['sfa_idempotency_key'] = idempotency_key

            move = user_env['account.move'].create(move_vals)
            move.action_post()

            _logger.info("Invoice %s created and posted successfully by user %s (Amount: %s)",
                         move.name, user.login, move.amount_total)

            return Response(
                json.dumps({
                    "status": "success",
                    "invoice_id": move.id,
                    "invoice_name": move.name,
                    "amount_total": move.amount_total,
                    "state": move.state,
                    "created_by": user.name,
                    "idempotent_replay": False
                }),
                content_type="application/json;charset=utf-8",
                status=201
            )
        except AccessError as ae:
            user_env.cr.rollback()
            _logger.warning("AccessError creating/posting invoice by user %s: %s", user.login, str(ae))
            return Response(
                json.dumps({"status": "error", "message": f"Forbidden: Insufficient permissions to create or post invoices ({str(ae)})"}),
                content_type="application/json;charset=utf-8",
                status=403
            )
        except (UserError, ValidationError, ValueError) as ue:
            user_env.cr.rollback()
            _logger.warning("Business logic error creating invoice by user %s: %s", user.login, str(ue))
            return Response(
                json.dumps({"status": "error", "message": str(ue)}),
                content_type="application/json;charset=utf-8",
                status=400
            )
        except Exception as e:
            user_env.cr.rollback()
            _logger.exception("Unexpected error creating invoice: %s", str(e))
            return Response(
                json.dumps({"status": "error", "message": "Internal server error"}),
                content_type="application/json;charset=utf-8",
                status=500
            )
