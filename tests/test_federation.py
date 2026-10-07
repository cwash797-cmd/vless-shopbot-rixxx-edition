"""RIXXX API contracts taken from panel/server/index.js (v1.11).

No live panel or production users are touched. Transport responses are controlled
here; these tests do not claim to verify actual VPN connectivity.
"""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from yarl import URL
from shop_bot.modules import xui_api


class Response:
    def __init__(self, status, data):
        self.status, self.data = status, data

    async def json(self):
        return self.data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Panel:
    def __init__(self):
        self.users = []
        self.calls = []
        self.list_status = 200
        self.login_status = 200
        self.fed_status = 200
        self.fed_data = {'ok': True, 'results': [{'name': 'peer', 'ok': True}]}
        self.link = 'https://sub.example/sub/same-token'
        self.disconnect_update = False
        self.disconnect_delete = False
        self.delete_persists = True
        self.bonus = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, url, **kwargs):
        self.calls.append(('POST', url, kwargs))
        if url.endswith('/api/login'):
            return Response(self.login_status, {'ok': self.login_status == 200})
        if '/federation/' in url:
            return Response(self.fed_status, self.fed_data)
        if url.endswith('/api/users'):
            user = {'id': 'u1', **kwargs['json']}
            self.users.append(user)
            return Response(201, dict(user))
        raise AssertionError(url)

    def get(self, url, **kwargs):
        self.calls.append(('GET', url, kwargs))
        if url.endswith('/api/users'):
            return Response(self.list_status, [dict(u) for u in self.users])
        if url.endswith('/sub-link'):
            return Response(200, {'link': self.link})
        raise AssertionError(url)

    def put(self, url, **kwargs):
        self.calls.append(('PUT', url, kwargs))
        if url.endswith('/bonus-links'):
            self.bonus = kwargs['json']['links']
            return Response(200, {'links': self.bonus})
        self.users[0].update(kwargs['json'])
        if self.disconnect_update:
            raise aiohttp.ServerDisconnectedError()
        return Response(200, self.users[0])

    def delete(self, url, **kwargs):
        self.calls.append(('DELETE', url, kwargs))
        if self.delete_persists:
            self.users.clear()
        if self.disconnect_delete:
            raise aiohttp.ServerDisconnectedError()
        return Response(200, {'ok': True})


