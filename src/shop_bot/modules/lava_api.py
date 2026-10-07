"""Lava.top Public API 1.22.0, one-time dynamically priced invoices.

Contract: https://gate.lava.top/docs/documentation.yaml
API credentials are sent only to the fixed gateway origin, without redirects.
"""
import asyncio
import logging
from decimal import Decimal, InvalidOperation
from hmac import compare_digest
from typing import Any, Dict, Optional
from urllib.parse import quote, urljoin, urlsplit

import aiohttp

logger = logging.getLogger(__name__)
LAVA_GATE_BASE_URL = 'https://gate.lava.top'


def amount_matches(amount, currency, expected_amount) -> bool:
    try:
        received = Decimal(str(amount))
        expected = Decimal(str(expected_amount))
        return (currency == 'RUB' and received.is_finite() and expected.is_finite()
                and received > 0 and received == expected)
    except (ValueError, InvalidOperation):
        return False


async def resolve_offer_id(api_key: str, identifier: str,
                           session: Optional[aiohttp.ClientSession] = None,
                           timeout_sec: int = 10) -> str:
    """Resolve items[].data.offers, following bounded same-origin pagination.

    Do not silently choose the first of multiple offers: the operator must supply
    the intended Offer ID in that case. A real Offer ID remains unchanged.
    """
    if not api_key or not identifier:
        return identifier
    url = f'{LAVA_GATE_BASE_URL}/api/v2/products?feedVisibility=ALL'
    headers = {'X-Api-Key': api_key, 'Accept': 'application/json'}
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_sec))
    seen = set()
    try:
        async with asyncio.timeout(timeout_sec):
            for _ in range(10):
                parsed = urlsplit(url)
                if (parsed.scheme != 'https' or parsed.netloc != 'gate.lava.top'
                        or parsed.path != '/api/v2/products' or parsed.fragment or url in seen):
                    break
                seen.add(url)
                async with session.get(url, headers=headers, allow_redirects=False) as response:
                    if response.status != 200:
                        logger.warning('Lava product lookup failed: HTTP %s', response.status)
                        break
                    data = await response.json()
                if not isinstance(data, dict) or not isinstance(data.get('items'), list):
                    break
                for item in data['items']:
                    if not isinstance(item, dict):
                        continue
                    product = item.get('data', item)  # Also tolerate the older flat representation.
                    if not isinstance(product, dict):
                        continue
                    offers = product.get('offers') or []
                    if not isinstance(offers, list):
                        continue
                    ids = [offer['id'] for offer in offers if isinstance(offer, dict)
                           and isinstance(offer.get('id'), str) and offer['id']]
                    if identifier in ids:
                        return identifier
                    if product.get('id') == identifier:
                        if len(ids) == 1:
                            return ids[0]
                        logger.warning('Product has no unique offer; configure the exact Offer ID')
                        return identifier
                next_page = data.get('nextPage')
                if not isinstance(next_page, str) or not next_page:
                    break
                url = urljoin(LAVA_GATE_BASE_URL, next_page)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        logger.warning('Lava product lookup unavailable')
    finally:
        if own_session:
            await session.close()
    return identifier


def _invoice_result(data, amount) -> Optional[Dict[str, Any]]:
    if not isinstance(data, dict):
        return None
    total = data.get('amountTotal')
    if not isinstance(total, dict) or not amount_matches(total.get('amount'), total.get('currency'), amount):
        logger.error('Lava invoice price/currency differs from the requested order')
        return None
    invoice_id, payment_url = data.get('id'), data.get('paymentUrl')
    if not isinstance(invoice_id, str) or not invoice_id or not isinstance(payment_url, str):
        return None
    parsed = urlsplit(payment_url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        return None
    return {'invoice_id': invoice_id, 'payment_url': payment_url, 'status': data.get('status'),
            'amount': float(amount), 'currency': 'RUB'}


async def create_invoice(amount: float, email: str, api_key: str, offer_id: str,
                         bot_username: Optional[str] = None, sbp_mode: bool = True,
                         timeout_sec: int = 15) -> Optional[Dict[str, Any]]:
    if not api_key or not offer_id:
        return None
    try:
        price = Decimal(str(amount))
        if not price.is_finite() or price <= 0 or price != price.quantize(Decimal('0.01')):
            return None
    except (ValueError, InvalidOperation):
        return None
    return_url = f'https://t.me/{bot_username}' if bot_username else 'https://t.me'
    payload = {'email': email, 'offerId': offer_id, 'currency': 'RUB', 'amount': float(price),
               'successful_return_url': return_url, 'failure_return_url': return_url,
               'cancel_return_url': return_url}
    if sbp_mode:
        payload.update(paymentProvider='PAY2ME', paymentMethod='SBP')
    headers = {'X-Api-Key': api_key, 'Content-Type': 'application/json', 'Accept': 'application/json'}
    url = f'{LAVA_GATE_BASE_URL}/api/v3/invoice'
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_sec)) as session:
            async with session.post(url, json=payload, headers=headers, allow_redirects=False) as response:
                data = await response.json()
                status = response.status
            if status in (200, 201):
                return _invoice_result(data, price)
            if status == 404:
                resolved = await resolve_offer_id(api_key, offer_id, session=session, timeout_sec=timeout_sec)
                if resolved != offer_id:
                    payload['offerId'] = resolved
                    async with session.post(url, json=payload, headers=headers, allow_redirects=False) as response:
                        if response.status in (200, 201):
                            return _invoice_result(await response.json(), price)
            logger.warning('Lava invoice creation failed; verify product, offer and dynamic pricing')
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        # Never retry an ambiguous POST: a contract may already have been created.
        logger.warning('Lava invoice creation could not be confirmed')
    return None


async def get_invoice_status(invoice_id: str, api_key: str,
                             timeout_sec: int = 15) -> Optional[Dict[str, Any]]:
    if not api_key or not invoice_id:
        return None
    headers = {'X-Api-Key': api_key, 'Accept': 'application/json'}
    url = f'{LAVA_GATE_BASE_URL}/api/v1/invoices/{quote(invoice_id, safe="")}'
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_sec)) as session:
            async with session.get(url, headers=headers, allow_redirects=False) as response:
                if response.status != 200:
                    return None
                data = await response.json()
        if not isinstance(data, dict) or data.get('id') != invoice_id:
            return None
        status = str(data.get('status', '')).lower()
        receipt = data.get('receipt') or {}
        if not isinstance(receipt, dict):
            return None
        return {'invoice_id': invoice_id, 'status': status, 'is_paid': status == 'completed',
                'amount': receipt.get('amount'), 'currency': receipt.get('currency')}
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        logger.warning('Lava invoice status unavailable')
        return None


def verify_webhook_auth(headers: dict, expected_key: Optional[str]) -> bool:
    """Only the configured X-Api-Key is accepted; missing configuration fails closed."""
    if not expected_key:
        return False
    received = headers.get('X-Api-Key') or headers.get('x-api-key')
    return isinstance(received, str) and compare_digest(received.encode(), expected_key.encode())
