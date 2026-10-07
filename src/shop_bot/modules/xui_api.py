"""RIXXX v1.11 panel adapter (not a 3x-ui adapter).

The panel owns the subscription token and aggregates local, federation and bonus
links. Never construct a subscription from an admin URL containing credentials.
"""
import asyncio
import hashlib
import ipaddress
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import aiohttp
from yarl import URL

from shop_bot.data_manager.database import get_host

logger = logging.getLogger(__name__)
NO_FEDERATION = 'No enabled federation nodes are configured.'
TIMEOUT = aiohttp.ClientTimeout(total=60, connect=10)


def _username(email):
    legacy = email.replace('@', '_').replace('+', '_').replace('.', '_')
    if re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', legacy):
        return legacy
    return 'tg_' + hashlib.sha256(email.encode()).hexdigest()[:40]


def _session(host):
    url = URL(host['host_url'].rstrip('/'))
    if url.scheme not in ('http', 'https') or not url.host or url.query_string or url.fragment:
        raise ValueError('Invalid panel URL')
    auth = aiohttp.BasicAuth(url.user, url.password or '') if url.user else None
    base = str(url.with_user(None)).rstrip('/')
    try:
        ipaddress.ip_address(url.host)
        ip_host = True
    except ValueError:
        ip_host = False
    # aiohttp rejects IP-host cookies by default, including 127.0.0.1.
    jar = aiohttp.CookieJar(unsafe=ip_host)
    return base, aiohttp.ClientSession(timeout=TIMEOUT, cookie_jar=jar, auth=auth)


async def _login_to_rixxx(session, host_url, username, password):
    async with session.post(f'{host_url}/api/login', json={'username': username, 'password': password}, allow_redirects=False) as response:
        if response.status != 200:
            logger.error('RIXXX login rejected: HTTP %s', response.status)
            return False
        data = await response.json()
        return isinstance(data, dict) and data.get('ok') is True


async def _users(session, base):
    async with session.get(f'{base}/api/users', allow_redirects=False) as response:
        if response.status != 200:
            raise RuntimeError('Could not list RIXXX users')
        data = await response.json()
        if not isinstance(data, list):
            raise ValueError('Unexpected RIXXX user list')
        return data


def _find(users, email):
    return next((u for u in users if u.get('email') == email or u.get('username') == _username(email)), None)


async def _federate(session, base, user_id, action, email):
    async with session.post(f'{base}/api/users/{user_id}/federation/{action}',
                            json={'email': email}, allow_redirects=False) as response:
        data = await response.json()
        if response.status == 400 and data.get('error') == NO_FEDERATION:
            return True
        results = data.get('results')
        success = (response.status == 200 and data.get('ok') is True
                   and isinstance(results, list)
                   and all(isinstance(r, dict) and r.get('ok') is True for r in results))
        if not success:
            logger.error('RIXXX federation %s incomplete for user ID %s (HTTP %s)', action, user_id, response.status)
        return success


async def _sub_link(session, base, user_id):
    async with session.get(f'{base}/api/users/{user_id}/sub-link', allow_redirects=False) as response:
        if response.status != 200:
            return None
        link = (await response.json()).get('link')
        if not isinstance(link, str):
            return None
        parsed = urlsplit(link)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Unsafe subscription URL returned by panel')
        return link


