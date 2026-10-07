import logging
import uuid
import qrcode
import aiohttp
import re
import aiohttp
import hashlib
import json
import base64
import asyncio
import math
from weakref import WeakValueDictionary

from urllib.parse import urlencode
from hmac import compare_digest
from functools import wraps
from yookassa import Payment
from io import BytesIO
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict


from aiogram import Bot, Router, F, types, html
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.types import BufferedInputFile
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ChatMemberStatus
from aiogram.utils.keyboard import InlineKeyboardBuilder

from shop_bot.bot import keyboards
from shop_bot.modules import xui_api, lava_api
from shop_bot.data_manager.database import (
    get_user, add_new_key, get_user_keys, update_user_stats,
    register_user_if_not_exists, get_next_key_number, get_key_by_id,
    update_key_info, set_trial_used, set_terms_agreed, get_setting, get_all_hosts,
    get_plans_for_host, get_plan_by_id, log_transaction, get_referral_count,
    add_to_referral_balance, create_pending_transaction, get_all_users,
    set_referral_balance, set_referral_balance_all,
    set_user_give_permission, hard_delete_user_db, ban_user, unban_user, delete_user_keys,
    set_custom_referral_percentage, remove_custom_referral_percentage, delete_key_by_email,
    find_and_complete_pending_transaction, get_payment, finish_payment, claim_trial, mark_payment_verified, record_payment_target
)

from shop_bot.config import (
    get_profile_text, get_vpn_active_text, VPN_INACTIVE_TEXT, VPN_NO_DATA_TEXT,
    get_key_info_text, CHOOSE_PAYMENT_METHOD_MESSAGE, get_purchase_success_text
)

TELEGRAM_BOT_USERNAME = None
PAYMENT_METHODS = None
ADMIN_ID = None

def is_main_admin(user_id: int | str) -> bool:
    return str(user_id) == get_setting("admin_telegram_id")

def can_use_give(user_id: int) -> bool:
    if is_main_admin(user_id): return True
    user_data = get_user(user_id)
    return bool(user_data and user_data.get('can_give') and not user_data.get('is_banned'))

def generate_client_email(user_id: int, key_number: int, host_name: str, is_trial: bool = False) -> str:
    user_data = get_user(user_id)
    uname = user_data.get('username') if user_data else None

    if uname:
        uname_clean = re.sub(r'[^a-zA-Z0-9]', '', uname)
    else:
        uname_clean = "user"

    if not uname_clean:
        uname_clean = "user"

    host_clean = re.sub(r'[^a-z0-9-]', '', host_name.lower())[:20] or 'rixxx'
    if is_trial:
        return f"{uname_clean}_{user_id}_key{key_number}-trial@telegram.bot"
    return f"{uname_clean[:12]}_{user_id}_{uuid.uuid4().hex[:12]}@{host_clean}.bot"


logger = logging.getLogger(__name__)
admin_router = Router()
user_router = Router()

class KeyPurchase(StatesGroup):
    waiting_for_host_selection = State()
    waiting_for_plan_selection = State()

class Onboarding(StatesGroup):
    waiting_for_subscription_and_agreement = State()

class PaymentProcess(StatesGroup):
    waiting_for_email = State()
    waiting_for_payment_method = State()

class Broadcast(StatesGroup):
    waiting_for_message = State()
    waiting_for_button_option = State()
    waiting_for_button_text = State()
    waiting_for_button_url = State()
    waiting_for_confirmation = State()

class WithdrawStates(StatesGroup):
    waiting_for_details = State()

def is_valid_email(email: str) -> bool:
    pattern = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
    return re.match(pattern, email) is not None

async def show_main_menu(message: types.Message, edit_message: bool = False):
    user_id = message.chat.id
    user_db_data = get_user(user_id)
    user_keys = get_user_keys(user_id)

    trial_available = not (user_db_data and user_db_data.get('trial_used'))
    is_admin = str(user_id) == ADMIN_ID

    text = "🏠 <b>Главное меню</b>\n\nВыберите действие:"
    keyboard = keyboards.create_main_menu_keyboard(user_keys, trial_available, is_admin)

    if edit_message:
        try:
            await message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest:
            pass
    else:
        await message.answer(text, reply_markup=keyboard)

def registration_required(f):
    @wraps(f)
    async def decorated_function(event: types.Update, *args, **kwargs):
        user_id = event.from_user.id
        user_data = get_user(user_id)
        if user_data and user_data.get('agreed_to_terms') and not user_data.get('is_banned'):
            return await f(event, *args, **kwargs)
        else:
            message_text = "Пожалуйста, для начала работы со мной, отправьте команду /start"
            if isinstance(event, types.CallbackQuery):
                await event.answer(message_text, show_alert=True)
            else:
                await event.answer(message_text)
    return decorated_function