class TestRixxxFederation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.panel = Panel()
        self.host = {'host_url': 'https://proxy:secret@hub.example/hidden/', 'host_username': 'admin', 'host_pass': 'password'}
        self.email = 'test_key1@hub.bot'
        for target, value in [('shop_bot.modules.xui_api.get_host', self.host),
                              ('shop_bot.modules.xui_api._session', ('https://hub.example/hidden', self.panel))]:
            mock = patch(target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    async def create(self):
        return await xui_api.create_or_update_key_on_host('Hub', self.email, 30)

    def existing(self, expiry=None):
        self.panel.users = [{'id': 'u1', 'username': xui_api._username(self.email), 'email': self.email,
                             'expiry': expiry or (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()}]

    async def test_create_deploys_and_returns_panel_subscription(self):
        result = await self.create()
        self.assertEqual(result['connection_string'], self.panel.link)
        self.assertTrue(result['federation_ok'])
        self.assertTrue(any('/federation/deploy' in url for _, url, _ in self.panel.calls))
        self.assertEqual(self.panel.users[0]['protocols'], ['naive', 'mieru', 'hy2'])

    async def test_active_renewal_extends_existing_expiry(self):
        old = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.existing(old.isoformat())
        result = await self.create()
        self.assertEqual(result['expiry_timestamp_ms'], int((old + timedelta(days=30)).timestamp() * 1000))
        self.assertEqual(len(self.panel.users), 1)
        self.assertEqual(result['connection_string'], self.panel.link)

    async def test_expired_renewal_starts_from_now(self):
        self.existing('2020-01-01T00:00:00Z')
        now = datetime.now(timezone.utc)
        result = await self.create()
        self.assertAlmostEqual(result['expiry_timestamp_ms'] / 1000, (now + timedelta(days=30)).timestamp(), delta=2)

    async def test_disconnected_update_is_read_back_not_extended_twice(self):
        old = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.existing(old.isoformat())
        self.panel.disconnect_update = True
        with patch('shop_bot.modules.xui_api.asyncio.sleep', new=AsyncMock()):
            result = await self.create()
        self.assertIsNotNone(result)
        self.assertEqual(result['expiry_timestamp_ms'], int((old + timedelta(days=30)).timestamp() * 1000))
        self.assertEqual(sum(method == 'PUT' for method, _, _ in self.panel.calls), 1)

    async def test_failed_user_list_never_creates(self):
        self.panel.list_status = 503
        self.assertIsNone(await self.create())
        self.assertEqual(self.panel.users, [])

    async def test_failed_login_never_creates(self):
        self.panel.login_status = 401
        self.assertIsNone(await self.create())
        self.assertEqual(self.panel.users, [])

    async def test_partial_deployment_is_reported(self):
        self.panel.fed_data['results'][0]['ok'] = False
        result = await self.create()
        self.assertIsNotNone(result)
        self.assertFalse(result['federation_ok'])

    async def test_no_federation_is_valid_single_node(self):
        self.panel.fed_status = 400
        self.panel.fed_data = {'error': xui_api.NO_FEDERATION}
        self.assertTrue((await self.create())['federation_ok'])

    async def test_other_400_is_not_silently_accepted(self):
        self.panel.fed_status = 400
        self.panel.fed_data = {'error': 'User has no email'}
        self.assertFalse((await self.create())['federation_ok'])

    async def test_successful_revoke_is_verified(self):
        self.existing()
        self.assertTrue(await xui_api.delete_client_on_host('Hub', self.email))
        paths = [url for _, url, _ in self.panel.calls]
        self.assertTrue(any('/federation/undeploy' in url for url in paths))
        self.assertEqual(self.panel.users, [])

    async def test_partial_undeploy_does_not_delete_hub_identity(self):
        self.existing()
        self.panel.fed_data['results'][0]['ok'] = False
        self.assertFalse(await xui_api.delete_client_on_host('Hub', self.email))
        self.assertEqual(len(self.panel.users), 1)
        self.assertFalse(any(method == 'DELETE' for method, _, _ in self.panel.calls))

    async def test_missing_hub_user_still_revokes_peers_by_email(self):
        self.assertTrue(await xui_api.delete_client_on_host('Hub', self.email))
        call = next(c for c in self.panel.calls if '/federation/undeploy' in c[1])
        self.assertEqual(call[2]['json'], {'email': self.email})

    async def test_failed_list_is_not_successful_delete(self):
        self.panel.list_status = 503
        self.assertFalse(await xui_api.delete_client_on_host('Hub', self.email))

    async def test_disconnect_is_not_successful_delete_without_readback(self):
        self.existing()
        self.panel.disconnect_delete = True
        self.panel.delete_persists = False
        with patch('shop_bot.modules.xui_api.asyncio.sleep', new=AsyncMock()):
            self.assertFalse(await xui_api.delete_client_on_host('Hub', self.email))

    async def test_admin_credentials_cannot_be_returned_as_subscription(self):
        self.panel.link = 'https://admin:password@hub.example/sub/token'
        self.assertIsNone(await self.create())

    async def test_bonus_links_replace_list_without_changing_subscription(self):
        key = {'host_name': 'Hub', 'xui_client_uuid': 'u1'}
        self.assertTrue(await xui_api.set_bonus_links(key, ['vless://example']))
        self.assertEqual(self.panel.bonus, ['vless://example'])
        self.assertEqual((await xui_api.get_key_details_from_host(key))['connection_string'], self.panel.link)
        self.assertTrue(await xui_api.set_bonus_links(key, []))
        self.assertEqual(self.panel.bonus, [])

    async def test_unsafe_bonus_scheme_rejected(self):
        self.assertFalse(await xui_api.set_bonus_links({'host_name': 'Hub'}, ['javascript:alert(1)']))

    async def test_invalid_duration_does_not_call_panel(self):
        for days in (0, -1, 36501):
            self.assertIsNone(await xui_api.create_or_update_key_on_host('Hub', self.email, days))
        self.assertEqual(self.panel.calls, [])


class SessionContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_ip_cookie_jar_and_basic_auth(self):
        base, session = xui_api._session({'host_url': 'http://proxy:p%40ss@127.0.0.1:3000/prefix/'})
        async with session:
            self.assertEqual(base, 'http://127.0.0.1:3000/prefix')
            self.assertEqual(session.auth.password, 'p@ss')
            session.cookie_jar.update_cookies({'connect.sid': 'test'}, URL(base))
            self.assertIn('connect.sid', session.cookie_jar.filter_cookies(URL(base)))
            self.assertEqual(session.timeout.connect, 10)

    async def test_bad_host_scheme_rejected(self):
        with self.assertRaises(ValueError):
            xui_api._session({'host_url': 'file:///etc/passwd'})

    def test_unicode_username_is_valid_and_deterministic(self):
        value = xui_api._username('user@Финляндия.bot')
        self.assertRegex(value, r'^[a-zA-Z0-9_-]{1,64}$')
        self.assertEqual(value, xui_api._username('user@Финляндия.bot'))


if __name__ == '__main__':
    unittest.main()
