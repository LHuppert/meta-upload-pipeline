"""
Meta Conversions API (CAPI) Gateway — Render-Version
----------------------------------------------------
Public auf https://meta-upload-pipeline.onrender.com erreichbar.

Sendet Events (Purchase, ViewContent, AddToCart, Lead) server-seitig an das
Facebook Pixel. Umgeht das Facebook-for-WooCommerce-Plugin vollständig und
arbeitet direkt gegen Graph API v21.0.

Dedup-Strategie: event_id matcht mit Browser-Pixel (Format: "order_<ID>")
damit Meta Ereignisse die doppelt feuern nur einmal zählt.
"""
import os
import json
import time
import hashlib
import uuid
import logging
import requests

logger = logging.getLogger(__name__)

META_GRAPH_URL = 'https://graph.facebook.com/v21.0'


# ────────────────────────────── Settings ──────────────────────────────

def _token() -> str:
    # System-User-Token oder User-Token mit ads_management Scope
    return os.environ.get('META_CAPI_TOKEN', '') or os.environ.get('META_ACCESS_TOKEN', '')


def _pixel_id() -> str:
    return os.environ.get('META_PIXEL_ID', '')


def _test_event_code() -> str:
    """Wenn gesetzt, Events im Meta Test-Events-Tool sichtbar (nicht live gezählt)."""
    return os.environ.get('META_CAPI_TEST_CODE', '')


def _shop_url() -> str:
    return os.environ.get('WC_SHOP_URL', 'https://terrapretawein.de')


# ────────────────────────────── Hashing ──────────────────────────────

def _sha256(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode('utf-8')).hexdigest()


def _hash_phone(phone: str) -> str:
    digits = ''.join(c for c in (phone or '') if c.isdigit())
    if not digits:
        return ''
    return hashlib.sha256(digits.encode('utf-8')).hexdigest()


def _hash_country(country: str) -> str:
    code = (country or '').strip().lower()
    if len(code) > 2:
        mapping = {'germany': 'de', 'deutschland': 'de', 'austria': 'at',
                   'österreich': 'at', 'switzerland': 'ch', 'schweiz': 'ch'}
        code = mapping.get(code, code[:2])
    if not code:
        return ''
    return hashlib.sha256(code.encode('utf-8')).hexdigest()


def build_user_data(
    email: str = '',
    phone: str = '',
    first_name: str = '',
    last_name: str = '',
    city: str = '',
    state: str = '',
    zip_code: str = '',
    country: str = '',
    external_id: str = '',
    client_ip: str = '',
    user_agent: str = '',
    fbc: str = '',
    fbp: str = '',
) -> dict:
    ud = {}
    if email:       ud['em'] = [_sha256(email)]
    if phone:       ud['ph'] = [_hash_phone(phone)]
    if first_name:  ud['fn'] = [_sha256(first_name)]
    if last_name:   ud['ln'] = [_sha256(last_name)]
    if city:        ud['ct'] = [_sha256(city.replace(' ', ''))]
    if state:       ud['st'] = [_sha256(state)]
    if zip_code:    ud['zp'] = [_sha256(zip_code)]
    if country:
        h = _hash_country(country)
        if h:
            ud['country'] = [h]
    if external_id: ud['external_id'] = [_sha256(str(external_id))]
    if client_ip:   ud['client_ip_address'] = client_ip
    if user_agent:  ud['client_user_agent'] = user_agent
    if fbc:         ud['fbc'] = fbc
    if fbp:         ud['fbp'] = fbp
    return ud


# ────────────────────────────── Core send ──────────────────────────────

