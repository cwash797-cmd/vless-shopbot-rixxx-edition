import asyncio
import logging
from datetime import datetime, timedelta
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram import Bot
from shop_bot.bot_controller import BotController
from shop_bot.data_manager import database
from shop_bot.bot import keyboards, handlers
import json

CHECK_INTERVAL_SECONDS = 300
NOTIFY_BEFORE_HOURS = {72, 48, 24, 1}
notified_users = {}
logger = logging.getLogger(__name__)

def format_time_left(hours: int) -> str:
    if hours >= 24:
        days = hours // 24
        if days % 10 == 1 and days % 100 != 11: return f"{days} день"
        elif 2 <= days % 10 <= 4 and (days % 100 < 10 or days % 100 >= 20): return f"{days} дня"
        else: return f"{days} дней"
    else:
        if hours % 10 == 1 and hours % 100 != 11: return f"{hours} час"
        elif 2 <= hours % 10 <= 4 and (hours % 100 < 10 or hours % 100 >= 20): return f"{hours} часа"
        else: return f"{hours} часов"

async def send_subscription_notification(bot: Bot, user_id: int, key_id: int, time_left_hours: int, expiry_date: datetime):
    try:
        time_text = format_time_left(time_left_hours)
        expiry_str = expiry_date.strftime('%d.%m.%Y в %H:%M')
        message = (
            f"⚠️ **Внимание!** ⚠️\n\n"
            f"Срок действия вашей подписки истекает через **{time_text}**.\n"
            f"Дата окончания: **{expiry_str}**\n\n"
            f"Продлите подписку, чтобы не остаться без доступа к сети!"
        )
        builder = InlineKeyboardBuilder()
        builder.button(text="🔑 Мои ключи", callback_data="manage_keys")
        builder.button(text="➕ Продлить ключ", callback_data=f"extend_key_{key_id}")
        builder.adjust(2)
        await bot.send_message(chat_id=user_id, text=message, reply_markup=builder.as_markup(), parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error sending subscription notification: {e}")

def _cleanup_notified_users(all_db_keys: list[dict]):
    if not notified_users: return
    active_key_ids = {(key['key_id'], key['expiry_date']) for key in all_db_keys}
    users_to_check = list(notified_users.keys())
    for user_id in users_to_check:
        keys_to_check = list(notified_users[user_id].keys())
        for key_id in keys_to_check:
            if key_id not in active_key_ids:
                del notified_users[user_id][key_id]
        if not notified_users[user_id]:
            del notified_users[user_id]

async def check_expiring_subscriptions(bot: Bot):
    current_time = datetime.now()
    all_keys = database.get_all_keys()
    _cleanup_notified_users(all_keys)
    for key in all_keys:
        try:
            expiry_date = datetime.fromisoformat(key['expiry_date'])
            time_left = expiry_date - current_time
            if time_left.total_seconds() < 0: continue
            total_hours_left = int(time_left.total_seconds() / 3600)
            user_id = key['user_id']
            key_id = (key['key_id'], key['expiry_date'])
            for hours_mark in NOTIFY_BEFORE_HOURS:
                if hours_mark - 1 < total_hours_left <= hours_mark:
                    notified_users.setdefault(user_id, {}).setdefault(key_id, set())
                    if hours_mark not in notified_users[user_id][key_id]:
                        await send_subscription_notification(bot, user_id, key['key_id'], hours_mark, expiry_date)
                        notified_users[user_id][key_id].add(hours_mark)
                    break 
        except Exception:
            logger.exception('Subscription scheduler operation failed')

async def sync_keys_with_panels():
    # Заглушка. Панель RIXXX сама удаляет просроченных юзеров через свой cron.
    pass

async def periodic_subscription_check(bot_controller: BotController):
    await asyncio.sleep(10)
    while True:
        try:
            await sync_keys_with_panels()
            if bot_controller.get_status().get("shop_bot_running"):
                bot = bot_controller.get_bot_instance()
                if bot:
                    for payment in database.get_verified_payments():
                        method = json.loads(payment['metadata']).get('payment_method')
                        if method == 'Lava.top SBP':
                            method = 'Lava.top'
                        await handlers.deliver_payment(bot, payment['payment_id'], method)
                    await check_expiring_subscriptions(bot)
        except Exception:
            logger.exception('Subscription scheduler operation failed')
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