async def create_or_update_key_on_host(host_name: str, email: str, days_to_add: int) -> dict | None:
    host = get_host(host_name)
    if not host or not email or not isinstance(days_to_add, int) or not 1 <= days_to_add <= 36500:
        return None
    try:
        base, context = _session(host)
        async with context as session:
            if not await _login_to_rixxx(session, base, host['host_username'], host['host_pass']):
                return None
            user = _find(await _users(session, base), email)
            now = datetime.now(timezone.utc)
            current = now
            if user and user.get('expiry'):
                current = datetime.fromisoformat(user['expiry'].replace('Z', '+00:00'))
                if current.tzinfo is None:
                    current = current.replace(tzinfo=timezone.utc)
            expiry = max(now, current) + timedelta(days=days_to_add)
            payload = {'expiry': expiry.isoformat()}
            try:
                if user:
                    async with session.put(f"{base}/api/users/{user['id']}", json=payload, allow_redirects=False) as response:
                        if response.status != 200:
                            return None
                else:
                    payload.update(username=_username(email), email=email, password=secrets.token_urlsafe(24),
                                   protocols=['naive', 'mieru', 'hy2'], quotaMB=0)
                    async with session.post(f'{base}/api/users', json=payload, allow_redirects=False) as response:
                        if response.status not in (200, 201):
                            return None
                        user = await response.json()
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
                # A disconnected PUT/POST may already be committed. Read it back;
                # do not blindly add the purchased period again.
                await asyncio.sleep(1)
                if not await _login_to_rixxx(session, base, host['host_username'], host['host_pass']):
                    return None
                user = _find(await _users(session, base), email)
                if not user or not user.get('expiry'):
                    return None
                actual = datetime.fromisoformat(user['expiry'].replace('Z', '+00:00'))
                if actual.tzinfo is None:
                    actual = actual.replace(tzinfo=timezone.utc)
                if abs((actual - expiry).total_seconds()) > 1:
                    return None
            if not user or not user.get('id'):
                return None
            federation_ok = False
            try:
                federation_ok = await _federate(session, base, user['id'], 'deploy', email)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                logger.error('Federation deploy could not be confirmed')
            link = await _sub_link(session, base, user['id'])
            if not link:
                return None
            return {'client_uuid': user['id'], 'email': email, 'expiry_timestamp_ms': int(expiry.timestamp() * 1000),
                    'connection_string': link, 'host_name': host_name, 'federation_ok': federation_ok}
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, RuntimeError, KeyError):
        # Do not log exception URLs: legacy configurations embed Basic Auth secrets.
        logger.error('RIXXX provisioning failed; check connectivity and panel configuration')
        return None


async def get_key_details_from_host(key_data: dict) -> dict | None:
    host = get_host(key_data.get('host_name'))
    if not host:
        return None
    try:
        base, context = _session(host)
        async with context as session:
            if not await _login_to_rixxx(session, base, host['host_username'], host['host_pass']):
                return None
            link = await _sub_link(session, base, key_data['xui_client_uuid'])
            return {'connection_string': link} if link else None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError):
        logger.error('Could not read RIXXX subscription')
        return None


async def delete_client_on_host(host_name: str, client_email: str) -> bool:
    host = get_host(host_name)
    if not host:
        return False
    try:
        base, context = _session(host)
        async with context as session:
            if not await _login_to_rixxx(session, base, host['host_username'], host['host_pass']):
                return False
            user = _find(await _users(session, base), client_email)
            # Panel supports email fallback if the hub user has already expired.
            user_id = user['id'] if user else 'deleted'
            if not await _federate(session, base, user_id, 'undeploy', client_email):
                return False
            if not user:
                return True
            try:
                async with session.delete(f'{base}/api/users/{user_id}', allow_redirects=False) as response:
                    if response.status not in (200, 404):
                        return False
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
                await asyncio.sleep(1)
                if not await _login_to_rixxx(session, base, host['host_username'], host['host_pass']):
                    return False
            return _find(await _users(session, base), client_email) is None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, RuntimeError, KeyError):
        logger.error('RIXXX revocation unconfirmed; retaining local key for retry')
        return False


async def set_bonus_links(key_data: dict, links: list[str]) -> bool:
    """Replace personal bonus URIs; the same /sub/token serves the updated list."""
    allowed = {'vless', 'vmess', 'trojan', 'ss', 'ssr', 'naive+https', 'https', 'mierus', 'mieru', 'hysteria2', 'hy2', 'tuic'}
    if len(links) > 30 or any(len(link) > 4096 or urlsplit(link).scheme not in allowed for link in links):
        return False
    host = get_host(key_data.get('host_name'))
    if not host:
        return False
    try:
        base, context = _session(host)
        async with context as session:
            if not await _login_to_rixxx(session, base, host['host_username'], host['host_pass']):
                return False
            async with session.put(f"{base}/api/users/{key_data['xui_client_uuid']}/bonus-links",
                                   json={'links': links}, allow_redirects=False) as response:
                return response.status == 200
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError):
        logger.error('Could not update RIXXX bonus links')
        return False