def send_event(
    event_name: str,
    user_data: dict,
    custom_data: dict = None,
    event_id: str = None,
    event_source_url: str = '',
    action_source: str = 'website',
) -> dict:
    pixel = _pixel_id()
    token = _token()
    if not pixel or not token:
        return {'ok': False, 'error': 'Pixel-ID oder Token fehlt'}

    event_id = event_id or str(uuid.uuid4())
    event = {
        'event_name': event_name,
        'event_time': int(time.time()),
        'event_id': event_id,
        'action_source': action_source,
        'user_data': user_data,
    }
    if event_source_url:
        event['event_source_url'] = event_source_url
    if custom_data:
        event['custom_data'] = custom_data

    payload = {'data': [event]}
    test_code = _test_event_code()
    if test_code:
        payload['test_event_code'] = test_code

    try:
        url = f'{META_GRAPH_URL}/{pixel}/events'
        resp = requests.post(
            url,
            params={'access_token': token},
            json=payload,
            timeout=15,
        )
        data = resp.json() if resp.content else {}
        if resp.status_code != 200:
            logger.error(f'CAPI {event_name} HTTP {resp.status_code}: {data}')
            return {
                'ok': False,
                'error': data.get('error', {}).get('message', f'HTTP {resp.status_code}'),
                'fbtrace_id': data.get('error', {}).get('fbtrace_id', ''),
            }
        return {
            'ok': True,
            'events_received': data.get('events_received', 0),
            'fbtrace_id': data.get('fbtrace_id', ''),
            'event_id': event_id,
        }
    except Exception as e:
        logger.error(f'CAPI {event_name} Exception: {e}')
        return {'ok': False, 'error': str(e)}


# ────────────────────────────── Convenience-Funktionen ──────────────────────────────

def send_purchase(order: dict, event_id: str = None) -> dict:
    """Verarbeitet ein WooCommerce-Order-Objekt und sendet ein Purchase-Event."""
    billing = order.get('billing', {}) or {}
    line_items = order.get('line_items', []) or []

    ud = build_user_data(
        email=billing.get('email', ''),
        phone=billing.get('phone', ''),
        first_name=billing.get('first_name', ''),
        last_name=billing.get('last_name', ''),
        city=billing.get('city', ''),
        state=billing.get('state', ''),
        zip_code=billing.get('postcode', ''),
        country=billing.get('country', ''),
        external_id=order.get('customer_id') or order.get('id', ''),
    )

    content_ids = [str(li.get('product_id')) for li in line_items if li.get('product_id')]
    num_items = sum(int(li.get('quantity', 1)) for li in line_items)

    cd = {
        'currency': (order.get('currency') or 'EUR').upper(),
        'value': float(order.get('total', 0) or 0),
        'content_type': 'product',
        'content_ids': content_ids,
        'num_items': num_items,
        'order_id': str(order.get('id', '')),
    }

    event_id = event_id or f"order_{order.get('id')}"

    source_url = ''
    if order.get('id'):
        source_url = f"{_shop_url().rstrip('/')}/checkout/order-received/{order['id']}/"

    return send_event(
        'Purchase',
        user_data=ud,
        custom_data=cd,
        event_id=event_id,
        event_source_url=source_url,
    )


def send_test() -> dict:
    """Dummy-Purchase für Pipeline-Verifikation."""
    ud = build_user_data(
        email='test@terrapretawein.de',
        phone='+4915112345678',
        first_name='Test',
        last_name='Kunde',
        city='Gundersheim',
        zip_code='67598',
        country='DE',
        external_id='test-999',
    )
    cd = {
        'currency': 'EUR',
        'value': 49.90,
        'content_type': 'product',
        'content_ids': ['0'],
        'num_items': 1,
        'order_id': 'TEST-999',
    }
    return send_event(
        'Purchase',
        user_data=ud,
        custom_data=cd,
        event_id=f'test_{int(time.time())}',
        event_source_url='https://terrapretawein.de/',
    )


def check_connection() -> dict:
    pixel = _pixel_id()
    token = _token()
    if not pixel:
        return {'ok': False, 'error': 'Pixel-ID fehlt'}
    if not token:
        return {'ok': False, 'error': 'Token fehlt'}
    try:
        r = requests.get(
            f'{META_GRAPH_URL}/{pixel}',
            params={'access_token': token, 'fields': 'id,name'},
            timeout=15,
        )
        if r.status_code == 200:
            d = r.json()
            return {'ok': True, 'pixel_id': d.get('id'), 'pixel_name': d.get('name')}
        return {'ok': False, 'error': f'HTTP {r.status_code}: {r.text[:200]}'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}
