import os
import logging
import asyncio
import json
import hmac
from decimal import Decimal, InvalidOperation
from yookassa import Payment
import hashlib
import base64
import secrets
import time
from urllib.parse import urlsplit, urlunsplit
import math
import threading
import pyotp
from werkzeug.security import check_password_hash, generate_password_hash
from hmac import compare_digest
from datetime import datetime, timedelta
from functools import wraps
from math import ceil
from flask import Flask, request, render_template, redirect, url_for, flash, session, current_app, abort

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from shop_bot.modules import xui_api, lava_api
from shop_bot.bot import handlers 
from shop_bot.data_manager.database import (
    get_all_settings, update_setting, get_all_hosts, get_plans_for_host,
    create_host, delete_host, create_plan, delete_plan, get_user_count,
    get_total_keys_count, get_total_spent_sum, get_daily_stats_for_charts,
    get_recent_transactions, get_paginated_transactions, get_all_users, get_user_keys,
    ban_user, unban_user, delete_user_keys, get_setting, find_and_complete_ton_transaction,
    find_and_complete_pending_transaction, get_payment, mark_payment_verified
)

_bot_controller = None

ALL_SETTINGS_KEYS = [
    "panel_login", "panel_password", "about_text", "terms_url", "privacy_url",
    "android_url", "ios_url", "windows_url", "linux_url",
    "support_user", "support_text", "channel_url", "telegram_bot_token",
    "telegram_bot_username", "admin_telegram_id", "yookassa_shop_id",
    "yookassa_secret_key", "sbp_enabled", "receipt_email", "cryptobot_token",
    "heleket_merchant_id", "heleket_api_key", "domain", "referral_percentage",
    "referral_discount", "ton_wallet_address", "tonapi_key", "force_subscription", "trial_enabled", "trial_duration_days", "enable_referrals", "minimum_withdrawal",
    "support_group_id", "support_bot_token",
    "lava_api_key", "lava_offer_id", "lava_webhook_key", "lava_sbp_only"
]

