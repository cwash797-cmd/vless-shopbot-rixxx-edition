from html import escape

CHOOSE_PLAN_MESSAGE = "Выберите подходящий тариф:"
CHOOSE_PAYMENT_METHOD_MESSAGE = "Выберите удобный способ оплаты:"
VPN_INACTIVE_TEXT = "❌ <b>Статус подписки:</b> Неактивна (срок истек)"
VPN_NO_DATA_TEXT = "ℹ️ <b>Статус подписки:</b> У вас пока нет активных подписок."

def get_profile_text(username, total_spent, total_months, vpn_status_text):
    return (
        f"👤 <b>Профиль:</b> {username}\n\n"
        f"💰 <b>Потрачено всего:</b> {total_spent:.0f} RUB\n"
        f"📅 <b>Приобретено месяцев:</b> {total_months}\n\n"
        f"{vpn_status_text}"
    )

def get_vpn_active_text(days_left, hours_left):
    return (
        f"✅ <b>Статус подписки:</b> Активна\n"
        f"⏳ <b>Осталось:</b> {days_left} д. {hours_left} ч."
    )

def get_key_info_text(key_number, expiry_date, created_date, connection_string):
    expiry_formatted = expiry_date.strftime('%d.%m.%Y в %H:%M')
    created_formatted = created_date.strftime('%d.%m.%Y в %H:%M')
    
    return (
        f"<b>🔑 Ваша умная подписка #{key_number}</b>\n\n"
        f"<b>➕ Приобретена:</b> {created_formatted}\n"
        f"<b>⏳ Действительна до:</b> {expiry_formatted}\n\n"
        f"Скопируйте эту ссылку и вставьте в Karing или Shadowrocket:\n\n"
        f"<code>{escape(connection_string)}</code>"
    )

def get_purchase_success_text(action: str, key_number: int, expiry_date, connection_string: str):
    action_text = "обновлена" if action == "extend" else "готова"
    expiry_formatted = expiry_date.strftime('%d.%m.%Y в %H:%M')

    return (
        f"🎉 <b>Ваша подписка #{key_number} {action_text}!</b>\n\n"
        f"⏳ <b>Она будет действовать до:</b> {expiry_formatted}\n\n"
        f"Для подключения скопируйте эту ссылку:\n\n"
        f"<code>{escape(connection_string)}</code>"
    )
