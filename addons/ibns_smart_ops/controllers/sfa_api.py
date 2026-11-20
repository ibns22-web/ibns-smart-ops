# -*- coding: utf-8 -*-
import hmac
import json
import logging
import math
import os
import threading
import time
from collections import defaultdict

try:
    from markupsafe import Markup, escape
except ImportError:
    from odoo.tools import Markup, html_escape as escape

import psycopg2
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
_rate_limit_lock = threading.Lock()


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
        Dilindungi _rate_limit_lock agar aman digunakan pada konkurensi multi-thread.
        """
        now = time.time()
        window_start = now - RATE_LIMIT_WINDOW

        # Identifikasi klien berdasarkan IP
        client_ip = request.httprequest.remote_addr or 'unknown_ip'

        with _rate_limit_lock:
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
        Koordinat bernilai (0.0, 0.0) (Null Island), False, atau None dikonversi menjadi null.
        Namun jika salah satu koordinat valid dan pasangannya 0.0 (misal di garis khatulistiwa lat=0.0
        atau meridian utama lon=0.0), nilai 0.0 tetap dipertahankan sebagai koordinat valid.
        Mencegah error parsing tipe data pada klien mobile akibat field kosong terkirim sebagai boolean false.
        """
        if isinstance(data, dict):
            res = {}
            has_lat = 'partner_latitude' in data
            has_lon = 'partner_longitude' in data
            is_null_island = False
            if has_lat and has_lon:
                lat_val = data.get('partner_latitude')
                lon_val = data.get('partner_longitude')
                is_null_island = (lat_val in (False, None, 0.0)) and (lon_val in (False, None, 0.0))

            for k, v in data.items():
                if k in ('partner_latitude', 'partner_longitude'):
                    if is_null_island or v is False or v is None:
                        res[k] = None
                    else:
                        res[k] = v
                elif v is False:
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

    def _get_user_env(self, user, target_company=None):
        """Membangun environment Odoo untuk user terautentikasi dengan konteks multi-company yang valid.
        Memastikan 'allowed_company_ids' selalu terisi dari user.company_ids agar tidak terblokir
        oleh Record Rules multi-company, dan menempatkan target_company di urutan pertama sebagai active company.
        Mendukung juga preferensi company via header 'X-Company-Id'.
        Mengembalikan tuple (user_env, error_response).
        """
        user_companies = user.company_ids
        if not user_companies:
            user_companies = user.company_id

        has_cid_header = 'X-Company-Id' in request.httprequest.headers or 'x-company-id' in request.httprequest.headers
        raw_cid = request.httprequest.headers.get('X-Company-Id')
        if not has_cid_header and raw_cid is not None:
            has_cid_header = True

        if target_company:
            active_company = target_company if target_company in user_companies else (user.company_id if user.company_id in user_companies else user_companies[0])
        elif has_cid_header:
            if isinstance(raw_cid, (bool, float)) or raw_cid is None:
                return None, Response(
                    json.dumps({"status": "error", "message": "Invalid X-Company-Id header: must be a positive integer"}),
                    content_type="application/json;charset=utf-8",
                    status=400
                )
            cid_str = str(raw_cid).strip()
            if not cid_str:
                return None, Response(
                    json.dumps({"status": "error", "message": "Invalid X-Company-Id header: must be a positive integer"}),
                    content_type="application/json;charset=utf-8",
                    status=400
                )
            try:
                cid = int(cid_str)
                if cid <= 0 or not cid_str.isdigit():
                    return None, Response(
                        json.dumps({"status": "error", "message": "Invalid X-Company-Id header: must be a positive integer"}),
                        content_type="application/json;charset=utf-8",
                        status=400
                    )
            except (ValueError, TypeError):
                return None, Response(
                    json.dumps({"status": "error", "message": "Invalid X-Company-Id header: must be a positive integer"}),
                    content_type="application/json;charset=utf-8",
                    status=400
                )

            if cid not in user_companies.ids:
                return None, Response(
                    json.dumps({"status": "error", "message": "Forbidden: You do not have access to the requested company"}),
                    content_type="application/json;charset=utf-8",
                    status=403
                )

            active_company = user_companies.filtered(lambda c: c.id == cid)[0]
        else:
            active_company = user.company_id if user.company_id in user_companies else user_companies[0]

        allowed_ids = [active_company.id] + [cid for cid in user_companies.ids if cid != active_company.id]
        ctx = dict(request.env.context, allowed_company_ids=allowed_ids)
        return request.env(user=user, context=ctx), None

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

        user_env, env_error = self._get_user_env(user)
        if env_error:
            return env_error

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

        if isinstance(outlet_id, bool):
            return Response(
                json.dumps({"status": "error", "message": "Invalid outlet_id format"}),
                content_type="application/json;charset=utf-8",
                status=400
            )

        user_env, env_error = self._get_user_env(user)
        if env_error:
            return env_error

        try:
            partner = user_env['res.partner'].browse(int(outlet_id))
            if not partner.exists() or not partner.active:
                return Response(
                    json.dumps({"status": "error", "message": "Outlet not found or archived"}),
                    content_type="application/json;charset=utf-8",
                    status=404
                )
            if partner.company_id and partner.company_id in user.company_ids and partner.company_id != user_env.company:
                user_env, env_error = self._get_user_env(user, target_company=partner.company_id)
                if env_error:
                    return env_error
                partner = partner.with_env(user_env)
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

        # Jika salah satu koordinat dikirimkan, keduanya wajib ada dan berupa angka finite yang valid
        if raw_lat is not None or raw_lon is not None:
            if raw_lat is None or raw_lon is None:
                return Response(
                    json.dumps({"status": "error", "message": "Both checkin_latitude and checkin_longitude are required when submitting coordinates"}),
                    content_type="application/json;charset=utf-8",
                    status=400
                )
            if isinstance(raw_lat, bool) or isinstance(raw_lon, bool):
                return Response(
                    json.dumps({"status": "error", "message": "Invalid checkin coordinates format: boolean values not allowed"}),
                    content_type="application/json;charset=utf-8",
                    status=400
                )
            try:
                checkin_lat = float(raw_lat)
                checkin_lon = float(raw_lon)
                if not (math.isfinite(checkin_lat) and math.isfinite(checkin_lon)):
                    raise ValueError("Non-finite coordinates")
                if not (-90.0 <= checkin_lat <= 90.0 and -180.0 <= checkin_lon <= 180.0):
                    raise ValueError("Coordinates out of WGS84 range")
            except (ValueError, TypeError):
                return Response(
                    json.dumps({"status": "error", "message": "Invalid checkin coordinates format: must be valid finite numbers (lat: -90..90, lon: -180..180)"}),
                    content_type="application/json;charset=utf-8",
                    status=400
                )

            has_partner_coords = (
                partner.partner_latitude is not False and partner.partner_latitude is not None and
                partner.partner_longitude is not False and partner.partner_longitude is not None
            )
            if has_partner_coords and (partner.partner_latitude != 0.0 or partner.partner_longitude != 0.0):
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

        if isinstance(partner_id, bool):
            return Response(
                json.dumps({"status": "error", "message": "Invalid partner_id format"}),
                content_type="application/json;charset=utf-8",
                status=400
            )

        sale_order_id = payload.get('sale_order_id')
        if sale_order_id is not None:
            if isinstance(sale_order_id, bool):
                return Response(
                    json.dumps({"status": "error", "message": "Invalid sale_order_id format"}),
                    content_type="application/json;charset=utf-8",
                    status=400
                )
            try:
                sale_order_id = int(sale_order_id)
            except (ValueError, TypeError):
                return Response(
                    json.dumps({"status": "error", "message": "Invalid sale_order_id format"}),
                    content_type="application/json;charset=utf-8",
                    status=400
                )

        if sale_order_id is None:
            raw_amount = payload.get('amount')
            if raw_amount is None:
                return Response(
                    json.dumps({"status": "error", "message": "amount is required"}),
                    content_type="application/json;charset=utf-8",
                    status=400
                )

            if isinstance(raw_amount, bool):
                return Response(
                    json.dumps({"status": "error", "message": "amount must be a finite number greater than zero"}),
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
        else:
            raw_amount = payload.get('amount')
            if raw_amount is not None:
                if isinstance(raw_amount, bool):
                    return Response(
                        json.dumps({"status": "error", "message": "amount must be a finite number greater than zero"}),
                        content_type="application/json;charset=utf-8",
                        status=400
                    )
                try:
                    amount = float(raw_amount)
                    if not math.isfinite(amount) or amount <= 0:
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

        user_env, env_error = self._get_user_env(user)
        if env_error:
            return env_error

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

        # Evaluasi target company berdasarkan partner (atau fallback active company user)
        target_company = partner.company_id if partner.company_id and partner.company_id in user.company_ids else user_env.company
        user_env, env_error = self._get_user_env(user, target_company=target_company)
        if env_error:
            return env_error
        partner = partner.with_env(user_env)

        sale_order = None
        if sale_order_id is not None:
            try:
                sale_order = user_env['sale.order'].browse(sale_order_id)
                if not sale_order.exists():
                    return Response(
                        json.dumps({"status": "error", "message": "Sale order not found"}),
                        content_type="application/json;charset=utf-8",
                        status=400
                    )
                if sale_order.partner_id.id != partner.id:
                    return Response(
                        json.dumps({"status": "error", "message": "Sale order does not belong to the specified partner"}),
                        content_type="application/json;charset=utf-8",
                        status=400
                    )
            except AccessError as ae:
                _logger.warning("AccessError browsing sale.order %s by user %s: %s", sale_order_id, user.login, str(ae))
                return Response(
                    json.dumps({"status": "error", "message": f"Forbidden: Insufficient permissions to access sale order ({str(ae)})"}),
                    content_type="application/json;charset=utf-8",
                    status=403
                )

        # Idempotency Check: Cegah duplikasi tagihan dalam cakupan hak akses user & allowed companies
        if idempotency_key:
            try:
                existing_move = user_env['account.move'].search([
                    ('sfa_idempotency_key', '=', idempotency_key),
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
            except AccessError as ae:
                _logger.warning("AccessError during idempotency check by user %s: %s", user.login, str(ae))
                return Response(
                    json.dumps({"status": "error", "message": f"Forbidden: Insufficient permissions to access invoices ({str(ae)})"}),
                    content_type="application/json;charset=utf-8",
                    status=403
                )

        try:
            if sale_order:
                invoices = sale_order._create_invoices()
                if not invoices:
                    return Response(
                        json.dumps({"status": "error", "message": "No invoiceable lines found for this sale order"}),
                        content_type="application/json;charset=utf-8",
                        status=400
                    )
                move = invoices[0]
                if idempotency_key:
                    move.sfa_idempotency_key = idempotency_key
                move.action_post()
            else:
                journal = user_env['account.journal'].search([
                    ('type', '=', 'sale'),
                    ('company_id', '=', target_company.id)
                ], limit=1)
                if not journal:
                    return Response(
                        json.dumps({
                            "status": "error",
                            "message": f"Sale journal for company '{target_company.name}' not found"
                        }),
                        content_type="application/json;charset=utf-8",
                        status=400
                    )

                # Gunakan default_account_id dari jurnal penjualan yang dipilih (mencegah AccessError pada
                # account.account dan mencegah pemilihan akun pendapatan acak seperti income_other/selisih kurs)
                inv_line_vals = {
                    'name': description,
                    'quantity': 1.0,
                    'price_unit': amount,
                    'tax_ids': [(5, 0, 0)],
                }
                if journal.default_account_id:
                    inv_line_vals['account_id'] = journal.default_account_id.id

                move_vals = {
                    'move_type': 'out_invoice',
                    'partner_id': partner.id,
                    'company_id': target_company.id,
                    'journal_id': journal.id,
                    'invoice_date': fields.Date.context_today(partner),
                    'invoice_line_ids': [(0, 0, inv_line_vals)],
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
        except psycopg2.IntegrityError as ie:
            user_env.cr.rollback()
            # Race condition handling: tangani request paralel dengan Idempotency-Key yang sama persis
            if idempotency_key:
                try:
                    existing_move = user_env['account.move'].search([
                        ('sfa_idempotency_key', '=', idempotency_key),
                    ], limit=1)
                    if existing_move:
                        _logger.info("Concurrent race-condition resolved for idempotency key '%s'. Returning existing invoice %s",
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
                except AccessError:
                    return Response(
                        json.dumps({"status": "error", "message": "Forbidden: Insufficient permissions to access invoices"}),
                        content_type="application/json;charset=utf-8",
                        status=403
                    )
            _logger.warning("Database integrity constraint violated during invoice creation: %s", str(ie))
            return Response(
                json.dumps({"status": "error", "message": "Idempotency key collision or database constraint violated"}),
                content_type="application/json;charset=utf-8",
                status=409
            )
        except Exception as e:
            user_env.cr.rollback()
            _logger.exception("Unexpected error creating invoice: %s", str(e))
            return Response(
                json.dumps({"status": "error", "message": "Internal server error"}),
                content_type="application/json;charset=utf-8",
                status=500
            )