def get_user_router() -> Router:
    user_router = Router()
    user_router.message.filter(F.chat.type == 'private')
    user_router.callback_query.filter(F.message.chat.type == 'private')

    @user_router.message(Command(commands=["give"]))
    async def admin_give_key(message: types.Message, bot: Bot):
        if not can_use_give(message.from_user.id):
            return

        args = message.text.split()
        if len(args) > 1 and (not args[1].isdigit() or not 0 < int(args[1]) < 2**63):
            return await message.answer('ID должен быть положительным целым числом.')
        if len(args) != 3:
            await message.answer("Использование: /give <ID_пользователя_Telegram> <дней>\nНапример: /give 123456789 3650")
            return

        try:
            target_user_id = int(args[1])
            days = int(args[2])
        except ValueError:
            await message.answer("❌ ID и дни должны быть числами.")
            return

        if not 1 <= days <= 36500:
            return await message.answer('Срок: от 1 до 36500 дней.')
        target = get_user(target_user_id)
        if not target:
            return await message.answer('Пользователь должен сначала написать боту /start.')
        if target.get('is_banned'):
            return await message.answer('Пользователь заблокирован. Сначала /unban.')
        hosts = get_all_hosts()
        if not hosts:
            await message.answer("❌ В админке нет доступных серверов. Сначала добавь хост.")
            return

        host_name = hosts[0]['host_name']
        key_number = get_next_key_number(target_user_id)
        email = generate_client_email(target_user_id, key_number, host_name)

        processing_msg = await message.answer(f"Создаю ключ на {days} дней...")

        result = await xui_api.create_or_update_key_on_host(host_name, email, days)
        if not result:
            await processing_msg.edit_text("❌ Ошибка при создании ключа в панели RIXXX.")
            return

        key_id = add_new_key(target_user_id, host_name, result['client_uuid'], result['email'], result['expiry_timestamp_ms'])

        connection_string = result['connection_string']
        expiry_date = datetime.fromtimestamp(result['expiry_timestamp_ms'] / 1000)

        final_text = get_purchase_success_text("готова", key_number, expiry_date, connection_string)

        try:
            await bot.send_message(
                chat_id=target_user_id,
                text="🎁 Администратор выдал вам подписку!\n\n" + final_text,
                reply_markup=keyboards.create_key_info_keyboard(key_id)
            )
            await processing_msg.edit_text(f"✅ Успешно! Ключ на {days} дней выдан пользователю {target_user_id}.")
        except Exception as e:
            await processing_msg.edit_text(f"✅ Ключ создан, но отправить в ЛС не удалось (юзер не запустил бота?): {e}")

    @user_router.message(Command(commands=["grant"]))
    async def cmd_grant(message: types.Message):
        if not is_main_admin(message.from_user.id): return
        args = message.text.split()
        if len(args) > 1 and (not args[1].isdigit() or not 0 < int(args[1]) < 2**63):
            return await message.answer('ID должен быть положительным целым числом.')
        if len(args) != 2: return await message.answer("Формат: /grant <ID>")
        if not get_user(int(args[1])):
            return await message.answer('Пользователь должен сначала написать боту /start.')
        set_user_give_permission(int(args[1]), True)
        await message.answer(f"✅ Пользователю {args[1]} выданы права на использование /give.")

    @user_router.message(Command(commands=["revoke"]))
    async def cmd_revoke(message: types.Message):
        if not is_main_admin(message.from_user.id): return
        args = message.text.split()
        if len(args) > 1 and (not args[1].isdigit() or not 0 < int(args[1]) < 2**63):
            return await message.answer('ID должен быть положительным целым числом.')
        if len(args) != 2: return await message.answer("Формат: /revoke <ID>")
        set_user_give_permission(int(args[1]), False)
        await message.answer(f"❌ У пользователя {args[1]} забраны права на /give.")

    @user_router.message(Command(commands=["ban"]))
    async def cmd_ban(message: types.Message):
        if not is_main_admin(message.from_user.id): return
        args = message.text.split()
        if len(args) > 1 and (not args[1].isdigit() or not 0 < int(args[1]) < 2**63):
            return await message.answer('ID должен быть положительным целым числом.')
        if len(args) != 2: return await message.answer("Формат: /ban <ID>")
        target_id = int(args[1])
        if is_main_admin(target_id):
            return await message.answer('Нельзя заблокировать владельца.')
        ban_user(target_id)
        revoked, total = await revoke_user_access(target_id)
        await message.answer(f'Доступ к боту заблокирован. Отозвано ключей: {revoked}/{total}. Неудачные отзывы сохранены для повтора.')
        return

    @user_router.message(Command(commands=["unban"]))
    async def cmd_unban(message: types.Message):
        if not is_main_admin(message.from_user.id): return
        args = message.text.split()
        if len(args) > 1 and (not args[1].isdigit() or not 0 < int(args[1]) < 2**63):
            return await message.answer('ID должен быть положительным целым числом.')
        if len(args) != 2: return await message.answer("Формат: /unban <ID>")
        target_id = int(args[1])
        unban_user(target_id)
        await message.answer(f"🕊 Пользователь {target_id} разблокирован.")

    @user_router.message(Command(commands=["delete_user"]))
    async def cmd_delete_user(message: types.Message):
        if not is_main_admin(message.from_user.id): return
        args = message.text.split()
        if len(args) > 1 and (not args[1].isdigit() or not 0 < int(args[1]) < 2**63):
            return await message.answer('ID должен быть положительным целым числом.')
        if len(args) != 2: return await message.answer("Формат: /delete_user <ID>")

        target_id = int(args[1])
        processing_msg = await message.answer("Удаляю пользователя с серверов и из БД...")

        # 1. Сначала удаляем ключи из панели RIXXX (XUI)
        keys_to_revoke = get_user_keys(target_id)
        success_count = 0
        for key in keys_to_revoke:
            result = await xui_api.delete_client_on_host(key['host_name'], key['key_email'])
            if result: success_count += 1

        if success_count != len(keys_to_revoke):
            await processing_msg.edit_text('Не все ключи отозваны. Профиль и ключи сохранены; повторите после восстановления серверов.')
            return
        hard_delete_user_db(target_id)

        await processing_msg.edit_text(
            f"🗑 Пользователь {target_id} полностью удален.\n"
            f"Удалено ключей с физических серверов: {success_count} из {len(keys_to_revoke)}.\n"
            f"Теперь он может заново взять пробный период."
        )

    @user_router.message(Command(commands=["setref"]))
    async def cmd_setref(message: types.Message):
        if not is_main_admin(message.from_user.id): return
        args = message.text.split()
        if len(args) > 1 and (not args[1].isdigit() or not 0 < int(args[1]) < 2**63):
            return await message.answer('ID должен быть положительным целым числом.')
        if len(args) != 3: return await message.answer("Формат: /setref <ID> <ПРОЦЕНТ>")

        try:
            target_id = int(args[1])
            percent = float(args[2])
            if not math.isfinite(percent) or not 0 <= percent <= 100:
                return await message.answer('Процент должен быть от 0 до 100.')
            set_custom_referral_percentage(target_id, percent)
            await message.answer(f"💎 Пользователю {target_id} установлен VIP-процент рефералки: {percent}%")
        except ValueError:
            await message.answer("Ошибка: ID и ПРОЦЕНТ должны быть числами.")

    @user_router.message(Command(commands=["delref"]))
    async def cmd_delref(message: types.Message):
        if not is_main_admin(message.from_user.id): return
        args = message.text.split()
        if len(args) > 1 and (not args[1].isdigit() or not 0 < int(args[1]) < 2**63):
            return await message.answer('ID должен быть положительным целым числом.')
        if len(args) != 2: return await message.answer("Формат: /delref <ID>")

        try:
            target_id = int(args[1])
            remove_custom_referral_percentage(target_id)
            await message.answer(f"🔙 У пользователя {target_id} убран VIP-процент (теперь стандартный).")
        except ValueError:
            await message.answer("Ошибка: ID должен быть числом.")

    @user_router.message(Command(commands=["users"]))
    async def cmd_users_list(message: types.Message):
        if not is_main_admin(message.from_user.id): return

        users = get_all_users()
        if not users:
            return await message.answer("База пользователей пуста.")

        # Формируем таблицу
        lines = [f"{'Telegram ID':<12} | {'Username':<18} | {'Статус':<9} | {'Ключи':<5} | {'Give':<5} | {'VIP %':<6}"]
        lines.append("-" * 74)

        for u in users:
            uid = str(u['telegram_id'])
            uname = f"@{u['username'][:17]}" if u.get('username') else "N/A"
            status = "Забанен" if u.get('is_banned') else "Активен"
            keys_count = str(len(get_user_keys(u['telegram_id'])))
            can_give = "Да" if u.get('can_give') else "Нет"
            vip_pct = f"{u.get('custom_referral_percentage')}%" if u.get('custom_referral_percentage') is not None else "-"

            lines.append(f"{uid:<12} | {uname:<18} | {status:<9} | {keys_count:<5} | {can_give:<5} | {vip_pct:<6}")

        # Записываем в виртуальный файл
        file_data = "\n".join(lines).encode('utf-8')
        document = BufferedInputFile(file_data, filename="vpn_users_report.txt")

        await message.answer_document(
            document,
            caption=f"📊 Выгрузка базы данных.\nВсего пользователей: {len(users)}"
        )


    @user_router.message(Command('keys'))
    async def admin_keys(message: types.Message):
        if not is_main_admin(message.from_user.id):
            return
        args = message.text.split()
        if len(args) != 2 or not args[1].isdigit():
            return await message.answer('Формат: /keys <Telegram ID>')
        keys = get_user_keys(int(args[1]))
        await message.answer('\n'.join(f"ID {k['key_id']}: {html.quote(k['host_name'])}" for k in keys) or 'Ключей нет.')

    @user_router.message(Command('bonus'))
    async def admin_bonus(message: types.Message):
        if not is_main_admin(message.from_user.id):
            return
        args = message.text.split()
        if len(args) < 3 or not args[1].isdigit():
            return await message.answer('Формат: /bonus <ID ключа из /keys> <URI через пробел> или /bonus <ID> clear. Список заменяется целиком.')
        key = get_key_by_id(int(args[1]))
        if not key:
            return await message.answer('Ключ не найден.')
        links = [] if args[2:] == ['clear'] else args[2:]
        success = await xui_api.set_bonus_links(key, links)
        await message.answer('Бонусные ссылки обновлены в той же подписке. Обновите её в клиенте.' if success else 'Не удалось обновить ссылки. Проверьте URI и доступность панели.')

    @user_router.message(CommandStart())
    async def start_handler(message: types.Message, state: FSMContext, bot: Bot, command: CommandObject):
        user_id = message.from_user.id
        username = message.from_user.username or message.from_user.full_name
        referrer_id = None

        if command.args and command.args.startswith('ref_'):
            try:
                potential_referrer_id = int(command.args.split('_')[1])
                if potential_referrer_id != user_id:
                    referrer_id = potential_referrer_id
                    logger.info(f"New user {user_id} was referred by {referrer_id}")
            except (IndexError, ValueError):
                logger.warning(f"Invalid referral code received: {command.args}")

        register_user_if_not_exists(user_id, username, referrer_id)
        user_id = message.from_user.id
        username = message.from_user.username or message.from_user.full_name
        user_data = get_user(user_id)

        if user_data and user_data.get('agreed_to_terms'):
            await message.answer(
                f"👋 Снова здравствуйте, {html.bold(message.from_user.full_name)}!",
                reply_markup=keyboards.main_reply_keyboard
            )
            await show_main_menu(message)
            return

        terms_url = get_setting("terms_url")
        privacy_url = get_setting("privacy_url")
        channel_url = get_setting("channel_url")

        if not channel_url or not terms_url or not privacy_url:
            set_terms_agreed(user_id)
            await show_main_menu(message)
            return

        is_subscription_forced = get_setting("force_subscription") == "true"

        show_welcome_screen = (is_subscription_forced and channel_url) or (terms_url and privacy_url)

        if not show_welcome_screen:
            set_terms_agreed(user_id)
            await show_main_menu(message)
            return

        welcome_parts = ["<b>Добро пожаловать!</b>\n"]

        if is_subscription_forced and channel_url:
            welcome_parts.append("Для доступа ко всем функциям, пожалуйста, подпишитесь на наш канал.\n")

        if terms_url:
            welcome_parts.append("Также необходимо ознакомиться и принять наши Условия использования.")
        elif privacy_url:
            welcome_parts.append("Также необходимо ознакомиться с нашей Политикой конфиденциальности.")
        elif terms_url and privacy_url:
            welcome_parts.append("Также необходимо ознакомиться с нашими Условиями использования и Политикой конфиденциальности.")

        welcome_parts.append("\nПосле этого нажмите кнопку ниже.")
        final_text = "\n".join(welcome_parts)

        await message.answer(
            final_text,
            reply_markup=keyboards.create_welcome_keyboard(
                channel_url=channel_url,
                is_subscription_forced=is_subscription_forced,
                terms_url=terms_url,
                privacy_url=privacy_url
            ),
            disable_web_page_preview=True
        )
        await state.set_state(Onboarding.waiting_for_subscription_and_agreement)

    @user_router.callback_query(Onboarding.waiting_for_subscription_and_agreement, F.data == "check_subscription_and_agree")
    async def check_subscription_handler(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
        user_id = callback.from_user.id
        channel_url = get_setting("channel_url")
        is_subscription_forced = get_setting("force_subscription") == "true"

        if not is_subscription_forced or not channel_url:
            await process_successful_onboarding(callback, state)
            return

        try:
            if '@' not in channel_url and 't.me/' not in channel_url:
                logger.error(f"Неверный формат URL канала: {channel_url}. Пропускаем проверку подписки.")
                await process_successful_onboarding(callback, state)
                return

            channel_id = '@' + channel_url.split('/')[-1] if 't.me/' in channel_url else channel_url
            member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)

            if member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]:
                await process_successful_onboarding(callback, state)
            else:
                await callback.answer("Вы еще не подписались на канал. Пожалуйста, подпишитесь и попробуйте снова.", show_alert=True)

        except Exception as e:
            logger.error(f"Ошибка при проверке подписки для user_id {user_id} на канал {channel_url}: {e}")
            await callback.answer("Не удалось проверить подписку. Убедитесь, что бот является администратором канала. Попробуйте позже.", show_alert=True)

    @user_router.message(Onboarding.waiting_for_subscription_and_agreement)
    async def onboarding_fallback_handler(message: types.Message):
        await message.answer("Пожалуйста, выполните требуемые действия и нажмите на кнопку в сообщении выше.")

    @user_router.message(F.text == "🏠 Главное меню")
    @registration_required
    async def main_menu_handler(message: types.Message):
        await show_main_menu(message)

    @user_router.callback_query(F.data == "back_to_main_menu")
    @registration_required
    async def back_to_main_menu_handler(callback: types.CallbackQuery):
        await callback.answer()
        await show_main_menu(callback.message, edit_message=True)

    @user_router.callback_query(F.data == "show_profile")
    @registration_required
    async def profile_handler_callback(callback: types.CallbackQuery):
        await callback.answer()
        user_id = callback.from_user.id
        user_db_data = get_user(user_id)
        user_keys = get_user_keys(user_id)
        if not user_db_data:
            await callback.answer("Не удалось получить данные профиля.", show_alert=True)
            return
        username = html.bold(user_db_data.get('username', 'Пользователь'))
        total_spent, total_months = user_db_data.get('total_spent', 0), user_db_data.get('total_months', 0)
        now = datetime.now()
        active_keys = [key for key in user_keys if datetime.fromisoformat(key['expiry_date']) > now]
        if active_keys:
            latest_key = max(active_keys, key=lambda k: datetime.fromisoformat(k['expiry_date']))
            latest_expiry_date = datetime.fromisoformat(latest_key['expiry_date'])
            time_left = latest_expiry_date - now
            vpn_status_text = get_vpn_active_text(time_left.days, time_left.seconds // 3600)
        elif user_keys: vpn_status_text = VPN_INACTIVE_TEXT
        else: vpn_status_text = VPN_NO_DATA_TEXT
        final_text = get_profile_text(username, total_spent, total_months, vpn_status_text)
        await callback.message.edit_text(final_text, reply_markup=keyboards.create_back_to_menu_keyboard())

    @user_router.callback_query(F.data == "start_broadcast")
    @registration_required
    async def start_broadcast_handler(callback: types.CallbackQuery, state: FSMContext):
        if str(callback.from_user.id) != ADMIN_ID:
            await callback.answer("У вас нет прав.", show_alert=True)
            return

        await callback.answer()
        await callback.message.edit_text(
            "Пришлите сообщение, которое вы хотите разослать всем пользователям.\n"
            "Вы можете использовать форматирование (<b>жирный</b>, <i>курсив</i>).\n"
            "Также поддерживаются фото, видео и документы.\n",
            reply_markup=keyboards.create_broadcast_cancel_keyboard()
        )
        await state.set_state(Broadcast.waiting_for_message)

    @user_router.message(Broadcast.waiting_for_message)
    async def broadcast_message_received_handler(message: types.Message, state: FSMContext):
        await state.update_data(message_to_send=message.model_dump_json())

        await message.answer(
            "Сообщение получено. Хотите добавить к нему кнопку со ссылкой?",
            reply_markup=keyboards.create_broadcast_options_keyboard()
        )
        await state.set_state(Broadcast.waiting_for_button_option)

    @user_router.callback_query(Broadcast.waiting_for_button_option, F.data == "broadcast_add_button")
    async def add_button_prompt_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await callback.message.edit_text(
            "Хорошо. Теперь отправьте мне текст для кнопки.",
            reply_markup=keyboards.create_broadcast_cancel_keyboard()
        )
        await state.set_state(Broadcast.waiting_for_button_text)

    @user_router.message(Broadcast.waiting_for_button_text)
    async def button_text_received_handler(message: types.Message, state: FSMContext):
        await state.update_data(button_text=message.text)
        await message.answer(
            "Текст кнопки получен. Теперь отправьте ссылку (URL), куда она будет вести.",
            reply_markup=keyboards.create_broadcast_cancel_keyboard()
        )
        await state.set_state(Broadcast.waiting_for_button_url)

    @user_router.message(Broadcast.waiting_for_button_url)
    async def button_url_received_handler(message: types.Message, state: FSMContext, bot: Bot):
        url_to_check = message.text

        is_valid = await is_url_reachable(url_to_check)

        if not is_valid:
            await message.answer(
                "❌ **Ссылка не прошла проверку.**\n\n"
                "Пожалуйста, убедитесь, что:\n"
                "1. Ссылка начинается с `http://` или `https://`.\n"
                "2. Доменное имя корректно (например, `example.com`).\n"
                "3. Сайт доступен в данный момент.\n\n"
                "Попробуйте еще раз."
            )
            return

        await state.update_data(button_url=url_to_check)
        await show_broadcast_preview(message, state, bot)

    @user_router.callback_query(Broadcast.waiting_for_button_option, F.data == "broadcast_skip_button")
    async def skip_button_handler(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
        await callback.answer()
        await state.update_data(button_text=None, button_url=None)
        await show_broadcast_preview(callback.message, state, bot)

    async def show_broadcast_preview(message: types.Message, state: FSMContext, bot: Bot):
        data = await state.get_data()
        message_json = data.get('message_to_send')
        original_message = types.Message.model_validate_json(message_json)

        button_text = data.get('button_text')
        button_url = data.get('button_url')

        preview_keyboard = None
        if button_text and button_url:
            builder = InlineKeyboardBuilder()
            builder.button(text=button_text, url=button_url)
            preview_keyboard = builder.as_markup()

        await message.answer(
            "Вот так будет выглядеть ваше сообщение. Отправляем?",
            reply_markup=keyboards.create_broadcast_confirmation_keyboard()
        )

        await bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=original_message.chat.id,
            message_id=original_message.message_id,
            reply_markup=preview_keyboard
        )

        await state.set_state(Broadcast.waiting_for_confirmation)

    @user_router.callback_query(Broadcast.waiting_for_confirmation, F.data == "confirm_broadcast")
    async def confirm_broadcast_handler(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
        await callback.message.edit_text("⏳ Начинаю рассылку... Это может занять некоторое время.")

        data = await state.get_data()
        message_json = data.get('message_to_send')
        original_message = types.Message.model_validate_json(message_json)

        button_text = data.get('button_text')
        button_url = data.get('button_url')

        final_keyboard = None
        if button_text and button_url:
            builder = InlineKeyboardBuilder()
            builder.button(text=button_text, url=button_url)
            final_keyboard = builder.as_markup()

        await state.clear()

        users = get_all_users()
        logger.info(f"Broadcast: Starting to iterate over {len(users)} users.")

        sent_count = 0
        failed_count = 0
        banned_count = 0

        for user in users:
            user_id = user['telegram_id']
            if user.get('is_banned'):
                banned_count += 1
                continue

            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=original_message.chat.id,
                    message_id=original_message.message_id,
                    reply_markup=final_keyboard
                )

                sent_count += 1
                await asyncio.sleep(0.1)
            except Exception as e:
                failed_count += 1
                logger.warning(f"Failed to send broadcast message to user {user_id}: {e}")

        await callback.message.answer(
            f"✅ Рассылка завершена!\n\n"
            f"👍 Отправлено: {sent_count}\n"
            f"👎 Не удалось отправить: {failed_count}\n"
            f"🚫 Пропущено (забанены): {banned_count}"
        )
        await show_main_menu(callback.message)

    @user_router.callback_query(StateFilter(Broadcast), F.data == "cancel_broadcast")
    async def cancel_broadcast_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("Рассылка отменена.")
        await state.clear()
        await show_main_menu(callback.message, edit_message=True)

    @user_router.callback_query(F.data == "show_referral_program")
    @registration_required
    async def referral_program_handler(callback: types.CallbackQuery):
        await callback.answer()
        user_id = callback.from_user.id
        user_data = get_user(user_id)
        bot_username = (await callback.bot.get_me()).username

        referral_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        referral_count = get_referral_count(user_id)
        balance = user_data.get('referral_balance', 0)

        custom_pct = user_data.get('custom_referral_percentage')
        rate_text = f" (Ваша персональная ставка: {custom_pct}% 💎)" if custom_pct is not None else ""

        text = (
            f"🤝 <b>Реферальная программа</b>{rate_text}\n\n"
            "Приглашайте друзей и получайте вознаграждение с <b>каждой</b> их покупки!\n\n"
            f"<b>Ваша реферальная ссылка:</b>\n<code>{referral_link}</code>\n\n"
            f"<b>Приглашено пользователей:</b> {referral_count}\n"
            f"<b>Ваш баланс:</b> {balance:.2f} RUB"
        )

        builder = InlineKeyboardBuilder()
        if balance >= float(get_setting("minimum_withdrawal") or "100"):
            builder.button(text="💸 Оставить заявку на вывод", callback_data="withdraw_request")
        builder.button(text="⬅️ Назад", callback_data="back_to_main_menu")
        await callback.message.edit_text(
            text, reply_markup=builder.as_markup()
        )

    @user_router.callback_query(F.data == "withdraw_request")
    @registration_required
    async def withdraw_request_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await callback.message.edit_text(
            "Пожалуйста, отправьте ваши реквизиты для вывода (номер карты или номер телефона и банк):"
        )
        await state.set_state(WithdrawStates.waiting_for_details)

    @user_router.message(WithdrawStates.waiting_for_details)
    @registration_required
    async def process_withdraw_details(message: types.Message, state: FSMContext):
        user_id = message.from_user.id
        user = get_user(user_id)
        balance = user.get('referral_balance', 0)
        details = message.text.strip()
        if balance < float(get_setting("minimum_withdrawal") or "100"):
            await message.answer("❌ Баланс меньше установленного минимума вывода.")
            await state.clear()
            return

        admin_id = int(get_setting("admin_telegram_id"))
        text = (
            f"💸 <b>Заявка на вывод реферальных средств</b>\n"
            f"👤 Пользователь: @{user.get('username', 'N/A')} (ID: <code>{user_id}</code>)\n"
            f"💰 Сумма: <b>{balance:.2f} RUB</b>\n"
            f"📄 Реквизиты: <code>{html.quote(details)}</code>\n\n"
            f"/approve_withdraw {user_id}\n/decline_withdraw {user_id}"
        )
        await message.answer("Ваша заявка отправлена администратору. Ожидайте ответа.")
        await message.bot.send_message(admin_id, text, parse_mode="HTML")
        await state.clear()

    @user_router.message(Command(commands=["approve_withdraw"]))
    async def approve_withdraw_handler(message: types.Message):
        admin_id = int(get_setting("admin_telegram_id"))
        if message.from_user.id != admin_id:
            return
        try:
            user_id = int(message.text.split()[1])
            user = get_user(user_id)
            balance = user.get('referral_balance', 0)
            if balance < float(get_setting("minimum_withdrawal") or "100"):
                await message.answer("Баланс меньше установленного минимума вывода.")
                return
            set_referral_balance(user_id, 0)
            await message.answer(f"✅ Выплата {balance:.2f} RUB пользователю {user_id} подтверждена.")
            await message.bot.send_message(
                user_id,
                f"✅ Ваша заявка на вывод {balance:.2f} RUB одобрена. Деньги будут переведены в ближайшее время."
            )
        except Exception as e:
            await message.answer(f"Ошибка: {e}")

    @user_router.message(Command(commands=["decline_withdraw"]))
    async def decline_withdraw_handler(message: types.Message):
        admin_id = int(get_setting("admin_telegram_id"))
        if message.from_user.id != admin_id:
            return
        try:
            user_id = int(message.text.split()[1])
            await message.answer(f"❌ Заявка пользователя {user_id} отклонена.")
            await message.bot.send_message(
                user_id,
                "❌ Ваша заявка на вывод отклонена. Проверьте корректность реквизитов и попробуйте снова."
            )
        except Exception as e:
            await message.answer(f"Ошибка: {e}")

    @user_router.callback_query(F.data == "show_about")
    @registration_required
    async def about_handler(callback: types.CallbackQuery):
        await callback.answer()

        about_text = get_setting("about_text")
        terms_url = get_setting("terms_url")
        privacy_url = get_setting("privacy_url")
        channel_url = get_setting("channel_url")

        final_text = about_text if about_text else "Информация о проекте не добавлена."

        keyboard = keyboards.create_about_keyboard(channel_url, terms_url, privacy_url)

        await callback.message.edit_text(
            final_text,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )

    @user_router.callback_query(F.data == "show_help")
    @registration_required
    async def about_handler(callback: types.CallbackQuery):
        await callback.answer()

        support_user = get_setting("support_user")
        support_text = get_setting("support_text")

        if support_user == None and support_text == None:
            await callback.message.edit_text(
                "Информация о поддержке не установлена. Установите её в админ-панели.",
                reply_markup=keyboards.create_back_to_menu_keyboard()
            )
        elif support_text == None:
            await callback.message.edit_text(
                "Для связи с поддержкой используйте кнопку ниже.",
                reply_markup=keyboards.create_support_keyboard(support_user)
            )
        else:
            await callback.message.edit_text(
                support_text + "\n\n",
                reply_markup=keyboards.create_support_keyboard(support_user)
            )

    @user_router.callback_query(F.data == "manage_keys")
    @registration_required
    async def manage_keys_handler(callback: types.CallbackQuery):
        await callback.answer()
        user_id = callback.from_user.id
        user_keys = get_user_keys(user_id)
        await callback.message.edit_text(
            "Ваши ключи:" if user_keys else "У вас пока нет ключей.",
            reply_markup=keyboards.create_keys_management_keyboard(user_keys)
        )

    @user_router.callback_query(F.data == "get_trial")
    @registration_required
    async def trial_period_handler(callback: types.CallbackQuery, state: FSMContext):
        user_id = callback.from_user.id
        user_db_data = get_user(user_id)
        if get_setting('trial_enabled') != 'true':
            return await callback.answer('Пробный период отключён.', show_alert=True)
        if user_db_data and user_db_data.get('trial_used'):
            await callback.answer("Вы уже использовали бесплатный пробный период.", show_alert=True)
            return

        hosts = get_all_hosts()
        if not hosts:
            await callback.message.edit_text("❌ В данный момент нет доступных серверов для создания пробного ключа.")
            return

        if len(hosts) == 1:
            await callback.answer()
            await process_trial_key_creation(callback.message, hosts[0]['host_name'])
        else:
            await callback.answer()
            await callback.message.edit_text(
                "Выберите сервер, на котором хотите получить пробный ключ:",
                reply_markup=keyboards.create_host_selection_keyboard(hosts, action="trial")
            )

    @user_router.callback_query(F.data.startswith("select_host_trial_"))
    @registration_required
    async def trial_host_selection_handler(callback: types.CallbackQuery):
        await callback.answer()
        host_name = callback.data[len("select_host_trial_"):]
        await process_trial_key_creation(callback.message, host_name)

    async def process_trial_key_creation(message: types.Message, host_name: str):
        user_id = message.chat.id
        if get_setting('trial_enabled') != 'true':
            return await message.answer('Пробный период отключён.')
        if host_name not in {h['host_name'] for h in get_all_hosts()}:
            return await message.answer('Сервер недоступен.')
        if not claim_trial(user_id):
            return await message.answer('Пробный период уже использован или требует проверки поддержки.')
        await message.edit_text(f"Отлично! Создаю для вас бесплатный ключ на {get_setting('trial_duration_days')} дня на сервере \"{host_name}\"...")

        try:
            key_number = get_next_key_number(user_id)
            email = generate_client_email(user_id, key_number, host_name, is_trial=True)
            result = await xui_api.create_or_update_key_on_host(
                host_name=host_name,
                email=email,
                days_to_add=int(get_setting("trial_duration_days"))
            )
            if not result:
                await message.edit_text("❌ Не удалось создать пробный ключ. Ошибка на сервере.")
                return

            new_key_id = add_new_key(
                user_id=user_id,
                host_name=host_name,
                xui_client_uuid=result['client_uuid'],
                key_email=result['email'],
                expiry_timestamp_ms=result['expiry_timestamp_ms']
            )

            if not new_key_id:
                return await message.answer('Ключ создан на сервере, но не сохранён. Обратитесь в поддержку.')
            await message.delete()
            new_expiry_date = datetime.fromtimestamp(result['expiry_timestamp_ms'] / 1000)
            final_text = get_purchase_success_text("готов", get_next_key_number(user_id) -1, new_expiry_date, result['connection_string'])
            await message.answer(text=final_text, reply_markup=keyboards.create_key_info_keyboard(new_key_id))

        except Exception as e:
            logger.error(f"Error creating trial key for user {user_id} on host {host_name}: {e}", exc_info=True)
            await message.edit_text("❌ Произошла ошибка при создании пробного ключа.")

    @user_router.callback_query(F.data.startswith("show_key_"))
    @registration_required
    async def show_key_handler(callback: types.CallbackQuery):
        key_id_to_show = int(callback.data.split("_")[2])
        await callback.message.edit_text("Загружаю информацию о ключе...")
        user_id = callback.from_user.id
        key_data = get_key_by_id(key_id_to_show)

        if not key_data or key_data['user_id'] != user_id:
            await callback.message.edit_text("❌ Ошибка: ключ не найден.")
            return

        try:
            details = await xui_api.get_key_details_from_host(key_data)
            if not details or not details['connection_string']:
                await callback.message.edit_text("❌ Ошибка на сервере. Не удалось получить данные ключа.")
                return

            connection_string = details['connection_string']
            expiry_date = datetime.fromisoformat(key_data['expiry_date'])
            created_date = datetime.fromisoformat(key_data['created_date'])

            all_user_keys = get_user_keys(user_id)
            key_number = next((i + 1 for i, key in enumerate(all_user_keys) if key['key_id'] == key_id_to_show), 0)

            final_text = get_key_info_text(key_number, expiry_date, created_date, connection_string)

            await callback.message.edit_text(
                text=final_text,
                reply_markup=keyboards.create_key_info_keyboard(key_id_to_show)
            )
        except Exception as e:
            logger.error(f"Error showing key {key_id_to_show}: {e}")
            await callback.message.edit_text("❌ Произошла ошибка при получении данных ключа.")


    @user_router.callback_query(F.data.startswith("show_qr_"))
    @registration_required
    async def show_qr_handler(callback: types.CallbackQuery):
        await callback.answer("Генерирую QR-код...")
        key_id = int(callback.data.split("_")[2])
        key_data = get_key_by_id(key_id)
        if not key_data or key_data['user_id'] != callback.from_user.id: return

        try:
            details = await xui_api.get_key_details_from_host(key_data)
            if not details or not details['connection_string']:
                await callback.answer("Ошибка: Не удалось сгенерировать QR-код.", show_alert=True)
                return

            connection_string = details['connection_string']
            qr_img = qrcode.make(connection_string)
            bio = BytesIO(); qr_img.save(bio, "PNG"); bio.seek(0)
            qr_code_file = BufferedInputFile(bio.read(), filename="vpn_qr.png")
            await callback.message.answer_photo(photo=qr_code_file)
        except Exception as e:
            logger.error(f"Error showing QR for key {key_id}: {e}")

    @user_router.callback_query(F.data.startswith("howto_vless_"))
    @registration_required
    async def show_instruction_handler(callback: types.CallbackQuery):
        await callback.answer()
        key_id = int(callback.data.split("_")[2])
        android_url = get_setting("android_url")
        ios_url = get_setting("ios_url")
        windows_url = get_setting("windows_url")
        linux_url = get_setting("linux_url")

        await callback.message.edit_text(
            "Выберите вашу платформу для получения инструкции:",
            reply_markup=keyboards.create_howto_vless_keyboard_key(
            android_url=android_url,
            windows_url=windows_url,
            ios_url=ios_url,
            linux_url=linux_url,
            key_id=key_id),
            disable_web_page_preview=False
        )

    @user_router.callback_query(F.data.startswith("howto_vless"))
    @registration_required
    async def show_instruction_handler(callback: types.CallbackQuery):
        await callback.answer()
        android_url = get_setting("android_url")
        ios_url = get_setting("ios_url")
        windows_url = get_setting("windows_url")
        linux_url = get_setting("linux_url")

        await callback.message.edit_text(
            "Выберите вашу платформу для получения инструкции:",
            reply_markup=keyboards.create_howto_vless_keyboard(
            android_url=android_url,
            windows_url=windows_url,
            ios_url=ios_url,
            linux_url=linux_url),
            disable_web_page_preview=False
        )

    @user_router.callback_query(F.data == "buy_new_key")
    @registration_required
    async def buy_new_key_handler(callback: types.CallbackQuery):
        await callback.answer()
        hosts = get_all_hosts()
        if not hosts:
            await callback.message.edit_text("❌ В данный момент нет доступных серверов для покупки.")
            return

        await callback.message.edit_text(
            "Выберите сервер, на котором хотите приобрести ключ:",
            reply_markup=keyboards.create_host_selection_keyboard(hosts, action="new")
        )

    @user_router.callback_query(F.data.startswith("select_host_new_"))
    @registration_required
    async def select_host_for_purchase_handler(callback: types.CallbackQuery):
        await callback.answer()
        host_name = callback.data[len("select_host_new_"):]
        plans = get_plans_for_host(host_name)
        if not plans:
            await callback.message.edit_text(f"❌ Для сервера \"{host_name}\" не настроены тарифы.")
            return
        await callback.message.edit_text(
            "Выберите тариф для нового ключа:",
            reply_markup=keyboards.create_plans_keyboard(plans, action="new", host_name=host_name)
        )

    @user_router.callback_query(F.data.startswith("extend_key_"))
    @registration_required
    async def extend_key_handler(callback: types.CallbackQuery):
        await callback.answer()

        try:
            key_id = int(callback.data.split("_")[2])
        except (IndexError, ValueError):
            await callback.message.edit_text("❌ Произошла ошибка. Неверный формат ключа.")
            return

        key_data = get_key_by_id(key_id)

        if not key_data or key_data['user_id'] != callback.from_user.id:
            await callback.message.edit_text("❌ Ошибка: Ключ не найден или не принадлежит вам.")
            return

        host_name = key_data.get('host_name')
        if not host_name:
            await callback.message.edit_text("❌ Ошибка: У этого ключа не указан сервер. Обратитесь в поддержку.")
            return

        plans = get_plans_for_host(host_name)

        if not plans:
            await callback.message.edit_text(
                f"❌ Извините, для сервера \"{host_name}\" в данный момент не настроены тарифы для продления."
            )
            return

        await callback.message.edit_text(
            f"Выберите тариф для продления ключа на сервере \"{host_name}\":",
            reply_markup=keyboards.create_plans_keyboard(
                plans=plans,
                action="extend",
                host_name=host_name,
                key_id=key_id
            )
        )

    @user_router.callback_query(F.data.startswith("buy_"))
    @registration_required
    async def plan_selection_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()

        try:
            parts = callback.data.split('_')[1:]
            action, key_id, plan_id = parts[-2], int(parts[-1]), int(parts[-3])
            host_name = '_'.join(parts[:-3])
            plan = get_plan_by_id(plan_id)
            if not plan or plan['host_name'] != host_name or action not in ('new', 'extend'):
                raise ValueError('Invalid plan')
            if action == 'extend':
                key = get_key_by_id(key_id)
                if not key or key['user_id'] != callback.from_user.id or key['host_name'] != host_name:
                    raise ValueError('Invalid key owner or host')
            elif key_id != 0:
                raise ValueError('Invalid new-key request')
        except (IndexError, ValueError):
            await state.clear()
            await callback.message.answer('Неверный тариф или ключ. Откройте меню заново.')
            return

        await state.update_data(
            action=action, key_id=key_id, plan_id=plan_id, host_name=host_name
        )

        await callback.message.edit_text(
            "📧 Пожалуйста, введите ваш email для отправки чека об оплате.\n\n"
            "Если вы не хотите указывать почту, нажмите кнопку ниже.",
            reply_markup=keyboards.create_skip_email_keyboard()
        )
        await state.set_state(PaymentProcess.waiting_for_email)

    @user_router.callback_query(PaymentProcess.waiting_for_email, F.data == "back_to_plans")
    async def back_to_plans_handler(callback: types.CallbackQuery, state: FSMContext):
        data = await state.get_data()
        await state.clear()

        action = data.get('action')

        if action == 'new':
            await buy_new_key_handler(callback)
        elif action == 'extend':
            await extend_key_handler(callback)
        else:
            await back_to_main_menu_handler(callback)

    @user_router.message(PaymentProcess.waiting_for_email)
    async def process_email_handler(message: types.Message, state: FSMContext):
        if is_valid_email(message.text):
            await state.update_data(customer_email=message.text)
            await message.answer(f"✅ Email принят: {message.text}")

            data = await state.get_data()
            await message.answer(
                CHOOSE_PAYMENT_METHOD_MESSAGE,
                reply_markup=keyboards.create_payment_method_keyboard(
                    payment_methods=PAYMENT_METHODS,
                    action=data.get('action'),
                    key_id=data.get('key_id')
                )
            )
            await state.set_state(PaymentProcess.waiting_for_payment_method)
            logger.info(f"User {message.chat.id}: State set to waiting_for_payment_method")
        else:
            await message.answer("❌ Неверный формат email. Попробуйте еще раз.")

    @user_router.callback_query(PaymentProcess.waiting_for_email, F.data == "skip_email")
    async def skip_email_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await state.update_data(customer_email=None)

        data = await state.get_data()
        await callback.message.edit_text(
            CHOOSE_PAYMENT_METHOD_MESSAGE,
            reply_markup=keyboards.create_payment_method_keyboard(
                payment_methods=PAYMENT_METHODS,
                action=data.get('action'),
                key_id=data.get('key_id')
            )
        )
        await state.set_state(PaymentProcess.waiting_for_payment_method)
        logger.info(f"User {callback.from_user.id}: State set to waiting_for_payment_method")

    async def show_payment_options(message: types.Message, state: FSMContext):
        data = await state.get_data()
        user_data = get_user(message.chat.id)
        plan = get_plan_by_id(data.get('plan_id'))

        if not plan:
            await message.edit_text("❌ Ошибка: Тариф не найден.")
            await state.clear()
            return

        price = Decimal(str(plan['price']))
        final_price = price
        discount_applied = False
        message_text = CHOOSE_PAYMENT_METHOD_MESSAGE

        if user_data.get('referred_by') and user_data.get('total_spent', 0) == 0:
            discount_percentage_str = get_setting("referral_discount") or "0"
            discount_percentage = Decimal(discount_percentage_str)

            if discount_percentage > 0:
                discount_amount = (price * discount_percentage / 100).quantize(Decimal("0.01"))
                final_price = price - discount_amount

                message_text = (
                    f"🎉 Как приглашенному пользователю, на вашу первую покупку предоставляется скидка {discount_percentage_str}%!\n"
                    f"Старая цена: <s>{price:.2f} RUB</s>\n"
                    f"<b>Новая цена: {final_price:.2f} RUB</b>\n\n"
                ) + CHOOSE_PAYMENT_METHOD_MESSAGE

        await state.update_data(final_price=float(final_price))

        await message.edit_text(
            message_text,
            reply_markup=keyboards.create_payment_method_keyboard(
                payment_methods=PAYMENT_METHODS,
                action=data.get('action'),
                key_id=data.get('key_id')
            )
        )
        await state.set_state(PaymentProcess.waiting_for_payment_method)

    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "back_to_email_prompt")
    async def back_to_email_prompt_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.message.edit_text(
            "📧 Пожалуйста, введите ваш email для отправки чека об оплате.\n\n"
            "Если вы не хотите указывать почту, нажмите кнопку ниже.",
            reply_markup=keyboards.create_skip_email_keyboard()
        )
        await state.set_state(PaymentProcess.waiting_for_email)


    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_yookassa")
    async def create_yookassa_payment_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("Создаю ссылку на оплату...")

        data = await state.get_data()
        user_data = get_user(callback.from_user.id)

        plan_id = data.get('plan_id')
        plan = get_plan_by_id(plan_id)

        if not plan:
            await callback.message.answer("Произошла ошибка при выборе тарифа.")
            await state.clear()
            return

        base_price = Decimal(str(plan['price']))
        price_rub = base_price

        if user_data.get('referred_by') and user_data.get('total_spent', 0) == 0:
            discount_percentage_str = get_setting("referral_discount") or "0"
            discount_percentage = Decimal(discount_percentage_str)
            if discount_percentage > 0:
                discount_amount = (base_price * discount_percentage / 100).quantize(Decimal("0.01"))
                price_rub = base_price - discount_amount

        plan_id = data.get('plan_id')
        customer_email = data.get('customer_email')
        host_name = data.get('host_name')
        action = data.get('action')
        key_id = data.get('key_id')

        if not customer_email:
            customer_email = get_setting("receipt_email")

        plan = get_plan_by_id(plan_id)
        if not plan:
            await callback.message.answer("Произошла ошибка при выборе тарифа.")
            await state.clear()
            return

        months = plan['months']
        user_id = callback.from_user.id

        try:
            price_str_for_api = f"{price_rub:.2f}"
            price_float_for_metadata = float(price_rub)

            receipt = None
            if customer_email and is_valid_email(customer_email):
                receipt = {
                    "customer": {"email": customer_email},
                    "items": [{
                        "description": f"Подписка на {months} мес.",
                        "quantity": "1.00",
                        "amount": {"value": price_str_for_api, "currency": "RUB"},
                        "vat_code": "1"
                    }]
                }
            payment_payload = {
                "amount": {"value": price_str_for_api, "currency": "RUB"},
                "confirmation": {"type": "redirect", "return_url": f"https://t.me/{TELEGRAM_BOT_USERNAME}"},
                "capture": True,
                "description": f"Подписка на {months} мес.",
                "metadata": {
                    "user_id": user_id, "months": months, "price": price_float_for_metadata,
                    "action": action, "key_id": key_id, "host_name": host_name,
                    "plan_id": plan_id, "customer_email": customer_email,
                    "payment_method": "YooKassa"
                }
            }
            if receipt:
                payment_payload['receipt'] = receipt

            payment = await asyncio.to_thread(Payment.create, payment_payload, str(uuid.uuid4()))
            if not create_pending_transaction(payment.id, user_id, price_float_for_metadata, payment_payload['metadata']):
                raise RuntimeError('Could not persist invoice')

            await state.clear()

            await callback.message.edit_text(
                "Нажмите на кнопку ниже для оплаты:",
                reply_markup=keyboards.create_payment_keyboard(payment.confirmation.confirmation_url)
            )
        except Exception as e:
            logger.error(f"Failed to create YooKassa payment: {e}", exc_info=True)
            await callback.message.answer("Не удалось создать ссылку на оплату.")
            await state.clear()

    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_cryptobot")
    async def create_cryptobot_invoice_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("Создаю счет в Crypto Pay...")

        data = await state.get_data()
        user_data = get_user(callback.from_user.id)

        plan_id = data.get('plan_id')
        user_id = data.get('user_id', callback.from_user.id)
        customer_email = data.get('customer_email')
        host_name = data.get('host_name')
        action = data.get('action')
        key_id = data.get('key_id')

        cryptobot_token = get_setting('cryptobot_token')
        if not cryptobot_token:
            logger.error(f"Attempt to create Crypto Pay invoice failed for user {user_id}: cryptobot_token is not set.")
            await callback.message.edit_text("❌ Оплата криптовалютой временно недоступна. (Администратор не указал токен).")
            await state.clear()
            return

        plan = get_plan_by_id(plan_id)
        if not plan:
            logger.error(f"Attempt to create Crypto Pay invoice failed for user {user_id}: Plan with id {plan_id} not found.")
            await callback.message.edit_text("❌ Произошла ошибка при выборе тарифа.")
            await state.clear()
            return

        plan_id = data.get('plan_id')
        plan = get_plan_by_id(plan_id)

        if not plan:
            await callback.message.answer("Произошла ошибка при выборе тарифа.")
            await state.clear()
            return

        base_price = Decimal(str(plan['price']))
        price_rub = base_price

        if user_data.get('referred_by') and user_data.get('total_spent', 0) == 0:
            discount_percentage_str = get_setting("referral_discount") or "0"
            discount_percentage = Decimal(discount_percentage_str)
            if discount_percentage > 0:
                discount_amount = (base_price * discount_percentage / 100).quantize(Decimal("0.01"))
                price_rub = base_price - discount_amount
        months = plan['months']

        try:
            payload_data = str(uuid.uuid4())
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                async with session.post('https://pay.crypt.bot/api/createInvoice',
                        headers={'Crypto-Pay-API-Token': cryptobot_token},
                        json={'currency_type': 'fiat', 'fiat': 'RUB', 'amount': str(price_rub),
                              'description': f'Подписка на {months} мес.', 'payload': payload_data,
                              'expires_in': 3600}, allow_redirects=False) as response:
                    invoice_data = await response.json()
                    if response.status != 200 or not invoice_data.get('ok'):
                        raise RuntimeError('Crypto Pay rejected invoice')
                    invoice = invoice_data['result']
            payment_url = invoice.get('bot_invoice_url') or invoice.get('pay_url')
            if not payment_url or not invoice.get('invoice_id'):
                raise RuntimeError('Incomplete Crypto Pay invoice')

            metadata = {"user_id": user_id, "months": months, "price": float(price_rub),
                        "action": action, "key_id": key_id, "host_name": host_name,
                        "plan_id": plan_id, "customer_email": customer_email, "payment_method": "CryptoBot"}
            if not create_pending_transaction(str(invoice['invoice_id']), user_id, float(price_rub), metadata):
                raise RuntimeError('Could not persist invoice')
            await callback.message.edit_text(
                "Нажмите на кнопку ниже для оплаты:",
                reply_markup=keyboards.create_payment_keyboard(payment_url)
            )
            await state.clear()

        except Exception as e:
            logger.error(f"Failed to create Crypto Pay invoice for user {user_id}: {e}", exc_info=True)
            await callback.message.edit_text(f"❌ Не удалось создать счет для оплаты криптовалютой.\n\n<pre>Ошибка: {e}</pre>")
            await state.clear()

    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_heleket")
    async def create_heleket_invoice_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("Создаю счет Heleket...")

        data = await state.get_data()
        plan = get_plan_by_id(data.get('plan_id'))
        user_data = get_user(callback.from_user.id)

        if not plan:
            await callback.message.edit_text("❌ Произошла ошибка при выборе тарифа.")
            await state.clear()
            return

        plan_id = data.get('plan_id')
        plan = get_plan_by_id(plan_id)

        if not plan:
            await callback.message.answer("Произошла ошибка при выборе тарифа.")
            await state.clear()
            return

        base_price = Decimal(str(plan['price']))
        price_rub_decimal = base_price

        if user_data.get('referred_by') and user_data.get('total_spent', 0) == 0:
            discount_percentage_str = get_setting("referral_discount") or "0"
            discount_percentage = Decimal(discount_percentage_str)
            if discount_percentage > 0:
                discount_amount = (base_price * discount_percentage / 100).quantize(Decimal("0.01"))
                price_rub_decimal = base_price - discount_amount
        months = plan['months']

        final_price_float = float(price_rub_decimal)

        pay_url = await _create_heleket_payment_request(
            user_id=callback.from_user.id,
            price=final_price_float,
            months=plan['months'],
            host_name=data.get('host_name'),
            state_data=data
        )

        if pay_url:
            await callback.message.edit_text(
                "Нажмите на кнопку ниже для оплаты:",
                reply_markup=keyboards.create_payment_keyboard(pay_url)
            )
            await state.clear()
        else:
            await callback.message.edit_text("❌ Не удалось создать счет Heleket. Попробуйте другой способ оплаты.")

    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_tonconnect")
    async def create_ton_invoice_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer('TON временно отключён: нет безопасной проверки платежей.', show_alert=True)
        await state.clear()

    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_lava")
    async def create_lava_invoice_handler(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
        await callback.answer("Создаю счет СБП через Lava.top...")

        data = await state.get_data()
        plan_id = data.get('plan_id')
        plan = get_plan_by_id(plan_id)
        user_data = get_user(callback.from_user.id)

        if not plan:
            await callback.message.edit_text("❌ Произошла ошибка при выборе тарифа.")
            await state.clear()
            return

        base_price = Decimal(str(plan['price']))
        price_rub_decimal = base_price

        if user_data and user_data.get('referred_by') and user_data.get('total_spent', 0) == 0:
            discount_percentage_str = get_setting("referral_discount") or "0"
            discount_percentage = Decimal(discount_percentage_str)
            if discount_percentage > 0:
                discount_amount = (base_price * discount_percentage / 100).quantize(Decimal("0.01"))
                price_rub_decimal = base_price - discount_amount

        final_price_float = float(price_rub_decimal)
        months = plan['months']
        user_id = callback.from_user.id
        host_name = data.get('host_name')
        action = data.get('action', 'new')
        key_id = data.get('key_id')
        customer_email = data.get('customer_email') or f"user{user_id}@telegram.bot"

        lava_api_key = get_setting("lava_api_key")
        lava_offer_id = get_setting("lava_offer_id")
        lava_sbp_only = get_setting("lava_sbp_only") != "false"
        bot_username = get_setting("telegram_bot_username")

        if not lava_api_key or not lava_offer_id:
            await callback.message.edit_text("❌ Оплата через Lava.top временно недоступна (не настроены ключи в панели).")
            await state.clear()
            return

        invoice_result = await lava_api.create_invoice(
            amount=final_price_float,
            email=customer_email,
            api_key=lava_api_key,
            offer_id=lava_offer_id,
            bot_username=bot_username,
            sbp_mode=lava_sbp_only
        )

        if not invoice_result or not invoice_result.get("payment_url"):
            await callback.message.edit_text("❌ Не удалось создать счет на оплату в Lava.top. Попробуйте позже или выберите другой способ.")
            await state.clear()
            return

        payment_id = invoice_result["invoice_id"]
        payment_url = invoice_result["payment_url"]

        metadata = {
            "user_id": user_id,
            "months": months,
            "price": final_price_float,
            "action": action,
            "key_id": key_id,
            "host_name": host_name,
            "plan_id": plan_id,
            "customer_email": customer_email,
            "payment_method": "Lava.top SBP" if lava_sbp_only else "Lava.top"
        }

        if not create_pending_transaction(payment_id, user_id, final_price_float, metadata):
            await callback.message.edit_text('Не удалось сохранить счёт. Не оплачивайте его; обратитесь в поддержку.')
            return

        method_name = "⚡ СБП (Система быстрых платежей)" if lava_sbp_only else "💳 Lava.top"
        text = (
            f"<b>Счет на оплату готов!</b>\n\n"
            f"<b>Способ оплаты:</b> {method_name}\n"
            f"<b>Сумма к оплате:</b> <code>{final_price_float:.2f} RUB</code>\n"
            f"<b>Тариф:</b> {plan['plan_name']} ({months} мес.)\n\n"
            f"Нажмите кнопку <b>«Оплатить через СБП»</b> для перехода в банковское приложение.\n"
            f"После завершения платежа нажмите <b>«Проверить оплату»</b>."
        )

        await callback.message.edit_text(
            text,
            reply_markup=keyboards.create_lava_payment_keyboard(payment_url, payment_id),
            parse_mode="HTML"
        )
        await state.clear()

    @user_router.callback_query(F.data.startswith("check_lava_"))
    async def check_lava_payment_handler(callback: types.CallbackQuery, bot: Bot):
        contract_id = callback.data.removeprefix('check_lava_').strip()
        row = get_payment(contract_id)
        if not row or row['user_id'] != callback.from_user.id or json.loads(row['metadata']).get('payment_method') not in ('Lava.top', 'Lava.top SBP'):
            return await callback.answer('Счёт не найден.', show_alert=True)
        if row['status'] != 'pending':
            return await callback.answer('Статус: ' + row['status'] + '. При задержке обратитесь в поддержку.', show_alert=True)
        await callback.answer('Проверяю оплату...')
        status = await lava_api.get_invoice_status(contract_id, get_setting('lava_api_key'))
        if not status or not status.get('is_paid'):
            await callback.message.answer('Оплата пока не подтверждена. Попробуйте позже.')
            return
        if not lava_api.amount_matches(status.get('amount'), status.get('currency'), row['amount_rub']):
            await callback.message.answer('Сумма или валюта платежа не совпадает со счётом. Обратитесь в поддержку; повторно платить не нужно.')
            return
        mark_payment_verified(contract_id)
        success = await deliver_payment(bot, contract_id, 'Lava.top', callback.from_user.id)
        if success:
            await callback.message.edit_text('Оплата подтверждена. Подписка доступна в разделе «Мои ключи».')
        else:
            await callback.message.answer('Платёж обрабатывается или требует проверки поддержки. Повторно платить не нужно.')

    @user_router.message(F.text)
    @registration_required
    async def unknown_message_handler(message: types.Message):
        if message.text.startswith('/'):
            await message.answer("Такой команды не существует. Попробуйте /start.")
        else:
            await message.answer("Я не понимаю эту команду. Пожалуйста, используйте кнопки меню.")
    return user_router

async def process_successful_onboarding(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("✅ Спасибо! Доступ предоставлен.")
    set_terms_agreed(callback.from_user.id)
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("Приятного использования!", reply_markup=keyboards.main_reply_keyboard)
    await show_main_menu(callback.message)

async def is_url_reachable(url: str) -> bool:
    pattern = re.compile(
        r'^(https?://)'
        r'(([a-zA-Z0-9-]+\.)+[a-zA-Z]{2,})'
        r'(/.*)?$'
    )
    if not re.match(pattern, url):
        return False

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.head(url, allow_redirects=True) as response:
                return response.status < 400
    except Exception as e:
        logger.warning(f"URL validation failed for {url}. Error: {e}")
        return False

async def notify_admin_of_purchase(bot: Bot, metadata: dict):
    if not ADMIN_ID:
        logger.warning("Admin notification skipped: ADMIN_ID is not set.")
        return

    try:
        user_id = metadata.get('user_id')
        months = metadata.get('months')
        price = float(metadata.get('price'))
        host_name = metadata.get('host_name')
        plan_id = metadata.get('plan_id')
        payment_method = metadata.get('payment_method', 'Unknown')

        user_info = get_user(user_id)
        plan_info = get_plan_by_id(plan_id)

        username = user_info.get('username', 'N/A') if user_info else 'N/A'
        plan_name = plan_info.get('plan_name', f'{months} мес.') if plan_info else f'{months} мес.'

        message_text = (
            "🎉 **Новая покупка!** 🎉\n\n"
            f"👤 **Пользователь:** @{username} (ID: `{user_id}`)\n"
            f"🌍 **Сервер:** {host_name}\n"
            f"📄 **Тариф:** {plan_name}\n"
            f"💰 **Сумма:** {price:.2f} RUB\n"
            f"💳 **Способ оплаты:** {payment_method}"
        )

        await bot.send_message(
            chat_id=ADMIN_ID,
            text=message_text,
            parse_mode='Markdown'
        )
        logger.info(f"Admin notification sent for a new purchase by user {user_id}.")

    except Exception as e:
        logger.error(f"Failed to send admin notification for purchase: {e}", exc_info=True)

async def _create_heleket_payment_request(user_id: int, price: float, months: int, host_name: str, state_data: dict) -> str | None:
    merchant_id = get_setting("heleket_merchant_id")
    api_key = get_setting("heleket_api_key")
    bot_username = get_setting("telegram_bot_username")
    domain = get_setting("domain")

    if not all([merchant_id, api_key, bot_username, domain]):
        logger.error("Heleket Error: Not all required settings are configured.")
        return None

    redirect_url = f"https://t.me/{bot_username}"
    order_id = str(uuid.uuid4())

    metadata = {
        "user_id": user_id, "months": months, "price": float(price),
        "action": state_data.get('action'), "key_id": state_data.get('key_id'),
        "host_name": host_name, "plan_id": state_data.get('plan_id'),
        "customer_email": state_data.get('customer_email'), "payment_method": "Heleket"
    }

    payload = {
        "amount": f"{price:.2f}",
        "currency": "RUB",
        "order_id": order_id,
        "description": json.dumps(metadata),
        "url_return": redirect_url,
        "url_success": redirect_url,
        "url_callback": f"https://{domain}/heleket-webhook",
        "lifetime": 1800,
        "is_payment_multiple": False
    }

    headers = {
        "merchant": merchant_id,
        "sign": _generate_heleket_signature(json.dumps(payload), api_key),
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            url = "https://api.heleket.com/v1/payment"
            async with session.post(url, json=payload, headers=headers) as response:
                result = await response.json()
                if response.status == 200 and result.get("result", {}).get("url"):
                    if not create_pending_transaction(order_id, user_id, price, metadata):
                        return None
                    return result["result"]["url"]
                else:
                    logger.error(f"Heleket API Error: Status {response.status}, Result: {result}")
                    return None
    except Exception as e:
        logger.error(f"Heleket request failed: {e}", exc_info=True)
        return None

def _generate_heleket_signature(data, api_key: str) -> str:
    if isinstance(data, dict):
        data_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    else:
        data_str = str(data)
    base64_encoded = base64.b64encode(data_str.encode()).decode()
    raw_string = f"{base64_encoded}{api_key}"
    return hashlib.md5(raw_string.encode()).hexdigest()

async def get_usdt_rub_rate() -> Decimal | None:
    url = "https://api.binance.com/api/v3/ticker/price"
    params = {"symbol": "USDTRUB"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                price_str = data.get('price')
                if price_str:
                    logger.info(f"Got USDT RUB: {price_str}")
                    return Decimal(price_str)
                logger.error("Can't find 'price' in Binance response.")
                return None
    except Exception as e:
        logger.error(f"Error getting USDT RUB Binance rate: {e}", exc_info=True)
        return None

async def get_ton_usdt_rate() -> Decimal | None:
    url = "https://api.binance.com/api/v3/ticker/price"
    params = {"symbol": "TONUSDT"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                price_str = data.get('price')
                if price_str:
                    logger.info(f"Got TON USDT: {price_str}")
                    return Decimal(price_str)
                logger.error("Can't find 'price' in Binance response.")
                return None
    except Exception as e:
        logger.error(f"Error getting TON USDT Binance rate: {e}", exc_info=True)
        return None

_payment_locks = WeakValueDictionary()


async def deliver_payment(bot: Bot, payment_id: str, method: str, user_id=None) -> bool:
    row = get_payment(payment_id)
    if not row:
        return False
    lock = _payment_locks.setdefault(row['user_id'], asyncio.Lock())
    async with lock:
        return await _deliver_payment_locked(bot, payment_id, method, user_id)


async def _deliver_payment_locked(bot: Bot, payment_id: str, method: str, user_id=None) -> bool:
    metadata = find_and_complete_pending_transaction(payment_id, method, user_id)
    if not metadata:
        return False
    successful = False
    try:
        successful = bool(await process_successful_payment(bot, metadata))
        return successful
    except Exception:
        logger.exception('Payment issuance requires manual reconciliation')
        return False
    finally:
        finish_payment(payment_id, successful)


async def process_successful_payment(bot: Bot, metadata: dict):
    try:
        user_id = int(metadata['user_id'])
        months = int(metadata['months'])
        price = float(metadata['price'])
        action = metadata['action']
        key_id = int(metadata['key_id'])
        host_name = metadata['host_name']
        plan_id = int(metadata['plan_id'])
        customer_email = metadata.get('customer_email')
        payment_method = metadata.get('payment_method')
        user = get_user(user_id)
        if not user or user.get('is_banned') or action not in ('new', 'extend') or not 1 <= months <= 1200 or not math.isfinite(price) or price <= 0:
            return False

        chat_id_to_delete = metadata.get('chat_id')
        message_id_to_delete = metadata.get('message_id')

    except (KeyError, ValueError, TypeError) as e:
        logger.error(f"FATAL: Could not parse metadata. Error: {e}. Metadata: {metadata}")
        return

    if chat_id_to_delete and message_id_to_delete:
        try:
            await bot.delete_message(chat_id=chat_id_to_delete, message_id=message_id_to_delete)
        except TelegramBadRequest as e:
            logger.warning(f"Could not delete payment message: {e}")

    # Provisioning must not depend on whether the buyer allows Telegram DMs.
    processing_message = None
    try:
        processing_message = await bot.send_message(chat_id=user_id, text='Оплата получена. Подготавливаю подписку.')
    except Exception:
        logger.warning('Could not send payment progress notification')
    try:
        email = ""
        if action == "new":
            key_number = get_next_key_number(user_id)
            email = generate_client_email(user_id, key_number, host_name)
        elif action == "extend":
            key_data = get_key_by_id(key_id)
            if not key_data or key_data['user_id'] != user_id or key_data['host_name'] != host_name:
                if processing_message:
                    await processing_message.edit_text('Ключ для продления не найден. Обратитесь в поддержку.')
                return
            email = key_data['key_email']

        metadata['key_email'] = email
        if metadata.get('payment_id'):
            record_payment_target(metadata['payment_id'], metadata)
        days_to_add = months * 30
        result = await xui_api.create_or_update_key_on_host(
            host_name=host_name,
            email=email,
            days_to_add=days_to_add
        )

        if not result:
            if processing_message:
                await processing_message.edit_text('Выдача требует проверки поддержки. Повторно оплачивать не нужно.')
            return

        if action == "new":
            key_id = add_new_key(user_id, host_name, result['client_uuid'], result['email'], result['expiry_timestamp_ms'])
        elif action == "extend":
            update_key_info(key_id, result['client_uuid'], result['expiry_timestamp_ms'])
        saved_key = get_key_by_id(key_id) if key_id else None
        if (not saved_key or saved_key['xui_client_uuid'] != str(result['client_uuid'])
                or abs(datetime.fromisoformat(saved_key['expiry_date']).timestamp() * 1000 - result['expiry_timestamp_ms']) > 1):
            return False
        if result.get('federation_ok') is False:
            logger.error('Subscription saved but federation is incomplete; admin must retry deploy')
            try:
                await bot.send_message(get_setting('admin_telegram_id'), f'Подписка {key_id}: не все ноды подтвердили довыпуск. Проверьте федерацию в панели RIXXX.')
            except Exception:
                logger.warning('Could not notify administrator of partial federation')

        metadata['issued_key_id'] = key_id
        if metadata.get('payment_id'):
            record_payment_target(metadata['payment_id'], metadata)
        price = float(metadata.get('price'))

        user_data = get_user(user_id)
        referrer_id = (user_data or {}).get('referred_by')

        if referrer_id and get_setting('enable_referrals') == 'true':
            referrer_user_data = get_user(referrer_id)
            if referrer_user_data and referrer_user_data.get('custom_referral_percentage') is not None:
                percentage = Decimal(str(referrer_user_data.get('custom_referral_percentage')))
            else:
                percentage = Decimal(get_setting("referral_percentage") or "0")

            reward = (Decimal(str(price)) * percentage / 100).quantize(Decimal("0.01"))

            if float(reward) > 0:
                add_to_referral_balance(referrer_id, float(reward))

                try:
                    referrer_username = user_data.get('username', 'пользователь')
                    await bot.send_message(
                        referrer_id,
                        f"🎉 Ваш реферал @{referrer_username} совершил покупку на сумму {price:.2f} RUB!\n"
                        f"💰 На ваш баланс начислено вознаграждение: {reward:.2f} RUB."
                    )
                except Exception as e:
                    logger.warning(f"Could not send referral reward notification to {referrer_id}: {e}")

        update_user_stats(user_id, price, months)

        user_info = get_user(user_id)

        # The original invoice row is the sole financial record (no duplicate sale).
        if processing_message:
            try:
                await processing_message.delete()
            except Exception:
                logger.warning('Could not remove payment progress message')

        connection_string = result['connection_string']
        new_expiry_date = datetime.fromtimestamp(result['expiry_timestamp_ms'] / 1000)

        all_user_keys = get_user_keys(user_id)
        key_number = next((i + 1 for i, key in enumerate(all_user_keys) if key['key_id'] == key_id), len(all_user_keys))

        final_text = get_purchase_success_text(
            action=action,
            key_number=key_number,
            expiry_date=new_expiry_date,
            connection_string=connection_string
        )

        try:
            await bot.send_message(chat_id=user_id, text=final_text,
                                   reply_markup=keyboards.create_key_info_keyboard(key_id))
            await notify_admin_of_purchase(bot, metadata)
        except Exception:
            logger.warning('Subscription saved but Telegram notification failed')
        return True

    except Exception:
        logger.exception('Payment issuance failed; manual reconciliation required')
        return False


async def revoke_user_access(user_id: int) -> tuple[int, int]:
    keys = get_user_keys(user_id)
    success = 0
    for key in keys:
        if await xui_api.delete_client_on_host(key['host_name'], key['key_email']):
            delete_key_by_email(key['key_email'])
            success += 1
    return success, len(keys)