def create_webhook_app(bot_controller_instance):
    global _bot_controller
    _bot_controller = bot_controller_instance

    flask_app = Flask(
        __name__,
        template_folder='templates',
        static_folder='static'
    )
    
    secret = os.environ.get('FLASK_SECRET_KEY', '')
    if secret and (len(secret) < 32 or secret in {
        'vless-shopbot-secret-key-change-me', 'generate_random_secret_key_here'
    }):
        raise ValueError('FLASK_SECRET_KEY must be a random secret of at least 32 characters')
    flask_app.config.update(
        SECRET_KEY=secret or secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Strict',
        SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', 'true').lower() == 'true',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=256 * 1024,
    )
    login_attempts = []
    login_lock = threading.Lock()
    webhook_endpoints = {'yookassa_webhook_handler', 'cryptobot_webhook_handler',
                         'heleket_webhook_handler', 'ton_webhook_handler', 'lava_webhook_handler'}

    def csrf_token():
        if 'csrf_token' not in session:
            session['csrf_token'] = secrets.token_urlsafe(32)
        return session['csrf_token']

    flask_app.jinja_env.globals['csrf_token'] = csrf_token

    @flask_app.before_request
    def protect_forms():
        if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'} and request.endpoint not in webhook_endpoints:
            expected = session.get('csrf_token')
            supplied = request.form.get('csrf_token', '')
            if not expected or not compare_digest(expected.encode(), supplied.encode()):
                abort(400, 'Invalid CSRF token')

    @flask_app.after_request
    def security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        return response
    @flask_app.context_processor
    def inject_current_year():
        return {'current_year': datetime.utcnow().year}

    def login_required(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if session.get('logged_in') is not True or session.get('credential_version') != credential_version():
                return redirect(url_for('login_page'))
            return f(*args, **kwargs)
        return decorated_function

    def credential_version():
        return hashlib.sha256(((get_setting('panel_login') or '') + ':' +
                               (get_setting('panel_password') or '')).encode()).hexdigest()

    @flask_app.route('/login', methods=['GET', 'POST'])
    def login_page():
        settings = get_all_settings()
        if request.method == 'POST':
            with login_lock:
                now = time.monotonic()
                login_attempts[:] = [t for t in login_attempts if now - t < 60]
                if len(login_attempts) >= 10:
                    return 'Too many login attempts; retry in one minute', 429
                login_attempts.append(now)
            password_hash = settings.get('panel_password') or ''
            valid_password = password_hash.startswith(('scrypt:', 'pbkdf2:')) and check_password_hash(
                password_hash, request.form.get('password', ''))
            totp_secret = os.environ.get('ADMIN_TOTP_SECRET')
            valid_totp = not totp_secret or pyotp.TOTP(totp_secret).verify(request.form.get('otp', ''))
            if request.form.get('username') == settings.get('panel_login') and valid_password and valid_totp:
                session.clear()
                session.permanent = True
                session['credential_version'] = credential_version()
                session['logged_in'] = True
                return redirect(url_for('dashboard_page'))
            else:
                flash('Неверный логин или пароль', 'danger')
        return render_template('login.html')

    @flask_app.route('/logout', methods=['POST'])
    @login_required
    def logout_page():
        session.pop('logged_in', None)
        flash('Вы успешно вышли.', 'success')
        return redirect(url_for('login_page'))

    def get_common_template_data():
        bot_status = _bot_controller.get_status()
        settings = get_all_settings()
        required_for_start = ['telegram_bot_token', 'telegram_bot_username', 'admin_telegram_id']
        all_settings_ok = all(settings.get(key) for key in required_for_start)
        return {"bot_status": bot_status, "all_settings_ok": all_settings_ok}

    @flask_app.route('/')
    @login_required
    def index():
        return redirect(url_for('dashboard_page'))

    @flask_app.route('/dashboard')
    @login_required
    def dashboard_page():
        stats = {
            "user_count": get_user_count(),
            "total_keys": get_total_keys_count(),
            "total_spent": get_total_spent_sum(),
            "host_count": len(get_all_hosts())
        }
        
        page = request.args.get('page', 1, type=int)
        per_page = 8
        
        transactions, total_transactions = get_paginated_transactions(page=page, per_page=per_page)
        total_pages = ceil(total_transactions / per_page)
        
        chart_data = get_daily_stats_for_charts(days=30)
        common_data = get_common_template_data()
        
        return render_template(
            'dashboard.html',
            stats=stats,
            chart_data=chart_data,
            transactions=transactions,
            current_page=page,
            total_pages=total_pages,
            **common_data
        )

    @flask_app.route('/users')
    @login_required
    def users_page():
        users = get_all_users()
        for user in users:
            user['user_keys'] = get_user_keys(user['telegram_id'])
        
        common_data = get_common_template_data()
        return render_template('users.html', users=users, **common_data)

    @flask_app.route('/settings', methods=['GET', 'POST'])
    @login_required
    def settings_page():
        if request.method == 'POST':
            for key, low, high in [('referral_percentage', 0, 100), ('referral_discount', 0, 99),
                                   ('trial_duration_days', 1, 36500), ('minimum_withdrawal', 1, 100000000)]:
                if key in request.form:
                    try:
                        value = float(request.form[key])
                    except ValueError:
                        abort(400, f'Invalid {key}')
                    if not math.isfinite(value) or not low <= value <= high or (key == 'trial_duration_days' and not value.is_integer()):
                        abort(400, f'Invalid {key}')
            admin_id = request.form.get('admin_telegram_id')
            if admin_id and (not admin_id.isdigit() or not 0 < int(admin_id) < 2**63):
                abort(400, 'Invalid administrator ID')
            if 'panel_password' in request.form and request.form.get('panel_password'):
                if len(request.form['panel_password']) < 12:
                    abort(400, 'Panel password must contain at least 12 characters')
                update_setting('panel_password', generate_password_hash(request.form['panel_password']))

            for checkbox_key in ['force_subscription', 'sbp_enabled', 'trial_enabled', 'enable_referrals', 'lava_sbp_only']:
                values = request.form.getlist(checkbox_key)
                value = values[-1] if values else 'false'
                update_setting(checkbox_key, 'true' if value == 'true' else 'false')

            for key in ALL_SETTINGS_KEYS:
                if key in ['panel_password', 'force_subscription', 'sbp_enabled', 'trial_enabled', 'enable_referrals', 'lava_sbp_only']:
                    continue
                if key in request.form:
                    value = str(int(float(request.form[key]))) if key == 'trial_duration_days' else request.form[key]
                    update_setting(key, value)

            flash('Настройки успешно сохранены!', 'success')
            return redirect(url_for('settings_page'))

        current_settings = get_all_settings()
        hosts = get_all_hosts()
        for host in hosts:
            host['plans'] = get_plans_for_host(host['host_name'])
            parsed = urlsplit(host['host_url'])
            host['display_url'] = urlunsplit((parsed.scheme, parsed.netloc.rsplit('@', 1)[-1], parsed.path, '', ''))
        
        common_data = get_common_template_data()
        return render_template('settings.html', settings=current_settings, hosts=hosts, **common_data)

    @flask_app.route('/start-shop-bot', methods=['POST'])
    @login_required
    def start_shop_bot_route():
        result = _bot_controller.start_shop_bot()
        flash(result.get('message', 'An error occurred.'), 'success' if result.get('status') == 'success' else 'danger')
        return redirect(request.referrer or url_for('dashboard_page'))

    @flask_app.route('/stop-shop-bot', methods=['POST'])
    @login_required
    def stop_shop_bot_route():
        result = _bot_controller.stop_shop_bot()
        flash(result.get('message', 'An error occurred.'), 'success' if result.get('status') == 'success' else 'danger')
        return redirect(request.referrer or url_for('dashboard_page'))

    @flask_app.route('/start-support-bot', methods=['POST'])
    @login_required
    def start_support_bot_route():
        result = _bot_controller.start_support_bot()
        flash(result.get('message', 'An error occurred.'), 'success' if result.get('status') == 'success' else 'danger')
        return redirect(request.referrer or url_for('dashboard_page'))

    @flask_app.route('/stop-support-bot', methods=['POST'])
    @login_required
    def stop_support_bot_route():
        result = _bot_controller.stop_support_bot()
        flash(result.get('message', 'An error occurred.'), 'success' if result.get('status') == 'success' else 'danger')
        return redirect(request.referrer or url_for('dashboard_page'))

    @flask_app.route('/users/ban/<int:user_id>', methods=['POST'])
    @login_required
    def ban_user_route(user_id):
        if str(user_id) == get_setting('admin_telegram_id'):
            abort(400, 'Cannot ban the owner')
        ban_user(user_id)
        count, total = asyncio.run(handlers.revoke_user_access(user_id))
        flash(f'Доступ к боту заблокирован. Отозвано ключей: {count}/{total}. Неудачные отзывы сохранены для повтора.', 'warning' if count != total else 'success')
        return redirect(url_for('users_page'))

    @flask_app.route('/users/unban/<int:user_id>', methods=['POST'])
    @login_required
    def unban_user_route(user_id):
        unban_user(user_id)
        flash(f'Пользователь {user_id} был разблокирован.', 'success')
        return redirect(url_for('users_page'))

    @flask_app.route('/users/revoke/<int:user_id>', methods=['POST'])
    @login_required
    def revoke_keys_route(user_id):
        count, total = asyncio.run(handlers.revoke_user_access(user_id))
        flash(f'Отозвано ключей: {count}/{total}. Неудачные отзывы сохранены для повтора.', 'success' if count == total else 'warning')

        return redirect(url_for('users_page'))

    @flask_app.route('/add-host', methods=['POST'])
    @login_required
    def add_host_route():
        name = request.form.get('host_name', '').strip()
        parsed = urlsplit(request.form.get('host_url', ''))
        if not name or len(name.encode()) > 26 or parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.query or parsed.fragment:
            abort(400, 'Invalid host name or panel URL')
        if any(h['host_name'] == name for h in get_all_hosts()):
            abort(400, 'Host already exists')
        create_host(
            name=name,
            url=request.form['host_url'],
            user=request.form['host_username'],
            passwd=request.form['host_pass'],
            inbound=int(request.form.get('host_inbound_id') or 1)
        )
        flash(f"Хост '{request.form['host_name']}' успешно добавлен.", 'success')
        return redirect(url_for('settings_page'))

    @flask_app.route('/delete-host/<host_name>', methods=['POST'])
    @login_required
    def delete_host_route(host_name):
        if any(k['host_name'] == host_name for user in get_all_users() for k in get_user_keys(user['telegram_id'])):
            abort(400, 'Revoke host keys successfully before deleting the host')
        delete_host(host_name)
        flash(f"Хост '{host_name}' и все его тарифы были удалены.", 'success')
        return redirect(url_for('settings_page'))

    @flask_app.route('/add-plan', methods=['POST'])
    @login_required
    def add_plan_route():
        try:
            months = int(request.form.get('months', ''))
            price = float(request.form.get('price', ''))
        except ValueError:
            abort(400, 'Invalid plan duration or price')
        if not 1 <= months <= 1200 or not math.isfinite(price) or price <= 0:
            abort(400, 'Invalid plan duration or price')
        if request.form.get('host_name') not in {h['host_name'] for h in get_all_hosts()}:
            abort(400, 'Host not found')
        create_plan(
            host_name=request.form['host_name'],
            plan_name=request.form['plan_name'],
            months=months,
            price=price
        )
        flash(f"Новый тариф для хоста '{request.form['host_name']}' добавлен.", 'success')
        return redirect(url_for('settings_page'))

    @flask_app.route('/delete-plan/<int:plan_id>', methods=['POST'])
    @login_required
    def delete_plan_route(plan_id):
        delete_plan(plan_id)
        flash("Тариф успешно удален.", 'success')
        return redirect(url_for('settings_page'))

    def dispatch_payment(payment_id, method, amount=None, currency=None):
        if not isinstance(payment_id, (str, int)) or not str(payment_id):
            return 'Missing payment ID', 400
        row = get_payment(str(payment_id))
        if not row:
            # Invoice persistence can race a fast callback: ask the provider to retry.
            return 'Unknown invoice', 503
        stored_method = json.loads(row['metadata']).get('payment_method')
        if stored_method == 'Lava.top SBP':
            stored_method = 'Lava.top'
        if stored_method != method:
            return 'Provider mismatch', 403
        if amount is not None:
            try:
                value = Decimal(str(amount))
                expected = Decimal(str(row['amount_rub']))
                if currency != 'RUB' or not value.is_finite() or value != expected:
                    return 'Amount or currency mismatch', 403
            except (ValueError, InvalidOperation):
                return 'Invalid amount', 400
        if row['status'] not in ('pending', 'verified'):
            return {'status': 'ok'}, 200
        bot = _bot_controller.get_bot_instance()
        loop = current_app.config.get('EVENT_LOOP')
        if not bot or not loop or not loop.is_running():
            return 'Payment worker unavailable; retry later', 503
        # Claim inside the coroutine, not before scheduling. If scheduling fails,
        # a verified invoice is also recovered by the scheduler after restart.
        mark_payment_verified(str(payment_id))
        task = handlers.deliver_payment(bot, str(payment_id), method)
        try:
            future = asyncio.run_coroutine_threadsafe(task, loop)
        except RuntimeError:
            task.close()
            return 'Payment worker unavailable', 503
        def report_failure(done):
            try:
                done.result()
            except Exception:
                logger.exception('Payment worker failed; inspect processing/review transactions')
        future.add_done_callback(report_failure)
        return {'status': 'ok'}, 200

    @flask_app.route('/yookassa-webhook', methods=['POST'])
    def yookassa_webhook_handler():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return 'Invalid JSON', 400
        if data.get('event') != 'payment.succeeded':
            return 'OK', 200
        obj = data.get('object')
        payment_id = obj.get('id') if isinstance(obj, dict) else None
        if not isinstance(payment_id, str) or not payment_id:
            return 'Missing payment ID', 400
        if not get_setting('yookassa_shop_id') or not get_setting('yookassa_secret_key'):
            return 'Provider not configured', 503
        try:
            # Ignore webhook metadata completely; retrieve proof from the provider.
            payment = Payment.find_one(payment_id)
            if payment.id != payment_id or payment.status != 'succeeded' or not payment.paid:
                return 'Unconfirmed payment', 403
            return dispatch_payment(payment_id, 'YooKassa', payment.amount.value, payment.amount.currency)
        except Exception:
            logger.warning('YooKassa verification failed')
            return 'Verification unavailable', 503

    @flask_app.route('/cryptobot-webhook', methods=['POST'])
    def cryptobot_webhook_handler():
        token = get_setting('cryptobot_token')
        signature = request.headers.get('crypto-pay-api-signature', '')
        if not token:
            return 'Provider not configured', 503
        key = hashlib.sha256(token.encode()).digest()
        expected = hmac.new(key, request.get_data(), hashlib.sha256).hexdigest()
        if not compare_digest(expected.encode(), signature.encode()):
            return 'Forbidden', 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return 'Invalid JSON', 400
        if data.get('update_type') != 'invoice_paid':
            return 'OK', 200
        invoice = data.get('payload', {})
        if not isinstance(invoice, dict) or invoice.get('status') != 'paid' or invoice.get('currency_type') != 'fiat':
            return 'Invalid invoice', 400
        if invoice.get('amount') is None:
            return 'Missing amount', 400
        return dispatch_payment(invoice.get('invoice_id'), 'CryptoBot', invoice['amount'], invoice.get('fiat'))

    @flask_app.route('/heleket-webhook', methods=['POST'])
    def heleket_webhook_handler():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return 'Invalid JSON', 400
        api_key = get_setting('heleket_api_key')
        if not api_key:
            return 'Provider not configured', 503
        signature = data.pop('sign', None)
        if not isinstance(signature, str):
            return 'Forbidden', 403
        # Heleket signs JSON in received field order, without the sign field.
        encoded = json.dumps(data, separators=(',', ':'), ensure_ascii=False).replace('/', '\\/')
        expected = hashlib.md5((base64.b64encode(encoded.encode()).decode() + api_key).encode()).hexdigest()
        if not compare_digest(expected.encode(), signature.encode()):
            return 'Forbidden', 403
        if data.get('status') not in ('paid', 'paid_over'):
            return 'OK', 200
        if data.get('amount') is None:
            return 'Missing amount', 400
        return dispatch_payment(data.get('order_id'), 'Heleket', data['amount'], data.get('currency'))

    @flask_app.route('/ton-webhook', methods=['POST'])
    def ton_webhook_handler():
        return 'TON payments disabled pending verified blockchain settlement', 503

    @flask_app.route('/lava-webhook', methods=['POST'])
    def lava_webhook_handler():
        if not lava_api.verify_webhook_auth(request.headers, get_setting('lava_webhook_key')):
            return 'Forbidden', 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return 'Invalid JSON', 400
        event = data.get('eventType') or data.get('event_type')
        if event != 'payment.success':
            return {'status': 'ok'}, 200
        payment_id = data.get('contractId') or data.get('contract_id') or data.get('id')
        # The authenticated event identifies a locally created invoice. Its stored
        # metadata, never client-supplied metadata, determines the subscription.
        if data.get('status') != 'completed' or data.get('amount') is None or not data.get('currency'):
            return 'Invalid one-time payment confirmation', 400
        return dispatch_payment(payment_id, 'Lava.top', data['amount'], data['currency'])

    return flask_app
