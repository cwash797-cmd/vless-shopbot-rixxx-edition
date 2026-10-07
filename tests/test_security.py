"""Offline regression tests using real Flask, aiogram and SQLite, not live accounts."""
import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update, User
from werkzeug.security import check_password_hash

from shop_bot.data_manager import database as db
from shop_bot.modules import lava_api
from shop_bot.bot import handlers
from shop_bot.bot.middlewares import BanMiddleware
from shop_bot.webhook_server.app import create_webhook_app


class DatabaseFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.db_patch = patch.object(db, 'DB_FILE', Path(self.temp.name) / 'test.db')
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        env = patch.dict(os.environ, {'ADMIN_PASSWORD': 'a-long-test-password', 'COOKIE_SECURE': 'false',
                                    'FLASK_SECRET_KEY': '', 'ADMIN_TOTP_SECRET': ''})
        env.start()
        self.addCleanup(env.stop)
        db.initialize_db()
        db.register_user_if_not_exists(42, 'buyer', None)
        db.register_user_if_not_exists(1, 'owner', None)
        db.update_setting('admin_telegram_id', '1')

    def invoice(self, payment_id='invoice-1', method='Lava.top', user_id=42):
        metadata = {'user_id': user_id, 'months': 1, 'price': 150, 'action': 'new',
                    'key_id': 0, 'host_name': 'Hub', 'plan_id': 1, 'payment_method': method}
        self.assertTrue(db.create_pending_transaction(payment_id, user_id, 150, metadata))
        return metadata


class DatabaseSecurityTests(DatabaseFixture, unittest.TestCase):
    def test_database_file_is_owner_only(self):
        self.assertEqual(db.DB_FILE.stat().st_mode & 0o777, 0o600)

    def test_password_is_hashed_and_survives_restart(self):
        saved = db.get_setting('panel_password')
        self.assertNotEqual(saved, os.environ['ADMIN_PASSWORD'])
        self.assertTrue(check_password_hash(saved, os.environ['ADMIN_PASSWORD']))
        db.initialize_db()
        self.assertEqual(saved, db.get_setting('panel_password'))

    def test_default_admin_migration_requires_new_password(self):
        db.update_setting('panel_password', 'admin')
        with patch.dict(os.environ, {'ADMIN_PASSWORD': ''}):
            with self.assertRaises(ValueError):
                db.initialize_db()
        db.initialize_db()
        self.assertTrue(check_password_hash(db.get_setting('panel_password'), 'a-long-test-password'))

    def test_concurrent_claim_has_exactly_one_winner(self):
        self.invoice()
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: db.find_and_complete_pending_transaction('invoice-1'), range(30)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(db.get_payment('invoice-1')['status'], 'processing')

    def test_wrong_provider_cannot_consume_invoice(self):
        self.invoice()
        self.assertIsNone(db.find_and_complete_pending_transaction('invoice-1', 'CryptoBot'))
        self.assertEqual(db.get_payment('invoice-1')['status'], 'pending')

    def test_wrong_owner_cannot_consume_invoice(self):
        self.invoice()
        self.assertIsNone(db.find_and_complete_pending_transaction('invoice-1', user_id=999))
        self.assertEqual(db.get_payment('invoice-1')['status'], 'pending')

    def test_failure_is_retained_for_review(self):
        self.invoice()
        db.find_and_complete_pending_transaction('invoice-1')
        db.finish_payment('invoice-1', False)
        self.assertEqual(db.get_payment('invoice-1')['status'], 'review')
        self.assertIsNone(db.find_and_complete_pending_transaction('invoice-1'))

    def test_ton_json_is_never_payment_proof(self):
        self.invoice(method='TON Connect')
        self.assertIsNone(db.find_and_complete_ton_transaction('invoice-1', 999))
        self.assertEqual(db.get_payment('invoice-1')['status'], 'pending')

    def test_verified_inbox_survives_restart_and_claims_once(self):
        self.invoice()
        db.mark_payment_verified('invoice-1')
        db.initialize_db()
        self.assertEqual(len(db.get_verified_payments()), 1)
        self.assertIsNotNone(db.find_and_complete_pending_transaction('invoice-1'))
        self.assertEqual(db.get_verified_payments(), [])
        self.assertIsNone(db.find_and_complete_pending_transaction('invoice-1'))

    def test_trial_claim_has_one_winner(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: db.claim_trial(42), range(20)))
        self.assertEqual(sum(results), 1)

    def test_generated_new_emails_do_not_collide_after_deletion(self):
        first = handlers.generate_client_email(42, 1, 'Финляндия')
        second = handlers.generate_client_email(42, 1, 'Финляндия')
        self.assertNotEqual(first, second)
        self.assertTrue(first.isascii())



class WebSecurityTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.controller = MagicMock()
        self.app = create_webhook_app(self.controller)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def csrf(self):
        self.client.get('/login')
        with self.client.session_transaction() as session:
            return session['csrf_token']

    def login(self):
        return self.client.post('/login', data={'username': 'admin', 'password': 'a-long-test-password', 'csrf_token': self.csrf()})

    def test_random_secret_is_not_reused(self):
        self.assertNotEqual(self.app.secret_key, create_webhook_app(self.controller).secret_key)

    def test_known_secret_rejected(self):
        with patch.dict(os.environ, {'FLASK_SECRET_KEY': 'vless-shopbot-secret-key-change-me'}):
            with self.assertRaises(ValueError):
                create_webhook_app(self.controller)

    def test_forged_login_flag_is_not_enough(self):
        with self.client.session_transaction() as session:
            session['logged_in'] = True
        self.assertEqual(self.client.get('/settings').status_code, 302)

    def test_no_csrf_no_login(self):
        self.assertEqual(self.client.post('/login', data={'username': 'admin', 'password': 'a-long-test-password'}).status_code, 400)

    def test_login_and_password_rotation_invalidates_session(self):
        self.assertEqual(self.login().status_code, 302)
        self.assertEqual(self.client.get('/settings').status_code, 200)
        db.update_setting('panel_password', 'different-hash')
        self.assertEqual(self.client.get('/settings').status_code, 302)

    def test_mutation_needs_csrf_even_when_authenticated(self):
        self.login()
        self.assertEqual(self.client.post('/users/ban/42').status_code, 400)
        self.assertFalse(db.get_user(42)['is_banned'])

    def test_login_rate_limit(self):
        csrf = self.csrf()
        for _ in range(10):
            self.client.post('/login', data={'username': 'admin', 'password': 'bad', 'csrf_token': csrf})
        self.assertEqual(self.client.post('/login', data={'csrf_token': csrf}).status_code, 429)

    def test_totp_required_when_configured(self):
        import pyotp
        secret = pyotp.random_base32()
        with patch.dict(os.environ, {'ADMIN_TOTP_SECRET': secret}):
            self.login()
            self.assertEqual(self.client.get('/settings').status_code, 302)
            response = self.client.post('/login', data={'username': 'admin', 'password': 'a-long-test-password',
                                                       'csrf_token': self.csrf(), 'otp': pyotp.TOTP(secret).now()})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self.client.get('/settings').status_code, 200)

    def test_lava_missing_secret_fails_closed(self):
        self.assertFalse(lava_api.verify_webhook_auth({}, None))
        self.assertFalse(lava_api.verify_webhook_auth({'Authorization': 'Basic prefix-secret-suffix'}, 'secret'))
        self.assertEqual(self.client.post('/lava-webhook', json={'eventType': 'payment.success'}).status_code, 403)

    def test_worker_down_preserves_pending_invoice(self):
        self.invoice()
        db.update_setting('lava_webhook_key', 'test-webhook-key')
        response = self.client.post('/lava-webhook', json={'eventType': 'payment.success', 'contractId': 'invoice-1', 'status': 'completed', 'amount': 150, 'currency': 'RUB'},
                                    headers={'X-Api-Key': 'test-webhook-key'})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(db.get_payment('invoice-1')['status'], 'pending')

    def test_lava_signed_webhook_wrong_amount_or_currency_rejected(self):
        self.invoice()
        db.update_setting('lava_webhook_key', 'test-webhook-key')
        for amount, currency in [(1, 'RUB'), (150, 'USD'), ('NaN', 'RUB')]:
            response = self.client.post('/lava-webhook', json={'eventType': 'payment.success',
                'contractId': 'invoice-1', 'status': 'completed', 'amount': amount, 'currency': currency},
                headers={'X-Api-Key': 'test-webhook-key'})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(db.get_payment('invoice-1')['status'], 'pending')

    def test_lava_event_name_without_success_status_is_not_payment(self):
        self.invoice()
        db.update_setting('lava_webhook_key', 'test-webhook-key')
        response = self.client.post('/lava-webhook', json={'eventType': 'payment.success',
            'contractId': 'invoice-1', 'status': 'failed', 'amount': 150, 'currency': 'RUB'},
            headers={'X-Api-Key': 'test-webhook-key'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(db.get_payment('invoice-1')['status'], 'pending')

    def test_duplicate_paid_webhook_acknowledged_without_worker(self):
        self.invoice()
        db.update_setting('lava_webhook_key', 'test-webhook-key')
        db.find_and_complete_pending_transaction('invoice-1')
        db.finish_payment('invoice-1', True)
        response = self.client.post('/lava-webhook', json={'eventType': 'payment.success', 'contractId': 'invoice-1', 'status': 'completed', 'amount': 150, 'currency': 'RUB'},
                                    headers={'X-Api-Key': 'test-webhook-key'})
        self.assertEqual(response.status_code, 200)

    def test_crypto_unsigned_callback_rejected(self):
        db.update_setting('cryptobot_token', 'test-token')
        self.assertEqual(self.client.post('/cryptobot-webhook', json={'update_type': 'invoice_paid'}).status_code, 403)

    def crypto_post(self, amount='150', fiat='RUB', payload_extra=None):
        db.update_setting('cryptobot_token', 'test-token')
        payload = {'invoice_id': 'invoice-1', 'status': 'paid', 'currency_type': 'fiat', 'fiat': fiat, 'amount': amount}
        payload.update(payload_extra or {})
        body = json.dumps({'update_type': 'invoice_paid', 'payload': payload}).encode()
        signature = hmac.new(hashlib.sha256(b'test-token').digest(), body, hashlib.sha256).hexdigest()
        return self.client.post('/cryptobot-webhook', data=body, content_type='application/json',
                                headers={'crypto-pay-api-signature': signature})

    def test_crypto_wrong_amount_and_currency_rejected(self):
        self.invoice(method='CryptoBot')
        self.assertEqual(self.crypto_post('1').status_code, 403)
        self.assertEqual(self.crypto_post('150', 'USD').status_code, 403)
        self.assertEqual(self.crypto_post('NaN').status_code, 403)
        self.assertEqual(db.get_payment('invoice-1')['status'], 'pending')

    def test_crypto_verified_but_offline_retry(self):
        self.invoice(method='CryptoBot')
        self.assertEqual(self.crypto_post(payload_extra={'payload': 'forged metadata ignored'}).status_code, 503)
        self.assertEqual(db.get_payment('invoice-1')['status'], 'pending')

    def test_yookassa_does_not_trust_callback_metadata(self):
        db.update_setting('yookassa_shop_id', 'test-shop')
        db.update_setting('yookassa_secret_key', 'test-secret')
        payment = SimpleNamespace(id='invoice-1', paid=False, status='pending')
        with patch('shop_bot.webhook_server.app.Payment.find_one', return_value=payment):
            response = self.client.post('/yookassa-webhook', json={'event': 'payment.succeeded',
                'object': {'id': 'invoice-1', 'metadata': {'user_id': 999, 'months': 120}}})
        self.assertEqual(response.status_code, 403)

    def test_yookassa_checks_verified_amount(self):
        self.invoice(method='YooKassa')
        db.update_setting('yookassa_shop_id', 'test-shop')
        db.update_setting('yookassa_secret_key', 'test-secret')
        payment = SimpleNamespace(id='invoice-1', paid=True, status='succeeded', amount=SimpleNamespace(value='1', currency='RUB'))
        with patch('shop_bot.webhook_server.app.Payment.find_one', return_value=payment):
            response = self.client.post('/yookassa-webhook', json={'event': 'payment.succeeded', 'object': {'id': 'invoice-1'}})
        self.assertEqual(response.status_code, 403)

    def test_disabled_ton_does_not_settle(self):
        self.invoice(method='TON Connect')
        self.assertEqual(self.client.post('/ton-webhook', json={'tx_id': 'fake'}).status_code, 503)
        self.assertEqual(db.get_payment('invoice-1')['status'], 'pending')

    def test_invalid_json_and_oversize_requests(self):
        self.assertEqual(self.client.post('/yookassa-webhook', json=[]).status_code, 400)
        self.assertEqual(self.client.post('/yookassa-webhook', data='x' * 300000, content_type='application/json').status_code, 413)

    def test_failed_web_revocation_preserves_key(self):
        db.add_new_key(42, 'Hub', 'u1', 'test@bot', 4102444800000)
        self.login()
        with self.client.session_transaction() as session:
            csrf = session.setdefault('csrf_token', 'test-csrf')
        with patch.object(handlers.xui_api, 'delete_client_on_host', new=AsyncMock(return_value=False)):
            response = self.client.post('/users/revoke/42', data={'csrf_token': csrf})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(db.get_user_keys(42)), 1)

    def test_signed_heleket_wrong_amount_rejected(self):
        import base64
        self.invoice(method='Heleket')
        db.update_setting('heleket_api_key', 'heleket-test')
        data = {'status': 'paid', 'order_id': 'invoice-1', 'amount': '1', 'currency': 'RUB'}
        serialized = json.dumps(data, separators=(',', ':'))
        data['sign'] = hashlib.md5((base64.b64encode(serialized.encode()).decode() + 'heleket-test').encode()).hexdigest()
        response = self.client.post('/heleket-webhook', data=json.dumps(data), content_type='application/json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(db.get_payment('invoice-1')['status'], 'pending')

    def test_heleket_unsigned_rejected(self):
        db.update_setting('heleket_api_key', 'heleket-test')
        self.assertEqual(self.client.post('/heleket-webhook', json={'status': 'paid'}).status_code, 403)

    def test_signed_heleket_valid_payment_requests_retry_when_offline(self):
        import base64
        self.invoice(method='Heleket')
        db.update_setting('heleket_api_key', 'heleket-test')
        data = {'status': 'paid', 'order_id': 'invoice-1', 'amount': '150', 'currency': 'RUB'}
        serialized = json.dumps(data, separators=(',', ':'))
        data['sign'] = hashlib.md5((base64.b64encode(serialized.encode()).decode() + 'heleket-test').encode()).hexdigest()
        response = self.client.post('/heleket-webhook', data=json.dumps(data), content_type='application/json')
        self.assertEqual(response.status_code, 503)

    def test_accepted_lava_webhook_persists_recoverable_inbox(self):
        self.invoice()
        db.update_setting('lava_webhook_key', 'test-webhook-key')
        self.app.config['EVENT_LOOP'] = MagicMock()
        def fake_schedule(coro, loop):
            coro.close()  # Simulate process stopping before the worker executes.
            return MagicMock()
        with patch('shop_bot.webhook_server.app.asyncio.run_coroutine_threadsafe', side_effect=fake_schedule):
            response = self.client.post('/lava-webhook', json={'eventType': 'payment.success', 'contractId': 'invoice-1', 'status': 'completed', 'amount': 150, 'currency': 'RUB'},
                                        headers={'X-Api-Key': 'test-webhook-key'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(db.get_payment('invoice-1')['status'], 'verified')

    def test_dashboard_shows_reconciliation_status_and_invoice_id(self):
        self.invoice()
        db.find_and_complete_pending_transaction('invoice-1')
        db.finish_payment('invoice-1', False)
        self.login()
        response = self.client.get('/dashboard')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'review', response.data)
        self.assertIn(b'invoice-1', response.data)

    def test_negative_plan_rejected(self):
        self.login()
        with self.client.session_transaction() as session:
            csrf = session.setdefault('csrf_token', 'test-csrf')
        response = self.client.post('/add-plan', data={'csrf_token': csrf, 'months': '-1', 'price': '150'})
        self.assertEqual(response.status_code, 400)



class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if hasattr(method, 'chat_id'):
            return Message(message_id=123, date=datetime.now(timezone.utc), chat=Chat(id=int(method.chat_id), type='private'),
                           text=getattr(method, 'text', '')).as_(bot)
        return True

    async def stream_content(self, *args, **kwargs):
        yield b''


class BotSecurityTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = RecordingSession()
        self.bot = Bot('123456789:' + 'A' * 35, session=self.session)
        self.dp = Dispatcher()
        middleware = BanMiddleware()
        self.dp.message.outer_middleware(middleware)
        self.dp.callback_query.outer_middleware(middleware)
        self.router = handlers.get_user_router()
        self.dp.include_router(self.router)

    async def command(self, text, user_id=1, chat_type='private'):
        message = Message(message_id=1, date=datetime.now(timezone.utc),
            chat=Chat(id=user_id, type=chat_type), from_user=User(id=user_id, is_bot=False, first_name='Tester'), text=text,
            entities=[MessageEntity(type='bot_command', offset=0, length=len(text.split()[0]))])
        return await self.dp.feed_update(self.bot, Update(update_id=1, message=message))

    async def test_admin_commands_are_not_swallowed(self):
        await self.command('/grant 42')
        self.assertTrue(db.get_user(42)['can_give'])
        await self.command('/revoke 42')
        self.assertFalse(db.get_user(42)['can_give'])

    async def test_non_owner_cannot_grant_privileges(self):
        await self.command('/grant 42', user_id=42)
        self.assertFalse(db.get_user(42)['can_give'])

    async def test_admin_commands_are_private_only(self):
        await self.command('/grant 42', chat_type='supergroup')
        self.assertFalse(db.get_user(42)['can_give'])

    async def test_banned_support_cannot_issue(self):
        db.set_user_give_permission(42, True)
        db.ban_user(42)
        with patch.object(handlers.xui_api, 'create_or_update_key_on_host', new_callable=AsyncMock) as create:
            await self.command('/give 1 30', user_id=42)
        create.assert_not_awaited()

    async def test_give_rejects_invalid_duration(self):
        with patch.object(handlers.xui_api, 'create_or_update_key_on_host', new_callable=AsyncMock) as create:
            for days in ('-1', '0', '36501', 'bad'):
                await self.command('/give 42 ' + days)
        create.assert_not_awaited()

    async def test_malformed_admin_id_does_not_crash(self):
        for name in ('grant', 'revoke', 'ban', 'unban', 'delete_user'):
            await self.command('/' + name + ' bad')

    async def test_referral_rate_must_be_finite_and_bounded(self):
        for rate in ('NaN', 'inf', '-1', '101'):
            await self.command('/setref 42 ' + rate)
            self.assertIsNone(db.get_user(42)['custom_referral_percentage'])

    async def test_delete_failure_keeps_user_and_keys(self):
        db.add_new_key(42, 'Hub', 'u1', 'test@bot', 4102444800000)
        with patch.object(handlers.xui_api, 'delete_client_on_host', new=AsyncMock(return_value=False)):
            await self.command('/delete_user 42')
        self.assertIsNotNone(db.get_user(42))
        self.assertEqual(len(db.get_user_keys(42)), 1)

    async def test_duplicate_payment_issues_once(self):
        self.invoice()
        with patch.object(handlers, 'process_successful_payment', new=AsyncMock(return_value=True)) as issue:
            results = await asyncio.gather(*(handlers.deliver_payment(self.bot, 'invoice-1', 'Lava.top') for _ in range(10)))
        self.assertEqual(sum(results), 1)
        issue.assert_awaited_once()
        self.assertEqual(db.get_payment('invoice-1')['status'], 'paid')

    async def test_paid_invoice_failure_is_not_lost(self):
        self.invoice()
        with patch.object(handlers, 'process_successful_payment', new=AsyncMock(side_effect=RuntimeError('panel offline'))):
            self.assertFalse(await handlers.deliver_payment(self.bot, 'invoice-1', 'Lava.top'))
        self.assertEqual(db.get_payment('invoice-1')['status'], 'review')

    async def test_bonus_update_is_owner_only(self):
        key_id = db.add_new_key(42, 'Hub', 'u1', 'test@bot', 4102444800000)
        with patch.object(handlers.xui_api, 'set_bonus_links', new=AsyncMock(return_value=True)) as update:
            await self.command(f'/bonus {key_id} vless://example', user_id=42)
            update.assert_not_awaited()
            await self.command(f'/bonus {key_id} vless://example')
            update.assert_awaited_once()
            self.assertEqual(update.call_args.args[1], ['vless://example'])

    async def callback(self, value, user_id=42):
        message = Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=user_id, type='private'), text='Menu')
        callback = CallbackQuery(id='test-callback', from_user=User(id=user_id, is_bot=False, first_name='Buyer'),
                                 chat_instance='test', message=message, data=value)
        await self.dp.feed_update(self.bot, Update(update_id=2, callback_query=callback))

    async def test_direct_trial_callback_cannot_bypass_used_flag(self):
        db.set_terms_agreed(42)
        db.set_trial_used(42)
        db.create_host('Hub', 'https://hub.example', 'admin', 'pass', 1)
        with patch.object(handlers.xui_api, 'create_or_update_key_on_host', new_callable=AsyncMock) as create:
            await self.callback('select_host_trial_Hub')
        create.assert_not_awaited()

    async def test_direct_trial_callback_respects_disabled_setting(self):
        db.set_terms_agreed(42)
        db.update_setting('trial_enabled', 'false')
        with patch.object(handlers.xui_api, 'create_or_update_key_on_host', new_callable=AsyncMock) as create:
            await self.callback('select_host_trial_Hub')
        create.assert_not_awaited()

    async def test_forged_plan_host_pair_rejected(self):
        db.set_terms_agreed(42)
        db.create_host('Cheap', 'https://hub.example', 'admin', 'pass', 1)
        db.create_plan('Cheap', 'Monthly', 1, 10)
        plan = db.get_plans_for_host('Cheap')[0]
        await self.callback(f"buy_Expensive_{plan['plan_id']}_new_0")
        context = self.dp.fsm.get_context(bot=self.bot, chat_id=42, user_id=42)
        self.assertIsNone(await context.get_state())

    async def test_blocked_telegram_dm_does_not_block_paid_issuance(self):
        metadata = self.invoice()
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=RuntimeError('DM blocked'))
        result = {'client_uuid': 'u1', 'email': 'test@bot', 'expiry_timestamp_ms': 4102444800000,
                  'connection_string': 'https://sub.example/sub/test', 'federation_ok': True}
        with patch.object(handlers.xui_api, 'create_or_update_key_on_host', new=AsyncMock(return_value=result)):
            self.assertTrue(await handlers.deliver_payment(bot, 'invoice-1', 'Lava.top'))
        self.assertEqual(db.get_payment('invoice-1')['status'], 'paid')
        self.assertEqual(len(db.get_user_keys(42)), 1)
        self.assertEqual(db.get_user(42)['total_spent'], 150)
        self.assertEqual(len(db.get_recent_transactions()), 1)

    async def test_renewal_updates_key_and_keeps_bonus_subscription(self):
        key_id = db.add_new_key(42, 'Hub', 'u1', 'test@bot', 4102444800000)
        metadata = {'user_id': 42, 'months': 1, 'price': 150, 'action': 'extend', 'key_id': key_id,
                    'host_name': 'Hub', 'plan_id': 1, 'payment_method': 'Lava.top'}
        db.create_pending_transaction('renewal', 42, 150, metadata)
        result = {'client_uuid': 'u1', 'email': 'test@bot', 'expiry_timestamp_ms': 4105036800000,
                  'connection_string': 'https://sub.example/sub/same-token', 'federation_ok': True}
        with patch.object(handlers.xui_api, 'create_or_update_key_on_host', new=AsyncMock(return_value=result)) as update:
            self.assertTrue(await handlers.deliver_payment(self.bot, 'renewal', 'Lava.top'))
            self.assertEqual(update.call_args.kwargs['email'], 'test@bot')
        self.assertEqual(len(db.get_user_keys(42)), 1)
        self.assertEqual(db.get_key_by_id(key_id)['xui_client_uuid'], 'u1')


    async def test_lava_poll_does_not_confirm_wrong_amount(self):
        self.invoice()
        result = {'is_paid': True, 'amount': 1, 'currency': 'RUB'}
        with patch.object(lava_api, 'get_invoice_status', new=AsyncMock(return_value=result)):
            await self.callback('check_lava_invoice-1')
        self.assertEqual(db.get_payment('invoice-1')['status'], 'pending')

    async def test_lava_poll_confirms_matching_receipt(self):
        self.invoice()
        result = {'is_paid': True, 'amount': 150, 'currency': 'RUB'}
        with patch.object(lava_api, 'get_invoice_status', new=AsyncMock(return_value=result)), \
             patch.object(handlers, 'process_successful_payment', new=AsyncMock(return_value=True)) as issue:
            await self.callback('check_lava_invoice-1')
        issue.assert_awaited_once()
        self.assertEqual(db.get_payment('invoice-1')['status'], 'paid')



if __name__ == '__main__':
    unittest.main()
