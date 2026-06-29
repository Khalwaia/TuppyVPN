import logging
import secrets
from aiogram import Router, F, Bot
from aiogram.types import Message, ChatMember, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramForbiddenError
import uuid
import aiosqlite
import aiohttp
import time
import datetime
import asyncio
from aiogram.types import (
    Message, ChatMember, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, WebAppInfo, FSInputFile
)

from yookassa import Configuration, Payment
from aiocryptopay import AioCryptoPay, Networks

from config_reader import config

logger = logging.getLogger(__name__)

# --- КОНФИГУРАЦИЯ ---
router = Router()

import os
DB_NAME = os.environ.get("DB_PATH", "tuppy_vpn.db")
CHANNEL_ID = "@tuppyvpn"
SUPPORT_USERNAME = "Tuppy VPN Support"
SUPPORT_LINK = "https://t.me/tuppyvpnsup_bot"

# Читаем все секреты из config (который берёт их из .env)
ADMIN_ID = config.admin_id

# Настройки платежек
YOOKASSA_SHOP_ID = config.yookassa_shop_id
YOOKASSA_SECRET_KEY = config.yookassa_secret_key.get_secret_value()
CRYPTOBOT_TOKEN = config.cryptobot_token.get_secret_value()

# Цены и периоды
PRICE_1_MONTH_RUB = 79.00
PRICE_3_MONTHS_RUB = 169.00
PRICE_1_MONTH_USDT = 1
PRICE_3_MONTHS_USDT = 2.5

Configuration.account_id = YOOKASSA_SHOP_ID
Configuration.secret_key = YOOKASSA_SECRET_KEY

cryptopay = AioCryptoPay(token=CRYPTOBOT_TOKEN, network=Networks.MAIN_NET)

# --- НАСТРОЙКИ REMNAWAVE ---
# Адрес панели без слеша в конце
REMNAWAVE_Url = config.remnawave_url
Subscription_URL = config.subscription_url
REMNAWAVE_API_TOKEN = config.remnawave_api_token.get_secret_value()

class AdminState(StatesGroup):
    waiting_for_message = State()
    confirm_send = State()

class WithdrawState(StatesGroup):
    waiting_for_details = State()

# --- РАБОТА С БАЗОЙ ДАННЫХ ---

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                active BOOLEAN,
                expiry_time INTEGER,
                sub_id TEXT,
                trial_used BOOLEAN DEFAULT 0,
                referrer_id INTEGER DEFAULT NULL,
                balance REAL DEFAULT 0.0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                details TEXT,
                status TEXT DEFAULT 'pending',
                created_at INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                payment_id TEXT PRIMARY KEY,
                user_id INTEGER,
                amount REAL,
                currency TEXT,
                provider TEXT,
                status TEXT, 
                created_at INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promocodes (
                code TEXT PRIMARY KEY,
                days INTEGER,
                max_activations INTEGER,
                current_activations INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promo_activations (
                user_id INTEGER,
                code TEXT,
                activated_at INTEGER,
                PRIMARY KEY (user_id, code)
            )
        """)
        try: await db.execute("ALTER TABLE users ADD COLUMN trial_used BOOLEAN DEFAULT 0")
        except: pass
        try: await db.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER DEFAULT NULL")
        except: pass
        try: await db.execute("ALTER TABLE users ADD COLUMN balance REAL DEFAULT 0.0")
        except: pass
        await db.commit()

async def get_user(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row 
        async with db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()

async def add_user(user_id, username, referrer_id=None):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO users (id, username, active, expiry_time, sub_id, trial_used, referrer_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, False, 0, None, False, referrer_id)
        )
        await db.commit()

async def activate_subscription_db(user_id, expiry_time, sub_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET active = ?, expiry_time = ?, sub_id = ? WHERE id = ?",
            (True, expiry_time, sub_id, user_id)
        )
        await db.commit()

async def create_payment_record(payment_id, user_id, amount, currency, provider):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO payments (payment_id, user_id, amount, currency, provider, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(payment_id), user_id, amount, currency,
             provider, 'pending', int(time.time()))
        )
        await db.commit()

async def get_payment_record(payment_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM payments WHERE payment_id = ?", (str(payment_id),)) as cursor:
            return await cursor.fetchone()

async def mark_payment_completed(payment_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE payments SET status = 'completed' WHERE payment_id = ?", (str(payment_id),))
        await db.commit()

async def get_all_active_users():
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE active = 1") as cursor:
            return await cursor.fetchall()
        
async def get_referral_count(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,)) as cursor:
            result = await cursor.fetchone()
            return result[0] if result else 0

async def deactivate_user_in_db(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))
        await db.commit()

async def get_users_for_broadcast(target_type: str):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        if target_type == 'active':
            async with db.execute("SELECT id FROM users WHERE active = 1") as cursor:
                return await cursor.fetchall()
        else:
            async with db.execute("SELECT id FROM users") as cursor:
                return await cursor.fetchall()

async def add_bonus_days(user_id, days):
    user = await get_user(user_id)
    if not user:
        return False
    current_time = int(time.time())
    if user['expiry_time'] > current_time:
        new_expiry = user['expiry_time'] + (days * 24 * 60 * 60)
    else:
        new_expiry = current_time + (days * 24 * 60 * 60)
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET active = 1, expiry_time = ? WHERE id = ?", (new_expiry, user_id))
        await db.commit()
    
    try:
        await ensure_subscription_client(user_id, add_days=days)
    except:
        pass
    return True

# --- API REMNAWAVE (FIXED) ---

def get_auth_headers():
    # Используем статический токен API
    return {
        'Authorization': f'Bearer {REMNAWAVE_API_TOKEN}',
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }

async def get_client_by_tgid(user_telegram_id: int):
    headers = get_auth_headers()
    # Запрашиваем список, но фильтруем его ПРЯМО в коде на точное совпадение
    url = f"{REMNAWAVE_Url}/api/users?size=500" 
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    users = data.get('response', {}).get('users', [])
                    
                    for u in users:
                        # СТРОГАЯ ПРОВЕРКА: либо telegramId совпадает полностью,
                        # либо username равен "user_ID"
                        tg_id_in_panel = str(u.get('telegramId'))
                        username_in_panel = u.get('username')
                        
                        if tg_id_in_panel == str(user_telegram_id) or username_in_panel == f"user_{user_telegram_id}":
                            print(f"✅ Найден точный матч: {username_in_panel}")
                            return await get_full_user_info(u.get('uuid'))
                            
                print(f"⚠️ Точного совпадения для {user_telegram_id} не найдено.")
    except Exception as e:
        print(f"❌ Ошибка строгого поиска: {e}")
    return None

async def find_in_list_fallback(user_telegram_id: int):
    """Запасной поиск по списку из 200 человек"""
    headers = get_auth_headers()
    url = f"{REMNAWAVE_Url}/api/users?size=200"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    users = data.get('response', {}).get('users', [])
                    for u in users:
                        if str(u.get('telegramId')) == str(user_telegram_id):
                            print(f"✅ [DEBUG] Найден по Telegram ID в общем списке!")
                            return await get_full_user_info(u.get('uuid'))
    except: pass
    return None

async def get_full_user_info(user_uuid: str):
    """Получаем расширенную информацию о пользователе, включая HWID"""
    headers = get_auth_headers()
    url = f"{REMNAWAVE_Url}/api/users/{user_uuid}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Согласно схеме в api-1(1).json, данные лежат в response
                    return data.get('response', {})
    except Exception as e:
        print(f"❌ Ошибка получения полной инфо: {e}")
    return None

async def get_default_squad_id():
    """Ищет ID сквада по имени 'Default-Squad' по правильному пути API"""
    headers = get_auth_headers()
    # ИСПРАВЛЕНО: В вашем API путь именно /api/internal-squads
    url = f"{REMNAWAVE_Url}/api/internal-squads"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # В вашей схеме ответ: { "response": { "internalSquads": [...] } }
                    squads = data.get('response', {}).get('internalSquads', [])
                    
                    print(f"🔎 Найдены внутренние сквады: {[s.get('name') for s in squads]}")
                    
                    for s in squads:
                        if s.get('name') == "Default-Squad":
                            u_id = s.get('uuid')
                            print(f"✅ Успешно получен ID для Default-Squad: {u_id}")
                            return u_id
                    
                    if squads:
                        print(f"⚠️ Default-Squad не найден, использую первый: {squads[0].get('name')}")
                        return squads[0].get('uuid')
                else:
                    print(f"❌ Ошибка API {resp.status} на пути /api/internal-squads")
    except Exception as e:
        print(f"❌ Исключение при поиске сквада: {e}")
    return None

async def set_client_enable_status(user_telegram_id: int, enable: bool):
    client = await get_client_by_tgid(user_telegram_id)
    if not client: return False
    
    headers = get_auth_headers()
    url = f"{REMNAWAVE_Url}/api/users" # URL БЕЗ UUID
    payload = {
        "uuid": client['uuid'], # UUID ПЕРЕДАЕМ ТУТ
        "status": "ACTIVE" if enable else "DISABLED",
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.patch(url, json=payload, headers=headers) as resp:
                return resp.status == 200
    except Exception as e:
        print(f"Error toggling status: {e}")
        return False
    
def format_bytes(size) -> str:
    """Форматирует байты в читаемый вид. Безопасно обрабатывает None и 0."""
    if not size:
        return "0.00 B"
    power = 2**10
    n = 0
    power_labels = {0: 'B', 1: 'KB', 2: 'MB', 3: 'GB', 4: 'TB'}
    while size > power and n < 4:
        size /= power
        n += 1
    return f"{size:.2f} {power_labels[n]}"

# --- ПРЯМОЙ ЗАПРОС К API ПАНЕЛИ ---

async def get_user_info_direct(user_telegram_id: int):
    """Получает актуальные данные пользователя напрямую из панели Remnawave."""
    client_data = await get_client_by_tgid(user_telegram_id)
    if not client_data:
        return None

    # Парсим дату окончания из формата ISO в timestamp
    expire_at_str = client_data.get('expireAt')
    expiry_ts = 0
    if expire_at_str:
        try:
            clean_date = expire_at_str.replace('Z', '')
            expiry_ts = int(datetime.datetime.fromisoformat(clean_date).timestamp())
        except Exception:
            expiry_ts = 0

    return {
        'uuid': client_data.get('uuid'),
        'sub_id': client_data.get('shortUuid'),
        'status': client_data.get('status'), # 'ACTIVE' или 'DISABLED'
        'expiry_time': expiry_ts,
        'used_traffic': client_data.get('usedTraffic', 0)
    }

async def ensure_subscription_client(user_telegram_id: int, add_days: int = 30):
    headers = get_auth_headers()
    current_time = int(time.time())
    target_username = f"user_{user_telegram_id}"
    
    # 1. Пытаемся найти существующего клиента
    existing_client = await get_client_by_tgid(user_telegram_id)
    
    # Определяем базовое время для продления
    base_time = current_time
    if existing_client and existing_client.get('expireAt'):
        try:
            clean_date = existing_client['expireAt'].replace('Z', '')
            current_expiry = int(datetime.datetime.fromisoformat(clean_date).timestamp())
            base_time = max(current_time, current_expiry)
        except: pass

    new_expiry_ts = base_time + (add_days * 24 * 60 * 60)
    new_expiry_iso = datetime.datetime.fromtimestamp(new_expiry_ts, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + "Z"
    squad_uuid = await get_default_squad_id()
    
    url = f"{REMNAWAVE_Url}/api/users"
    
    # Общие данные для PATCH и POST
    payload = {
        "expireAt": new_expiry_iso,
        "status": "ACTIVE",
        "hwidDeviceLimit": 5,
        "trafficLimitStrategy": "NO_RESET",
        "trafficLimitBytes": 0,
        "telegramId": user_telegram_id
    }
    if squad_uuid:
        payload["activeInternalSquads"] = [squad_uuid]

    async with aiohttp.ClientSession() as session:
        # СИТУАЦИЯ А: Пользователь найден в панели — ОБНОВЛЯЕМ (PATCH)
        if existing_client:
            payload["uuid"] = existing_client['uuid']
            async with session.patch(url, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return 'UPDATED', data.get('response', {}).get('shortUuid') or existing_client.get('shortUuid'), new_expiry_ts
                else:
                    print(f"❌ Ошибка PATCH: {resp.status} - {await resp.text()}")

        # СИТУАЦИЯ Б: Пользователь НЕ найден — СОЗДАЕМ (POST)
        else:
            payload["username"] = target_username
            async with session.post(url, json=payload, headers=headers) as resp:
                resp_data = await resp.text()
                if resp.status in [200, 201]:
                    data = await resp.json()
                    return 'NEW', data.get('response', {}).get('shortUuid'), new_expiry_ts
                
                # Если всё равно лезет ошибка "Username exists", значит поиск по GET всё еще врет
                elif "A019" in resp_data:
                    print(f"‼️ Критическая рассинхронизация: API говорит, что {target_username} есть, но поиск его не видит.")
                    # В этом крайнем случае можно только посоветовать проверить права API-токена
                    # или вручную проверить, нет ли в панели юзера с таким же именем, но БЕЗ Telegram ID
                    
    return 'ERROR', None, None

async def force_find_client_by_username(target_username: str):
    """Поиск по всем пользователям без фильтров, если обычный поиск подвел"""
    headers = get_auth_headers()
    # В некоторых панелях нужно запрашивать большой лимит, чтобы увидеть всех
    url = f"{REMNAWAVE_Url}/api/users?size=1000" 
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    users = data.get('response', {}).get('users', [])
                    for u in users:
                        if u.get('username') == target_username:
                            # Возвращаем полную инфу
                            return await get_full_user_info(u.get('uuid'))
    except: pass
    return None

async def check_subscription(bot: Bot, user_id: int) -> bool:
    try:
        member: ChatMember = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ['creator', 'administrator', 'member', 'restricted']
    except Exception:
        return False

# --- ОБРАБОТЧИКИ АДМИНКИ ---

async def update_balance(user_id: int, amount: float):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, user_id))
        await db.commit()

async def create_withdrawal(user_id: int, amount: float, details: str):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "INSERT INTO withdrawals (user_id, amount, details, created_at) VALUES (?, ?, ?, ?)",
            (user_id, amount, details, int(time.time()))
        )
        await db.commit()
        return cursor.lastrowid

async def get_withdrawal(req_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM withdrawals WHERE id = ?", (req_id,)) as cursor:
            return await cursor.fetchone()
    
async def update_withdrawal_status(req_id: int, status: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE withdrawals SET status = ? WHERE id = ?", (status, req_id))
        await db.commit()

@router.message(Command("addpromo"))
async def add_promo_handler(message: Message):
    if message.from_user.id != ADMIN_ID: return

    try:
        args = message.text.split()
        if len(args) != 4:
            await message.answer("ℹ️ <b>Формат:</b> /addpromo [КОД] [ДНИ] [КОЛ-ВО]\nПример: <code>/addpromo NEWYEAR 30 100</code>", parse_mode="HTML")
            return
        
        code = args[1].upper().strip()
        days = int(args[2])
        limit = int(args[3])

        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT code FROM promocodes WHERE code = ?", (code,)) as cursor:
                if await cursor.fetchone():
                    await message.answer(f"❌ Код <code>{code}</code> уже существует!", parse_mode="HTML")
                    return
            
            await db.execute(
                "INSERT INTO promocodes (code, days, max_activations, current_activations) VALUES (?, ?, ?, 0)",
                (code, days, limit)
            )
            await db.commit()
        
        await message.answer(f"✅ <b>Промокод создан!</b>\n\nКод: <code>{code}</code>\nДней: {days}\nЛимит активаций: {limit}", parse_mode="HTML")

    except ValueError:
        await message.answer("❌ Ошибка: Дни и Лимит должны быть числами.")
    except Exception as e:
        await message.answer(f"❌ Ошибка базы данных: {e}")

@router.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID: return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Рассылка ВСЕМ", callback_data="admin_broadcast:all")],
        [InlineKeyboardButton(text="💎 Рассылка АКТИВНЫМ", callback_data="admin_broadcast:active")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="close_admin")]
    ])
    await message.answer("👑 <b>Панель управления</b>", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "close_admin")
async def close_admin_panel(callback: CallbackQuery):
    await callback.message.delete()

@router.callback_query(F.data.startswith("admin_broadcast:"))
async def start_broadcast(callback: CallbackQuery, state: FSMContext):
    target = callback.data.split(":")[1]
    await state.update_data(target_type=target)
    await state.set_state(AdminState.waiting_for_message)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="cancel_broadcast")]])
    await callback.message.edit_text("📝 <b>Пришлите сообщение для рассылки:</b>", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "cancel_broadcast")
async def cancel_broadcast(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Рассылка отменена.")

@router.message(AdminState.waiting_for_message)
async def process_broadcast_message(message: Message, state: FSMContext):
    await state.update_data(message_id=message.message_id, from_chat_id=message.chat.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить", callback_data="confirm_broadcast")],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="cancel_broadcast")]
    ])
    await message.answer("👁️ <b>Предпросмотр:</b>\nОтправить?", reply_markup=kb, parse_mode="HTML")
    await state.set_state(AdminState.confirm_send)

@router.callback_query(F.data == "confirm_broadcast", AdminState.confirm_send)
async def execute_broadcast(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    target_type = data.get('target_type')
    message_id = data.get('message_id')
    from_chat_id = data.get('from_chat_id')
    await state.clear()
    await callback.message.edit_text("🚀 <b>Рассылка запущена...</b>")
    users = await get_users_for_broadcast(target_type)
    for user in users:
        try:
            await bot.copy_message(chat_id=user['id'], from_chat_id=from_chat_id, message_id=message_id)
            await asyncio.sleep(0.05)
        except: pass
    await callback.message.answer("✅ <b>Рассылка завершена!</b>", parse_mode="HTML")

# --- ОБРАБОТЧИКИ ПОЛЬЗОВАТЕЛЕЙ ---

@router.message(CommandStart())
async def start(message: Message, bot: Bot):
    user_id = message.from_user.id
    name = message.from_user.first_name
    username = message.from_user.username
    display_username = f"@{username}" if username else f"ID: {user_id}"
    user = await get_user(user_id)
    if not user:
        referrer_id = None
        args = message.text.split()
        if len(args) > 1:
            try:
                possible_referrer = int(args[1])
                if possible_referrer != user_id:
                    ref_user = await get_user(possible_referrer)
                    if ref_user: referrer_id = possible_referrer
            except: pass
        await add_user(user_id, display_username, referrer_id)
        if referrer_id:
            # Убираем add_bonus_days
            referral_count = await get_referral_count(referrer_id)
            new_count = referral_count + 1 
            try: await bot.send_message(
                                chat_id=referrer_id,
                                text=f"🎉 <b>У вас новый реферал!</b>\n"
                                     f"Пользователь {name} присоединился по вашей ссылке.\n"
                                     f"Всего приглашено: {new_count}. Вы получите процент от его покупок!",
                                parse_mode="HTML"
                            )
            except: pass
    
    is_subscribed_to_channel = await check_subscription(bot, user_id)
    from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="❤️ Купить подписку"), KeyboardButton(text="👤 Информация")],
        [KeyboardButton(text="🎁 Пробный день"), KeyboardButton(text="🤝 Рефералы")],
        [KeyboardButton(text="🧑‍💻 Поддержка")]
    ], resize_keyboard=True)
    text = (
        f"👋 <b>Привет, {name}!</b>\n\n"
        f"Добро пожаловать в <b>Tuppy VPN</b> - ваш надежный доступ к свободному интернету.\n"
        f"🚀 Высокая скорость \n🛡 Анонимность \n🌍 Доступ к любым ресурсам"
    )
    if not is_subscribed_to_channel:
        text += f"\n\n⚠️ Подпишитесь на канал: {CHANNEL_ID}"
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@router.message(Command("promo"))
async def activate_promo_handler(message: Message, bot: Bot):
    user_id = message.from_user.id

    args = message.text.split()
    if len(args) != 2:
        await message.answer("ℹ️ Введите команду и код.\nПример: <code>/promo SUPER2025</code>", parse_mode="HTML")
        return

    code = args[1].upper().strip()

    # --- Предварительные проверки (быстро, без гонки) ---
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute("SELECT * FROM promocodes WHERE code = ?", (code,)) as cursor:
            promo = await cursor.fetchone()

        if not promo:
            await message.answer("❌ Такого промокода не существует.")
            return

        if promo['current_activations'] >= promo['max_activations']:
            await message.answer("❌ Этот промокод закончился (лимит исчерпан).")
            return

        async with db.execute(
            "SELECT 1 FROM promo_activations WHERE user_id = ? AND code = ?", (user_id, code)
        ) as cursor:
            if await cursor.fetchone():
                await message.answer("❌ Вы уже активировали этот промокод ранее.")
                return

    await message.answer("⏳ Активация промокода...")

    days_to_add = promo['days']

    status, sub_id, new_expiry = await ensure_subscription_client(user_id, add_days=days_to_add)

    if status != 'ERROR':
        current_ts = int(time.time())

        async with aiosqlite.connect(DB_NAME) as db:
            # --- FIX: Атомарный UPDATE с проверкой лимита прямо в SQL ---
            # Если между проверкой выше и этой строкой кто-то уже исчерпал лимит,
            # WHERE не выполнится и rowcount будет 0.
            cursor = await db.execute(
                "UPDATE promocodes SET current_activations = current_activations + 1 "
                "WHERE code = ? AND current_activations < max_activations",
                (code,)
            )
            if cursor.rowcount == 0:
                await db.rollback()
                await message.answer("❌ Промокод был только что исчерпан. Попробуйте другой.")
                return

            await db.execute(
                "UPDATE users SET active = 1, expiry_time = ?, sub_id = ? WHERE id = ?",
                (new_expiry, sub_id, user_id)
            )
            await db.execute(
                "INSERT INTO promo_activations (user_id, code, activated_at) VALUES (?, ?, ?)",
                (user_id, code, current_ts)
            )
            await db.commit()

        sub_link = f"{Subscription_URL}/{sub_id}"

        await message.answer(
            f"🎉 <b>Промокод активирован!</b>\n"
            f"✅ Добавлено дней: <b>{days_to_add}</b>\n\n"
            f"🔗 Ваша ссылка обновлена:\n<code>{sub_link}</code>",
            parse_mode="HTML"
        )

        await bot.send_message(
            ADMIN_ID,
            f"🎟 PROMO: {code}\nUser: {message.from_user.username} ({user_id})\nDays: +{days_to_add}"
        )

    else:
        await message.answer("⚠️ Ошибка активации на сервере (Remnawave). Попробуйте позже или напишите в поддержку.")

@router.message(F.text == "🎁 Пробный день")
async def free_trial_handler(message: Message, bot: Bot, state: FSMContext): 
    user_id = message.from_user.id
    
    current_state = await state.get_state()
    if current_state == "processing_trial":
        return 
        
    if not await check_subscription(bot, user_id):
        await message.answer(f"⛔ Подпишитесь на канал: {CHANNEL_ID}")
        return

    user = await get_user(user_id)
    username_user = message.from_user.username
    
    if user['trial_used']: 
        await message.answer("❌ Вы уже использовали пробный период.")
        return

    await state.set_state("processing_trial")
    await message.answer("⏳ Активация 24 часа...")

    status, sub_id, new_expiry = await ensure_subscription_client(user_id, add_days=1)
    
    if status != 'ERROR':
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET active=?, expiry_time=?, sub_id=?, trial_used=? WHERE id=?", 
                             (True, new_expiry, sub_id, True, user_id))
            await db.commit()
            
        sub_link = f"{Subscription_URL}/{sub_id}"
        await message.answer(f"🎉 <b>Готово!</b>\nСсылка:\n<code>{sub_link}</code>", parse_mode="HTML")
        await bot.send_message(ADMIN_ID, f"🎁 Юзернейм: @{username_user} \nTRIAL: {user_id}")
    else:
        await message.answer("⚠️ Ошибка активации. Попробуйте позже.")
    
    await state.clear()

@router.message(F.text == "🤝 Рефералы")
@router.message(F.text == "🤝 Рефералы")
async def referrals_handler(message: Message, bot: Bot):
    user_id = message.from_user.id
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
    
    user = await get_user(user_id)
    balance = user['balance'] if user and 'balance' in user.keys() else 0.0
    ref_count = await get_referral_count(user_id)
    
    if ref_count >= 11: percent = 40
    elif ref_count >= 6: percent = 25
    else: percent = 10

    text = (
        f"🤝 <b>Партнерская программа</b>\n\n"
        f"Приглашайте друзей и получайте процент от их покупок на свой баланс! Деньги можно вывести или купить за них подписку.\n\n"
        f"📊 <b>Ваша статистика:</b>\n"
        f"👥 Приглашено: <b>{ref_count}</b>\n"
        f"📈 Ваша ставка: <b>{percent}%</b>\n"
        f"💰 Баланс: <b>{balance}₽</b>\n\n"
        f"<i>(1-5 реф. = 10% | 6-10 реф. = 25% | 11+ реф. = 40%)</i>\n\n"
        f"🔗 <b>Ваша реферальная ссылка:</b>\n<code>{ref_link}</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Поделиться", url=f"https://t.me/share/url?url={ref_link}&text=Быстрый VPN! Попробуй.")],
        [InlineKeyboardButton(text="💳 Вывести средства", callback_data="withdraw_funds")],
        [InlineKeyboardButton(text="🛒 Купить за баланс", callback_data="buy_with_balance")]
    ])
    
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "withdraw_funds")
async def withdraw_request(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    balance = user['balance'] if user and 'balance' in user.keys() else 0.0
    
    if balance < 100:
        await callback.answer(f"❌ Минимальная сумма вывода - 100₽. Ваш баланс: {balance}₽", show_alert=True)
        return
        
    await state.set_state(WithdrawState.waiting_for_details)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="cancel_withdraw")]])
    await callback.message.edit_text(
        f"💸 <b>Вывод средств</b>\nДоступно: {balance}₽\n\n"
        "Отправьте сообщением ваши реквизиты (Например: <i>Сбербанк 1234567890123456 Иванов И.</i>):",
        reply_markup=kb, parse_mode="HTML"
    )

@router.callback_query(F.data == "cancel_withdraw")
async def cancel_withdraw(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.answer("Вывод отменен")

@router.message(WithdrawState.waiting_for_details)
async def process_withdraw_details(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    details = message.text
    user = await get_user(user_id)
    balance = user['balance']

    await state.clear()

    # --- FIX: Сначала создаём заявку, потом списываем баланс.
    # Если create_withdrawal упадёт — деньги не потеряются.
    try:
        req_id = await create_withdrawal(user_id, balance, details)
        await update_balance(user_id, -balance)
    except Exception as e:
        logger.error(f"Ошибка создания заявки на вывод для {user_id}: {e}")
        await message.answer("❌ Техническая ошибка при создании заявки. Попробуйте позже.")
        return

    await message.answer("✅ <b>Заявка создана!</b>\nОжидайте подтверждения.", parse_mode="HTML")

    # Отправляем админу
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Выплачено", callback_data=f"adm_wd:app:{req_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_wd:rej:{req_id}")]
    ])
    await bot.send_message(
        ADMIN_ID,
        f"💸 <b>Новая заявка на вывод #{req_id}</b>\n"
        f"Пользователь: @{message.from_user.username} ({user_id})\n"
        f"Сумма: <b>{balance}₽</b>\n"
        f"Реквизиты: <code>{details}</code>",
        reply_markup=kb, parse_mode="HTML"
    )

@router.callback_query(F.data.startswith("adm_wd:"))
async def admin_handle_withdraw(callback: CallbackQuery, bot: Bot):
    if callback.from_user.id != ADMIN_ID: return
    _, action, req_id = callback.data.split(":")
    req_id = int(req_id)
    
    req = await get_withdrawal(req_id)
    if not req or req['status'] != 'pending':
        return await callback.answer("Заявка уже обработана!")
        
    user_id = req['user_id']
    amount = req['amount']
    
    if action == "app":
        await update_withdrawal_status(req_id, 'approved')
        await callback.message.edit_text(callback.message.html_text + "\n\n✅ <b>ВЫПЛАЧЕНО</b>", parse_mode="HTML")
        try:
            await bot.send_message(user_id, f"✅ <b>Ваша заявка на вывод {amount}₽ одобрена!</b>\nСредства отправлены на ваши реквизиты.", parse_mode="HTML")
        except: pass
    elif action == "rej":
        await update_withdrawal_status(req_id, 'rejected')
        await update_balance(user_id, amount) # Возвращаем деньги
        await callback.message.edit_text(callback.message.html_text + "\n\n❌ <b>ОТКЛОНЕНО</b>", parse_mode="HTML")
        try:
            await bot.send_message(user_id, f"❌ <b>Ваша заявка на вывод {amount}₽ отклонена.</b>\nСредства возвращены на баланс.\n\nПожалуйста, напишите в тех. поддержку для уточнения: {SUPPORT_USERNAME}", parse_mode="HTML")
        except: pass

@router.callback_query(F.data == "buy_with_balance")
async def buy_with_balance_menu(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    balance = user['balance']
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📅 1 Месяц - {PRICE_1_MONTH_RUB}₽", callback_data="bal_pay:1")],
        [InlineKeyboardButton(text=f"🗓 3 Месяца - {PRICE_3_MONTHS_RUB}₽", callback_data="bal_pay:3")],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="cancel_payment_process")]
    ])
    await callback.message.edit_text(
        f"🛒 <b>Оплата с баланса</b>\nДоступно: {balance}₽\n\nВыберите тариф:",
        reply_markup=kb, parse_mode="HTML"
    )

@router.callback_query(F.data.startswith("bal_pay:"))
async def process_balance_pay(callback: CallbackQuery, bot: Bot):
    plan_id = callback.data.split(":")[1]
    user_id = callback.from_user.id
    user = await get_user(user_id)
    balance = user['balance']
    
    price = PRICE_1_MONTH_RUB if plan_id == '1' else PRICE_3_MONTHS_RUB
    days = 30 if plan_id == '1' else 90
    
    if balance < price:
        return await callback.answer("❌ Недостаточно средств на балансе!", show_alert=True)
        
    await update_balance(user_id, -price)
    await callback.message.edit_text("⏳ Активация подписки...")
    
    status_api, sub_id, new_expiry = await ensure_subscription_client(user_id, add_days=days)
    if status_api != 'ERROR':
        await activate_subscription_db(user_id, new_expiry, sub_id)
        sub_link = f"{Subscription_URL}/{sub_id}" # Исправлен путь ссылки
        await bot.send_message(ADMIN_ID, f"🛒 ОПЛАТА БАЛАНСОМ {price}₽\nUser: {user_id}")
        await callback.message.edit_text(
            f"🎉 <b>Успешно оплачено!</b>\nНачислено: {days} дней.\n🔗 Ваша ссылка:\n<code>{sub_link}</code>", 
            parse_mode="HTML"
        )
    else:
        await update_balance(user_id, price) # Возвращаем деньги
        await callback.message.edit_text("⚠️ Ошибка активации. Средства возвращены на баланс.")

@router.message(F.text.lower() == "❤️ купить подписку")
async def offer_subscription_handler(message: Message, bot: Bot):
    user_id = message.from_user.id
    if not await check_subscription(bot, user_id):
        await message.answer(f"⛔ Подпишитесь на канал: {CHANNEL_ID}")
        return
    
    text = (
        "🚀 <b>Tuppy VPN Premium — Максимальная свобода!</b>\n\n"
        "<b>Что входит в подписку:</b>\n"
        "⚡️ Высокая скорость: до 10 Гбит/с, никаких зависаний видео в 4K.\n"
        "🌍 Зарубежные локации: Доступ к Instagram, YouTube, Netflix и др.\n"
        "📱 Мультиплатформа: Работает на iPhone, Android, PC и Mac.\n"
        "🛡 Полная анонимность: Мы не ведем логи и скрываем ваш трафик.\n"
        "♾️ Безлимитный трафик: Качайте сколько угодно.\n\n"
        "💰 <b>Стоимость:</b>\n"
        "79.0₽ / 30 дней\n"
        "169.0₽ / 3 месяца (Выгодно!)\n\n"
        "🔄 Моментальная активация сразу после оплаты.\n"
        "👨‍💻 Приоритет в поддержке, быстрые ответы и помощь от создателя сервиса!\n\n"
        "<b>Выберите вариант подписки:</b>"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📅 1 Месяц - {PRICE_1_MONTH_RUB}₽", callback_data="select_plan:1")],
        [InlineKeyboardButton(text=f"🗓 3 Месяца - {PRICE_3_MONTHS_RUB}₽", callback_data="select_plan:3")],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="cancel_payment_process")]
    ])
    
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("select_plan:"))
async def process_plan_selection(callback: CallbackQuery):
    plan_id = callback.data.split(":")[1] 
    
    if plan_id == '1':
        price_rub = PRICE_1_MONTH_RUB
        price_usdt = PRICE_1_MONTH_USDT
        duration_text = "30 дней"
    else:
        price_rub = PRICE_3_MONTHS_RUB
        price_usdt = PRICE_3_MONTHS_USDT
        duration_text = "90 дней"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Банковская карта ({price_rub}₽)", callback_data=f"method_yookassa:{plan_id}")],
        [InlineKeyboardButton(text=f"💎 CryptoBot ({price_usdt} USDT)", callback_data=f"method_crypto:{plan_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="offer_subscription_entry")]
    ])
    
    await callback.message.edit_text(
        f"💳 <b>Оплата подписки на {duration_text}</b>\n\nВыберите удобный способ оплаты:", 
        reply_markup=kb, 
        parse_mode="HTML"
    )

@router.callback_query(F.data == "offer_subscription_entry")
async def back_to_offers(callback: CallbackQuery, bot: Bot):
    await callback.message.delete()
    text = (
        "🚀 <b>Tuppy VPN Premium — Максимальная свобода!</b>\n\n"
        "💰 <b>Стоимость:</b>\n79.0₽ / 30 дней\n169.0₽ / 3 месяца\n\n"
        "<b>Выберите вариант подписки:</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📅 1 Месяц - {PRICE_1_MONTH_RUB}₽", callback_data="select_plan:1")],
        [InlineKeyboardButton(text=f"🗓 3 Месяца - {PRICE_3_MONTHS_RUB}₽", callback_data="select_plan:3")],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="cancel_payment_process")]
    ])
    await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "cancel_payment_process")
async def cancel_payment(callback: CallbackQuery):
    await callback.message.delete()
    await callback.answer("Операция отменена")

@router.callback_query(F.data.startswith("method_yookassa"))
async def start_yookassa_payment(callback: CallbackQuery):
    plan_id = callback.data.split(":")[1]
    user_id = callback.from_user.id
    
    if plan_id == '1':
        amount = PRICE_1_MONTH_RUB
        desc = f"VPN 30 days: {user_id}"
    else:
        amount = PRICE_3_MONTHS_RUB
        desc = f"VPN 90 days: {user_id}"

    await callback.message.edit_text("⏳ Создаем счет ЮKassa...")
    try:
        def create_payment_sync():
            idempotence_key = str(uuid.uuid4())
            payment = Payment.create({
                "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
                "confirmation": {"type": "redirect", "return_url": "https://t.me/TuppyVpnAdmin_robot"},
                "capture": True, "description": desc, "metadata": {"user_id": user_id, "plan_id": plan_id}
            }, idempotence_key)
            return payment
        payment = await asyncio.to_thread(create_payment_sync)
        await create_payment_record(payment.id, user_id, amount, "RUB", "yookassa")
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"↗️ Оплатить {amount}₽", url=payment.confirmation.confirmation_url)],
            [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"check_yookassa:{payment.id}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data=f"select_plan:{plan_id}")]
        ])
        await callback.message.edit_text(f"💳 Ссылка на оплату сформирована.", reply_markup=kb)
    except Exception as e:
        print(e)
        await callback.message.edit_text("❌ Ошибка платежной системы.")

@router.callback_query(F.data.startswith("check_yookassa"))
async def check_yookassa_status(callback: CallbackQuery, bot: Bot):
    payment_id = callback.data.split(":")[1]
    await check_and_activate(callback, bot, "yookassa", payment_id)

@router.callback_query(F.data.startswith("method_crypto"))
async def start_crypto_payment(callback: CallbackQuery):
    plan_id = callback.data.split(":")[1]
    user_id = callback.from_user.id
    
    if plan_id == '1':
        amount = PRICE_1_MONTH_USDT
    else:
        amount = PRICE_3_MONTHS_USDT
        
    await callback.message.edit_text("⏳ Создаем счет CryptoBot...")
    try:
        logger.info(f"[CryptoBot] Creating invoice: user={user_id} amount={amount} USDT")
        invoice = await cryptopay.create_invoice(
            asset='USDT',
            amount=amount
        )
        logger.info(f"[CryptoBot] Invoice created: id={invoice.invoice_id} url={invoice.bot_invoice_url}")
        await create_payment_record(invoice.invoice_id, user_id, amount, "USDT", "crypto")

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"↗️ Оплатить {amount} USDT", url=invoice.bot_invoice_url)],
            [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"check_crypto:{invoice.invoice_id}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data=f"select_plan:{plan_id}")]
        ])
        await callback.message.edit_text(f"💎 Оплата {amount} USDT", reply_markup=kb)
    except Exception as e:
        logger.exception(f"CryptoBot create_invoice error for user {user_id}: {e}")
        await callback.message.edit_text(
            f"❌ Ошибка CryptoBot.\n"
            f"<i>Попробуйте позже или напишите в поддержку: {SUPPORT_LINK}</i>",
            parse_mode="HTML"
        )

@router.callback_query(F.data.startswith("check_crypto"))
async def check_crypto_status(callback: CallbackQuery, bot: Bot):
    invoice_id = int(callback.data.split(":")[1])
    await check_and_activate(callback, bot, "crypto", invoice_id)

async def check_and_activate(callback: CallbackQuery, bot: Bot, provider: str, payment_id):
    user_id = callback.from_user.id
    payment_record = await get_payment_record(payment_id)
    if not payment_record:
        await callback.answer("❌ Платеж не найден.")
        return
    if payment_record[5] == 'completed':
        await callback.message.edit_text("✅ Уже оплачено!")
        return
    
    paid_amount = payment_record[2]
    days_to_add = 30 
    
    if provider == "yookassa":
        if paid_amount >= PRICE_3_MONTHS_RUB - 10: 
            days_to_add = 90
    elif provider == "crypto":
        if paid_amount >= PRICE_3_MONTHS_USDT - 0.1:
            days_to_add = 90

    is_paid = False
    try:
        if provider == "yookassa":
            payment = await asyncio.to_thread(lambda: Payment.find_one(payment_id))
            if payment.status == "succeeded":
                is_paid = True
        elif provider == "crypto":
            # aiocryptopay 0.4.x: get_invoices принимает одиночный ID, не список
            invoice = await cryptopay.get_invoices(invoice_ids=payment_id)
            if invoice and invoice.status == 'paid':
                is_paid = True
    except Exception as e:
        logger.exception(f"Payment check error [{provider}] payment_id={payment_id}: {e}")
        await callback.answer("❌ Ошибка проверки платежа. Попробуйте позже.", show_alert=True)
        return
    
    if is_paid:
        await mark_payment_completed(payment_id)
        status_api, sub_id, new_expiry = await ensure_subscription_client(user_id, add_days=days_to_add)
        
        if status_api != 'ERROR':
            username_user = callback.from_user.username
            await activate_subscription_db(user_id, new_expiry, sub_id)
            
            # --- РЕФЕРАЛЬНАЯ СИСТЕМА: НАЧИСЛЕНИЕ ПРОЦЕНТОВ ---
            buyer_data = await get_user(user_id)
            if buyer_data and buyer_data['referrer_id']:
                ref_id = buyer_data['referrer_id']
                ref_count = await get_referral_count(ref_id)
                
                # Считаем процент
                if ref_count >= 11: percent = 40
                elif ref_count >= 6: percent = 25
                else: percent = 10
                
                # Если крипта, считаем примерный рублевый эквивалент тарифа для баланса
                if provider == "crypto":
                    paid_rub = PRICE_3_MONTHS_RUB if days_to_add == 90 else PRICE_1_MONTH_RUB
                else:
                    paid_rub = paid_amount
                
                bonus = round(paid_rub * (percent / 100), 2)
                await update_balance(ref_id, bonus)
                
                try:
                    await bot.send_message(
                        ref_id, 
                        f"💰 <b>Реферальное начисление!</b>\n\nВаш реферал оплатил подписку. "
                        f"Вам начислено <b>{bonus}₽</b> (Ставка: {percent}%).\nТекущий баланс пополнен.", 
                        parse_mode="HTML"
                    )
                except: pass
            
            sub_link = f"{Subscription_URL}/sub/{sub_id}"
            await bot.send_message(ADMIN_ID, f"💰 ОПЛАТА ({provider}) {paid_amount} \nUser: {user_id} \nДней: {days_to_add} \n Username: {username_user}")
            await callback.message.edit_text(f"🎉 <b>Успешно!</b>\nНачислено: {days_to_add} дней.\n🔗 Ваша ссылка:\n<code>{sub_link}</code>\n\n📋 <b>Инструкция:</b>\n1. Скопируйте полученную ссылку.\n2. Установите Happ или аналог.\n3. Импортируйте ссылку. \n<b>Для автоподключения перейдите по ссылке и следуйте инструкции</b>", parse_mode="HTML")
        else:
            await callback.message.edit_text("⚠️ Оплата прошла, но ошибка выдачи ключа в Remnawave. Админ уведомлен.")
            await bot.send_message(ADMIN_ID, f"🆘 Ошибка ключа Remnawave: {user_id} (Оплатил {paid_amount})")
    else:
        await callback.answer("⏳ Платеж еще не прошел.")

@router.message(F.text.lower() == "🧑‍💻 поддержка")
async def support_handler(message: Message):
    text = (
        f"💬 <b>Служба поддержки</b>\n\n"
        f"Если у вас возникли вопросы, напишите нам:\n"
        f"👉 <a href='https://t.me/tuppyvpnsup_bot'>{SUPPORT_USERNAME}</a>"
    )
    await message.answer(text, parse_mode='HTML', disable_web_page_preview=True)

@router.message(F.text.lower() == "👤 информация")
@router.message(Command("info"))
async def user_info_handler(message: Message):
    user_id = message.from_user.id
    await message.answer("⏳ Запрашиваю данные из панели...") # Чтобы юзер видел отклик
    
    panel_user = await get_client_by_tgid(user_id)
    
    if not panel_user:
        await message.answer(
            f"ℹ️ <b>Подписка не найдена.</b>\n\n"
            f"Ваш ID: <code>{user_id}</code>\n"
            f"В панели управления нет записи с вашим Telegram ID или именем <code>user_{user_id}</code>.",
            parse_mode="HTML"
        )
        return

    # Если нашли, парсим данные (используем ключи из api-1(1).json)
    current_time = int(time.time())
    expire_at_str = panel_user.get('expireAt')
    expiry_ts = 0
    if expire_at_str:
        try:
            # Убираем миллисекунды, если они есть, для корректного парсинга
            date_part = expire_at_str.split('.')[0].replace('Z', '')
            expiry_ts = int(datetime.datetime.fromisoformat(date_part).timestamp())
        except Exception as e:
            print(f"❌ Ошибка парсинга даты: {e}")

    is_active = panel_user.get('status') == 'ACTIVE' and (expiry_ts > current_time or expiry_ts == 0)
    
    # Трафик: в Remnawave поле называется 'userTraffic' (объект или число)
    user_traffic_raw = panel_user.get('userTraffic')
    logger.info(f"[TRAFFIC] userTraffic raw value for {user_id}: {user_traffic_raw!r}")

    if isinstance(user_traffic_raw, dict):
        # Объект вида {"upload": 123, "download": 456} или {"bytes": 789}
        upload = user_traffic_raw.get('upload', 0) or 0
        download = user_traffic_raw.get('download', 0) or 0
        total_bytes = user_traffic_raw.get('bytes') or (upload + download)
        used_traffic = format_bytes(total_bytes)
    elif isinstance(user_traffic_raw, (int, float)) and user_traffic_raw:
        used_traffic = format_bytes(user_traffic_raw)
    else:
        used_traffic = "0.00 B"
    short_id = panel_user.get('shortUuid')
    sub_link = f"{Subscription_URL}/{short_id}"

    status_text = "✅ <b>Активна</b>" if is_active else "🔴 <b>Не активна</b>"
    expiry_date = datetime.datetime.fromtimestamp(expiry_ts).strftime('%d.%m.%Y') if expiry_ts > 0 else "Безлимит"

    text = (
        f"👤 <b>Личный кабинет</b>\n"
        f"ID: <code>{user_id}</code>\n"
        f"➖➖➖➖➖➖➖➖➖➖\n"
        f"📊 <b>Статус:</b> {status_text}\n"
        f"📅 <b>Истекает:</b> <code>{expiry_date}</code>\n"
        f"📉 <b>Трафик:</b> {used_traffic}\n"
        f"➖➖➖➖➖➖➖➖➖➖"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡️ Автоподключение", web_app=WebAppInfo(url=sub_link))],
        [InlineKeyboardButton(text="🔗 Ссылка", callback_data="info_get_link"),
         InlineKeyboardButton(text="📱 Устройства", callback_data="info_devices")],
        [InlineKeyboardButton(text="💳 Продлить", callback_data="extend_sub_callback")]
    ])

    await message.answer(text, reply_markup=kb, parse_mode="HTML")

async def get_client_sessions(user_telegram_id: int):
    headers = get_auth_headers()
    # Сначала получаем UUID пользователя
    client = await get_client_by_tgid(user_telegram_id)
    if not client:
        return []
    
    # Запрос на получение статистики/сессий конкретного пользователя
    url = f"{REMNAWAVE_Url}/api/users/{client['uuid']}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # В Remnawave сессии обычно лежат в 'activeSessions' или вычисляются по 'nodes'
                    return data.get('response', {}).get('nodes', [])
    except Exception as e:
        print(f"Error fetching sessions: {e}")
    return []

async def get_user_hwid_devices(user_uuid: str):
    """Получает список привязанных устройств пользователя по правильному пути API"""
    headers = get_auth_headers()
    url = f"{REMNAWAVE_Url}/api/hwid/devices/{user_uuid}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Возвращаем массив devices
                    return data.get('response', {}).get('devices', [])
                else:
                    print(f"❌ Ошибка API {resp.status} при запросе устройств")
    except Exception as e:
        print(f"❌ Исключение при получении устройств: {e}")
    return []

async def get_full_user_info(user_uuid: str):
    headers = get_auth_headers()
    # Запрашиваем данные конкретного пользователя
    url = f"{REMNAWAVE_Url}/api/users/{user_uuid}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Возвращаем чистое тело ответа
                    return data.get('response', {})
    except Exception as e:
        print(f"❌ Ошибка get_full_user_info: {e}")
    return None

@router.callback_query(F.data == "info_devices")
async def info_devices_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    # 1. Получаем полные данные юзера (для лимита и UUID)
    client_full = await get_client_by_tgid(user_id)
    if not client_full:
        return await callback.answer("❌ Ошибка получения данных")

    user_uuid = client_full.get('uuid')
    limit = client_full.get('hwidDeviceLimit', 5)

    # 2. Получаем список устройств через новый эндпоинт
    devices = await get_user_hwid_devices(user_uuid)
    active_count = len(devices)
    
    device_list_text = ""
    for i in range(limit):
        if i < active_count:
            device = devices[i]
            hwid_val = device.get('hwid', 'Unknown')
            # Выводим модель, платформу или User-Agent, если они есть
            model = device.get('deviceModel') or device.get('platform') or device.get('userAgent') or "Устройство"
            
            # Сокращаем длинный HWID для красоты
            short_name = f"{hwid_val[:8]}...{hwid_val[-4:]}" if len(hwid_val) > 12 else hwid_val
            device_list_text += f"{i+1}. 🟢 <b>{model}</b> (<code>{short_name}</code>)\n"
        else:
            device_list_text += f"{i+1}. ⚪️ <i>Свободный слот</i>\n"

    text = (
        f"📱 <b>Ваши устройства</b>\n"
        f"➖➖➖➖➖➖➖➖➖➖\n"
        f"🔐 <b>Лимит HWID:</b> {active_count}/{limit}\n\n"
        f"<b>Статус слотов:</b>\n{device_list_text}\n"
        f"➖➖➖➖➖➖➖➖➖➖"
    )
    
    kb = []
    # Показываем кнопку сброса только если есть привязанные устройства
    if active_count > 0:
        kb.append([InlineKeyboardButton(text="🗑 Сбросить все устройства", callback_data="device_reset_all")])
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_info")])
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")

@router.callback_query(F.data == "device_reset_all")
async def device_reset_all_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    # 1. Получаем UUID пользователя
    client = await get_client_by_tgid(user_id)
    if not client or 'uuid' not in client:
        return await callback.answer("❌ Ошибка: пользователь не найден", show_alert=True)
    
    user_uuid = client['uuid']
    headers = get_auth_headers()
    
    # 2. Правильный путь для удаления всех устройств
    url = f"{REMNAWAVE_Url}/api/hwid/devices/delete-all"

    # В POST запросе нужно передать userUuid в теле (payload)
    payload = {
        "userUuid": user_uuid
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status in [200, 201, 204]:
                    await callback.answer("✅ Устройства успешно сброшены!", show_alert=True)
                    print(f"🧹 HWID сброшены для пользователя {user_id}")
                    
                    # Мгновенно обновляем меню, чтобы слоты стали "Свободными"
                    await info_devices_handler(callback)
                else:
                    error_data = await resp.text()
                    print(f"❌ Ошибка сброса: {resp.status} - {error_data}")
                    await callback.answer(f"❌ Не удалось сбросить (Код: {resp.status})", show_alert=True)
                    
    except Exception as e:
        print(f"❌ Исключение при сбросе устройств: {e}")
        await callback.answer("❌ Техническая ошибка при сбросе", show_alert=True)

@router.callback_query(F.data == "info_get_link")
async def info_get_link_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    panel_user = await get_user_info_direct(user_id)
    
    if panel_user and panel_user['sub_id']:
        sub_link = f"{Subscription_URL}/{panel_user['sub_id']}"
        text = (
            f"🔗 <b>Ваша ссылка подписки:</b>\n\n"
            f"<code>{sub_link}</code>\n\n"
            f"👆 <i>Нажмите на ссылку, чтобы скопировать, затем вставьте её в один из конфигураторов</i>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
             [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_info")]
        ])
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await callback.answer("❌ Подписка не найдена на сервере.", show_alert=True)

@router.callback_query(F.data == "extend_sub_callback")
async def extend_sub_handler(callback: CallbackQuery, bot: Bot):
    # Перенаправляем на выбор тарифа (существующая функция)
    await offer_subscription_handler(callback.message, bot)

@router.callback_query(F.data == "back_to_info")
async def back_to_info_handler(callback: CallbackQuery):
    # Удаляем текущее сообщение и отправляем новую "Информацию"
    # (делаем так, чтобы обновился WebAppInfo, так как edit_text с WebApp иногда глючит)
    await callback.message.delete()
    await user_info_handler(callback.message)

@router.callback_query(F.data == "migrate_remnawave")
async def migrate_user_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = await get_user(user_id)
    
    if not user or not user['active']:
        await callback.answer("У вас нет активной подписки для переноса.")
        return

    await callback.message.edit_text("⏳ <b>Перенос данных в Remnawave...</b>", parse_mode="HTML")
    
    current_time = int(time.time())
    expiry_time = user['expiry_time']
    
    if expiry_time <= current_time:
        await callback.message.edit_text("❌ Ваша подписка истекла.")
        return
        
    status, sub_id, new_expiry = await ensure_subscription_client(user_id, add_days=0) 
    
    if status != 'ERROR':
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET sub_id = ? WHERE id = ?", (sub_id, user_id))
            await db.commit()
            
        sub_link = f"{Subscription_URL}/{sub_id}"
        await callback.message.edit_text(
            f"✅ <b>Миграция успешна!</b>\n\n"
            f"Ваша новая ссылка:\n<code>{sub_link}</code>\n\n"
            f"Пожалуйста, обновите подписку в приложении.",
            parse_mode="HTML"
        )
    else:
        await callback.message.edit_text("❌ Ошибка миграции. Обратитесь в поддержку.")

@router.message(Command('status'))
async def subscription_status(message: Message, bot: Bot):
    user_id = message.from_user.id
    panel_user = await get_user_info_direct(user_id)
    
    if not panel_user:
        await message.answer("ℹ️ Данные не найдены. Если вы оплачивали подписку, обратитесь в поддержку.")
        return

    time_now = int(time.time())
    referral_count = await get_referral_count(user_id) # Рефералы останутся из новой БД, если будут
    
    # Проверка, истекла ли подписка физически
    if panel_user['expiry_time'] <= time_now:
         await message.answer(
             f"❌ <b>Ваша подписка не активна.</b>\n\n"
             f"👥 Приглашено друзей: <b>{referral_count}</b>\n"
             f"Чтобы пользоваться VPN, пожалуйста, продлите подписку.",
             parse_mode="HTML"
         )
         if panel_user['status'] == 'ACTIVE':
             await set_client_enable_status(user_id, False) 
         return

    is_subscribed_channel = await check_subscription(bot, user_id)

    if is_subscribed_channel:
        # Если юзер в канале, а в панели он выключен — включаем
        if panel_user['status'] != 'ACTIVE':
             await message.answer("🔄 Вижу, вы снова с нами! Включаю ваш VPN...")
             await set_client_enable_status(user_id, enable=True)
        
        dt = datetime.datetime.fromtimestamp(panel_user['expiry_time'])
        expiry_str = dt.strftime('%d.%m.%Y в %H:%M')
        sub_link = f"{Subscription_URL}/{panel_user['sub_id']}"
        
        await message.answer(
            f"✅ <b>Подписка активна</b>\n\n"
            f"📅 Истекает: <b>{expiry_str}</b>\n"
            f"👥 Рефералов: <b>{referral_count}</b>\n\n"
            f"🔗 Ваша ссылка:\n<code>{sub_link}</code>\n"
            f"📋 <b>Инструкция:</b>\n1. Скопируйте полученную ссылку.\n2. Установите и вставьте в V2RayTun(Сверху плюс, импортировать из буфера обмена).\n3. Подключитесь.",
            parse_mode="HTML"
        )
    else:
        await message.answer(
            f"⚠️ <b>Доступ приостановлен</b>\n\n"
            f"У вас есть оплаченная подписка, но вы не подписаны на наш канал {CHANNEL_ID}.\n"
            f"Подпишитесь, чтобы VPN снова заработал, и введите /status повторно."
        )
        if panel_user['status'] == 'ACTIVE':
             await set_client_enable_status(user_id, enable=False)

@router.callback_query(F.data == "offer_subscription_entry")
async def back_to_sel(cb: CallbackQuery, bot: Bot):
    await offer_subscription_handler(cb.message, bot)

@router.callback_query(F.data == "cancel_payment_process")
async def cancel_proc(cb: CallbackQuery):
    await cb.message.delete()

async def periodic_subscription_check(bot: Bot):
    while True:
        print("🔄 Запуск плановой проверки подписок на канал...")
        active_users = await get_all_active_users()
        
        for user in active_users:
            user_id = user['id']
            
            is_subscribed = await check_subscription(bot, user_id)
            
            if not is_subscribed:
                print(f"❌ Пользователь {user_id} отписался. Отключаем VPN.")
                await set_client_enable_status(user_id, enable=False)
                await deactivate_user_in_db(user_id)
                try:
                    await bot.send_message(
                        user_id,
                        "⚠️ <b>Ваша подписка приостановлена!</b>\n\n"
                        "Вы отписались от нашего канала. VPN был временно отключен.\n"
                        "Чтобы включить его снова, подпишитесь на канал и нажмите /status.",
                        parse_mode="HTML"
                    )
                except Exception: pass
            
            await asyncio.sleep(0.5) 

        print("✅ Проверка завершена. Следующая через 12 часов.")
        await asyncio.sleep(12 * 3600)