import os
import time
import asyncio
import logging
import sqlite3
from contextlib import suppress
from threading import Thread

from dotenv import load_dotenv
from flask import Flask
import requests

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, RPCError


# =========================================================
# CONFIGURATION
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()

try:
    API_ID = int(os.getenv("API_ID", "0"))
except ValueError:
    API_ID = 0

PORT = int(os.getenv("PORT", "10000"))
DB_FILE = os.getenv("DB_FILE", "speaker_manager.db")
MONITOR_INTERVAL = max(3, int(os.getenv("MONITOR_INTERVAL", "5")))

ADMIN_IDS = {7871224176, 8454401183}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing in environment variables")

if not API_ID:
    raise RuntimeError("API_ID missing in environment variables")

if not API_HASH:
    raise RuntimeError("API_HASH missing in environment variables")


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("AutoSpeaker")


# =========================================================
# DATABASE
# =========================================================

db = sqlite3.connect(
    DB_FILE,
    check_same_thread=False,
)

db.row_factory = sqlite3.Row


def init_db():
    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            session_string TEXT,
            connected INTEGER DEFAULT 0,
            blocked INTEGER DEFAULT 0,
            coins REAL DEFAULT 0.0,
            total_spent REAL DEFAULT 0.0,
            bot_active INTEGER DEFAULT 1,
            created_at INTEGER
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            channel_type TEXT NOT NULL,
            channel_username TEXT NOT NULL,
            channel_id INTEGER,
            title TEXT,
            active INTEGER DEFAULT 1,
            auto_allow INTEGER DEFAULT 1,
            created_at INTEGER,
            UNIQUE(owner_id, channel_type)
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS global_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            session_string TEXT,
            added_by INTEGER,
            active INTEGER DEFAULT 1,
            created_at INTEGER
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS user_global_usage (
            user_id INTEGER PRIMARY KEY,
            session_id INTEGER,
            connected_at INTEGER
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount_usd REAL,
            coins REAL,
            screenshot_file_id TEXT,
            status TEXT DEFAULT 'pending',
            created_at INTEGER
        )
    """)

    # Default Settings
    defaults = {
        "binance_pay_id": "839892941",
        "coin_rate": "65000",
        "min_deposit": "1",
        "cost_per_hour": "1000"  # প্রতি ঘণ্টা মনিটরিংয়ের জন্য কত কয়েন কাটবে
    }

    for k, v in defaults.items():
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    db.commit()


init_db()


# =========================================================
# BOT INITIALIZATION
# =========================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

dp.include_router(router)

clients = {}
client_locks = {}
monitor_locks = {}


# =========================================================
# STATES
# =========================================================

class StringSessionState(StatesGroup):
    waiting_session = State()


class TargetChannelState(StatesGroup):
    waiting_channel = State()


class LiveChannelState(StatesGroup):
    waiting_channel = State()


class AdminBroadcastState(StatesGroup):
    waiting_message = State()


class AdminSearchState(StatesGroup):
    waiting_user = State()


class AdminAddSessionState(StatesGroup):
    waiting_username = State()
    waiting_string = State()


class DepositState(StatesGroup):
    waiting_amount = State()
    waiting_screenshot = State()


class AdminSettingsState(StatesGroup):
    waiting_value = State()


# =========================================================
# LOCKS
# =========================================================

def get_lock(lock_map, user_id):
    if user_id not in lock_map:
        lock_map[user_id] = asyncio.Lock()

    return lock_map[user_id]


# =========================================================
# KEYBOARDS
# =========================================================

def main_keyboard(user_id):
    kb = [
        [
            KeyboardButton(text="🔐 Connect Telegram"),
            KeyboardButton(text="🎯 Target Channel"),
        ],
        [
            KeyboardButton(text="📺 Live Stream Channel"),
            KeyboardButton(text="📊 Live Status"),
        ],
        [
            KeyboardButton(text="🌐 Admin Session"),
            KeyboardButton(text="💰 Top-up"),
        ],
        [
            KeyboardButton(text="👤 Profile"),
            KeyboardButton(text="⚙️ Bot On/Off"),
        ],
        [
            KeyboardButton(text="ℹ️ Help"),
        ],
    ]
    if user_id in ADMIN_IDS:
        kb.append([KeyboardButton(text="👑 Admin Panel")])

    return ReplyKeyboardMarkup(
        keyboard=kb,
        resize_keyboard=True,
        is_persistent=True,
    )


def admin_reply_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Add Global Session"), KeyboardButton(text="📋 List Sessions")],
            [KeyboardButton(text="👥 Usage Stats"), KeyboardButton(text="🔍 Search User")],
            [KeyboardButton(text="💳 Deposit Requests"), KeyboardButton(text="⚙️ Settings")],
            [KeyboardButton(text="📢 Broadcast"), KeyboardButton(text="🔙 Main Menu")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_menu(user_id):
    inline_kb = [
        [InlineKeyboardButton(text="🔐 Connect Telegram", callback_data="connect")],
        [InlineKeyboardButton(text="🎯 Target Channel", callback_data="target")],
        [InlineKeyboardButton(text="📺 Live Stream Channel", callback_data="live")],
        [InlineKeyboardButton(text="📊 Live Status", callback_data="status")],
        [InlineKeyboardButton(text="🌐 Admin Session", callback_data="global_session_menu")],
        [InlineKeyboardButton(text="💰 Top-up", callback_data="top_up")],
        [InlineKeyboardButton(text="👤 Profile", callback_data="profile")],
        [InlineKeyboardButton(text="⚙️ Bot On/Off", callback_data="toggle_bot")],
        [InlineKeyboardButton(text="ℹ Help", callback_data="help")],
    ]
    if user_id in ADMIN_IDS:
        inline_kb.append([InlineKeyboardButton(text="👑 Admin Panel", callback_data="admin_panel")])

    return InlineKeyboardMarkup(inline_keyboard=inline_kb)


def admin_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Add Global Session", callback_data="admin_add_session")],
            [InlineKeyboardButton(text="📋 List Global Sessions", callback_data="admin_list_sessions")],
            [InlineKeyboardButton(text="👥 Users Using Sessions", callback_data="admin_usage_stats")],
            [InlineKeyboardButton(text="🔍 Search User", callback_data="admin_search_user")],
            [InlineKeyboardButton(text="💳 Deposit Requests", callback_data="admin_deposits")],
            [InlineKeyboardButton(text="⚙️ Deposit Settings", callback_data="admin_settings")],
            [InlineKeyboardButton(text="📢 Send Broadcast", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="🔙 Main Menu", callback_data="admin_back")],
        ]
    )


# =========================================================
# DATABASE HELPERS
# =========================================================

def get_setting(key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""


def is_connected_user(user_id):
    row = db.execute(
        "SELECT connected, blocked FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()

    if row and row["blocked"]:
        return False
    return bool(row and row["connected"])


def get_channel(owner_id, channel_type):
    return db.execute(
        """
        SELECT *
        FROM channels
        WHERE owner_id=?
          AND channel_type=?
          AND active=1
        """,
        (owner_id, channel_type),
    ).fetchone()


# =========================================================
# TELETHON CLIENT
# =========================================================

def get_client(user_id):
    if user_id not in clients:
        session_string = ""
        usage = db.execute("SELECT session_id FROM user_global_usage WHERE user_id=?", (user_id,)).fetchone()
        if usage:
            gs = db.execute("SELECT session_string FROM global_sessions WHERE id=?", (usage["session_id"],)).fetchone()
            if gs:
                session_string = gs["session_string"]

        if not session_string:
            row = db.execute(
                """
                SELECT session_string
                FROM users
                WHERE user_id=?
                  AND connected=1
                """,
                (user_id,),
            ).fetchone()
            session_string = row["session_string"] if row and row["session_string"] else ""

        clients[user_id] = TelegramClient(
            StringSession(session_string),
            API_ID,
            API_HASH,
            device_model="Telegram Auto Speaker",
            system_version="Python",
            app_version="1.0",
            connection_retries=5,
            timeout=30,
            auto_reconnect=True,
        )

    return clients[user_id]


async def ensure_client(user_id):
    client = get_client(user_id)

    async with get_lock(client_locks, user_id):
        if not client.is_connected():
            await client.connect()

        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telegram session invalid. Connect Telegram again."
            )

    return client


async def disconnect_client(user_id):
    client = clients.pop(user_id, None)

    if client:
        with suppress(Exception):
            if client.is_connected():
                await client.disconnect()


# =========================================================
# START
# =========================================================

@router.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.chat.id
    row = db.execute("SELECT blocked FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row and row["blocked"]:
        await message.answer("❌ আপনি এই বট থেকে ব্লকড হয়েছেন।")
        return

    db.execute(
        """
        INSERT INTO users (user_id, connected, blocked, bot_active, created_at)
        VALUES (?, 0, 0, 1, ?)
        ON CONFLICT(user_id) DO NOTHING
        """,
        (user_id, int(time.time()))
    )
    db.commit()

    await message.answer(
        "🎙 <b>TELEGRAM AUTO SPEAKER</b>\n\n"
        "Target Channel-এর সদস্যরা তোমার Live Stream Channel-এর "
        "লাইভ কলে যোগ দিলে বট তাদের শনাক্ত করার চেষ্টা করবে। "
        "প্রয়োজনীয় Telegram permission থাকলে muted সদস্যকে "
        "unmute করার অনুরোধ পাঠাবে।\n\n"
        "প্রথমে 🔐 Connect Telegram করো অথবা 🌐 Admin Session ব্যবহার করো।",
        parse_mode="HTML",
        reply_markup=main_keyboard(user_id),
    )

    await message.answer(
        "👇 Control Panel:",
        reply_markup=main_menu(user_id),
    )


# =========================================================
# PROFILE & BOT ON/OFF
# =========================================================

@router.message(F.text == "👤 Profile")
async def profile_text_handler(message: Message, state: FSMContext):
    await state.clear()
    await show_profile(message)


@router.callback_query(F.data == "profile")
async def profile_callback(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await show_profile(call.message)


async def show_profile(message: Message):
    user_id = message.chat.id
    user = db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not user:
        return

    target = get_channel(user_id, "target")
    live = get_channel(user_id, "live")
    bot_status = "🟢 Active" if user["bot_active"] else "🔴 Off"

    text = (
        f"👤 <b>User Profile</b>\n\n"
        f"• User ID: <code>{user['user_id']}</code>\n"
        f"• Target Channel: {target['channel_username'] if target else 'Not Set'}\n"
        f"• Live Channel: {live['channel_username'] if live else 'Not Set'}\n"
        f"• 💎 Current Balance: {user['coins']} Coins\n"
        f"• 💸 Total Spent: {user['total_spent']} Coins\n"
        f"• 🤖 Bot Status: {bot_status}\n"
    )

    await message.answer(text, parse_mode="HTML", reply_markup=main_keyboard(user_id))


@router.message(F.text == "⚙️ Bot On/Off")
async def toggle_bot_text_handler(message: Message, state: FSMContext):
    await state.clear()
    await toggle_bot_action(message)


@router.callback_query(F.data == "toggle_bot")
async def toggle_bot_callback(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await toggle_bot_action(call.message)


async def toggle_bot_action(message: Message):
    user_id = message.chat.id
    user = db.execute("SELECT bot_active FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not user:
        return

    new_status = 0 if user["bot_active"] else 1
    db.execute("UPDATE users SET bot_active=? WHERE user_id=?", (new_status, user_id))
    db.commit()

    status_text = "🟢 Bot turned ON" if new_status else "🔴 Bot turned OFF"
    await message.answer(f"{status_text} সফলভাবে!", reply_markup=main_keyboard(user_id))


# =========================================================
# TOP-UP / DEPOSIT SYSTEM
# =========================================================

@router.message(F.text == "💰 Top-up")
async def top_up_text_handler(message: Message, state: FSMContext):
    await state.clear()
    await start_deposit(message, state)


@router.callback_query(F.data == "top_up")
async def top_up_callback(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await start_deposit(call.message, state)


async def start_deposit(message: Message, state: FSMContext):
    user_id = message.chat.id
    user = db.execute("SELECT coins FROM users WHERE user_id=?", (user_id,)).fetchone()
    coins = user["coins"] if user else 0

    binance_id = get_setting("binance_pay_id")
    rate = get_setting("coin_rate")

    await state.set_state(DepositState.waiting_amount)
    await message.answer(
        f"💎 আপনার বর্তমান ব্যালেন্স: {coins} coins\n"
        f"💵 রেট: $1 = {rate} Coins\n\n"
        f"🟡 Binance Pay ID: <code>{binance_id}</code>\n\n"
        f"👉 ধাপ ১: কত ডলার ডিপোজিট করেছেন তা লিখুন (যেমন: 1, 5, 10):",
        parse_mode="HTML"
    )


@router.message(DepositState.waiting_amount)
async def process_deposit_amount(message: Message, state: FSMContext):
    if not message.text or message.text.startswith(("🔐", "🎯", "📺", "📊", "🌐", "ℹ️", "👑", "💰", "👤", "⚙️")):
        await state.clear()
        return

    try:
        amount = float(message.text.strip())
        min_dep = float(get_setting("min_deposit") or "1")
        if amount < min_dep:
            await message.answer(f"❌ সর্বনিম্ন ডিপোজিট ${min_dep} হতে হবে। আবার লিখুন:")
            return
    except ValueError:
        await message.answer("❌ সঠিক সংখ্যা লিখুন (যেমন: 1 বা 5):")
        return

    rate = float(get_setting("coin_rate") or "65000")
    coins = amount * rate

    await state.update_data(amount=amount, coins=coins)
    await state.set_state(DepositState.waiting_screenshot)

    await message.answer(
        f"💰 আপনার ডিপোজিট: ${amount} = {coins} Coins\n\n"
        f"👉 ধাপ ২: আপনার পেমেন্টের স্ক্রিনশট (Photo) টি পাঠান:"
    )


@router.message(DepositState.waiting_screenshot, F.photo)
async def process_deposit_screenshot(message: Message, state: FSMContext):
    data = await state.get_data()
    amount = data.get("amount")
    coins = data.get("coins")
    photo_file_id = message.photo[-1].file_id

    cursor = db.execute(
        """
        INSERT INTO deposits (user_id, amount_usd, coins, screenshot_file_id, status, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?)
        """,
        (message.chat.id, amount, coins, photo_file_id, int(time.time()))
    )
    deposit_id = cursor.lastrowid
    db.commit()
    await state.clear()

    await message.answer("✅ আপনার ডিপোজিট রিকোয়েস্ট অ্যাডমিনের কাছে পাঠানো হয়েছে। যাচাই করে ব্যালেন্স যুক্ত করা হবে।", reply_markup=main_keyboard(message.chat.id))

    # Notify Admins
    admin_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Accept", callback_data=f"dep_acc_{deposit_id}"),
                InlineKeyboardButton(text="❌ Reject", callback_data=f"dep_rej_{deposit_id}")
            ]
        ]
    )

    for admin_id in ADMIN_IDS:
        with suppress(Exception):
            await bot.send_photo(
                chat_id=admin_id,
                photo=photo_file_id,
                caption=f"💳 <b>New Deposit Request!</b>\n\n• User ID: <code>{message.chat.id}</code>\n• Amount: ${amount}\n• Coins: {coins}",
                parse_mode="HTML",
                reply_markup=admin_markup
            )


@router.message(DepositState.waiting_screenshot)
async def process_deposit_screenshot_invalid(message: Message):
    await message.answer("❌ দয়া করে একটি স্ক্রিনশট ছবি (Photo) পাঠান।")


# =========================================================
# HELP
# =========================================================

async def send_help(message: Message):
    user_id = message.chat.id
    await message.answer(
        "ℹ️ <b>কিভাবে ব্যবহার করবে</b>\n\n"
        "1️⃣ 🔐 Connect Telegram অথবা 🌐 Admin Session ব্যবহার করো।\n\n"
        "2️⃣ 🎯 Target Channel সেট করো।\n"
        "3️⃣ 📺 Live Stream Channel সেট করো।\n"
        "4️⃣ লাইভ চালু রাখলে বট মনিটর করবে এবং আপনার ব্যালেন্স থেকে কয়েন কাটা হবে। ব্যালেন্স শেষ হলে কাজ বন্ধ হয়ে যাবে।",
        parse_mode="HTML",
        reply_markup=main_keyboard(user_id),
    )


@router.message(F.text == "ℹ️ Help")
async def help_text_handler(message: Message):
    await send_help(message)


@router.callback_query(F.data == "help")
async def help_callback(call: CallbackQuery):
    await call.answer()
    await send_help(call.message)


# =========================================================
# CONNECT TELEGRAM
# =========================================================

@router.message(F.text == "🔐 Connect Telegram")
async def connect_text_handler(message: Message, state: FSMContext):
    await connect_telegram_menu(message, state)


@router.callback_query(F.data == "connect")
async def connect_callback(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await connect_telegram_menu(call.message, state)


async def connect_telegram_menu(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.chat.id

    if is_connected_user(user_id):
        try:
            client = await ensure_client(user_id)
            me = await client.get_me()

            await message.answer(
                "✅ <b>Telegram Already Connected</b>\n\n"
                f"Account ID: <code>{me.id}</code>\n\n"
                "এখন Target ও Live Channel সেট করো।",
                parse_mode="HTML",
                reply_markup=main_keyboard(user_id),
            )
            return

        except Exception:
            await disconnect_client(user_id)
            db.execute("UPDATE users SET connected=0 WHERE user_id=?", (user_id,))
            db.commit()

    await state.set_state(StringSessionState.waiting_session)
    await message.answer(
        "🔐 <b>Connect via String Session</b>\n\n"
        "Telethon String Session পাঠাও:",
        parse_mode="HTML",
    )


@router.message(StringSessionState.waiting_session)
async def process_string_session(message: Message, state: FSMContext):
    if not message.text or message.text.startswith(("🔐", "🎯", "📺", "📊", "🌐", "ℹ️", "👑", "💰", "👤", "⚙️")):
        await state.clear()
        return

    session_string = message.text.strip()
    user_id = message.chat.id

    if len(session_string) < 30:
        await message.answer("❌ String Session অসম্পূর্ণ মনে হচ্ছে। আবার চেষ্টা করো।")
        return

    db.execute("DELETE FROM user_global_usage WHERE user_id=?", (user_id,))
    db.commit()

    await disconnect_client(user_id)

    client = TelegramClient(
        StringSession(session_string),
        API_ID,
        API_HASH,
        device_model="Telegram Auto Speaker",
        system_version="Python",
        app_version="1.0",
        connection_retries=3,
        timeout=30,
        auto_reconnect=True,
    )

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            await message.answer("❌ Session অনুমোদিত নয় বা মেয়াদোত্তীর্ণ।")
            return

        me = await client.get_me()
        clients[user_id] = client

        db.execute(
            """
            INSERT INTO users (user_id, session_string, connected, created_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET session_string=excluded.session_string, connected=1
            """,
            (user_id, session_string, int(time.time())),
        )
        db.commit()
        await state.clear()

        with suppress(Exception):
            await message.delete()

        await message.answer(
            f"✅ <b>Telegram Connected Successfully!</b>\n\nAccount ID: <code>{me.id}</code>",
            parse_mode="HTML",
            reply_markup=main_keyboard(user_id),
        )
    except Exception:
        with suppress(Exception):
            await client.disconnect()
        await state.clear()
        await message.answer("❌ কানেক্ট করা যায়নি। Session যাচাই করুন।")


# =========================================================
# GLOBAL / ADMIN SESSION SELECTION FOR USERS
# =========================================================

@router.message(F.text == "🌐 Admin Session")
async def global_session_text_handler(message: Message, state: FSMContext):
    await state.clear()
    await show_global_sessions(message)


@router.callback_query(F.data == "global_session_menu")
async def global_session_callback(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await show_global_sessions(call.message)


async def show_global_sessions(message: Message):
    user_id = message.chat.id
    sessions = db.execute("SELECT * FROM global_sessions WHERE active=1").fetchall()

    if not sessions:
        await message.answer("❌ বর্তমানে কোনো অ্যাডমিন সেশন যুক্ত করা হয়নি।")
        return

    current = db.execute("SELECT session_id FROM user_global_usage WHERE user_id=?", (user_id,)).fetchone()
    current_sid = current["session_id"] if current else None

    buttons = []
    for s in sessions:
        mark = " ✅" if current_sid == s["id"] else ""
        buttons.append([InlineKeyboardButton(text=f"📱 @{s['username']}{mark}", callback_data=f"use_gs_{s['id']}")])

    buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data="main_menu_cb")])

    await message.answer(
        "🌐 <b>Available Admin Sessions</b>\n\nনিচের তালিকা থেকে একটি সেশন সিলেক্ট করুন:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.callback_query(F.data.startswith("use_gs_"))
async def select_global_session(call: CallbackQuery):
    await call.answer()
    user_id = call.message.chat.id
    session_id = int(call.data.split("_")[2])

    gs = db.execute("SELECT * FROM global_sessions WHERE id=?", (session_id,)).fetchone()
    if not gs:
        await call.message.answer("❌ সেশনটি পাওয়া যায়নি।")
        return

    db.execute(
        """
        INSERT INTO user_global_usage (user_id, session_id, connected_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET session_id=excluded.session_id
        """,
        (user_id, session_id, int(time.time()))
    )
    db.commit()

    await disconnect_client(user_id)
    await call.message.answer(
        f"✅ সফলভাবে <b>@{gs['username']}</b> সেশনটি সিলেক্ট করা হয়েছে!",
        parse_mode="HTML",
        reply_markup=main_keyboard(user_id)
    )


@router.callback_query(F.data == "main_menu_cb")
async def main_menu_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    user_id = call.message.chat.id
    await call.message.answer("👇 Control Panel:", reply_markup=main_menu(user_id))


# =========================================================
# TARGET & LIVE CHANNEL MENUS
# =========================================================

@router.message(F.text == "🎯 Target Channel")
async def target_text_handler(message: Message, state: FSMContext):
    await state.clear()
    await target_start(message, state)


@router.callback_query(F.data == "target")
async def target_callback(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await target_start(call.message, state)


async def target_start(message: Message, state: FSMContext):
    user_id = message.chat.id
    if not is_connected_user(user_id) and not db.execute("SELECT * FROM user_global_usage WHERE user_id=?", (user_id,)).fetchone():
        await message.answer("❌ আগে 🔐 Connect Telegram অথবা 🌐 Admin Session সিলেক্ট করো।")
        return

    old = get_channel(user_id, "target")
    if old:
        await message.answer(f"🎯 <b>Target Channel</b>\n\n🔗 {old['channel_username']}\n\nপরিবর্তন করতে নতুন @username পাঠাও।", parse_mode="HTML")
    else:
        await message.answer("🎯 <b>Target Channel</b>\n\nusername পাঠাও (যেমন: <code>@Channel</code>):", parse_mode="HTML")

    await state.set_state(TargetChannelState.waiting_channel)


@router.message(F.text == "📺 Live Stream Channel")
async def live_text_handler(message: Message, state: FSMContext):
    await state.clear()
    await live_start(message, state)


@router.callback_query(F.data == "live")
async def live_callback(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await live_start(call.message, state)


async def live_start(message: Message, state: FSMContext):
    user_id = message.chat.id
    if not is_connected_user(user_id) and not db.execute("SELECT * FROM user_global_usage WHERE user_id=?", (user_id,)).fetchone():
        await message.answer("❌ আগে 🔐 Connect Telegram অথবা 🌐 Admin Session সিলেক্ট করো।")
        return

    old = get_channel(user_id, "live")
    if old:
        await message.answer(f"📺 <b>Live Stream Channel</b>\n\n🔗 {old['channel_username']}\n\nপরিবর্তন করতে নতুন @username পাঠাও।", parse_mode="HTML")
    else:
        await message.answer("📺 <b>Live Stream Channel</b>\n\nusername পাঠাও:", parse_mode="HTML")

    await state.set_state(LiveChannelState.waiting_channel)


@router.message(TargetChannelState.waiting_channel)
async def save_target_channel(message: Message, state: FSMContext):
    await save_channel(message, state, "target")


@router.message(LiveChannelState.waiting_channel)
async def save_live_channel(message: Message, state: FSMContext):
    await save_channel(message, state, "live")


def normalize_username(value):
    value = (value or "").strip()
    for prefix in ("https://t.me/", "http://t.me/"):
        if value.startswith(prefix):
            value = value[len(prefix):].split("/", 1)[0]
            break
    if value and not value.startswith("@"):
        value = "@" + value
    return value


async def save_channel(message: Message, state: FSMContext, channel_type: str):
    if message.text and message.text.startswith(("🔐", "🎯", "📺", "📊", "🌐", "ℹ️", "👑", "💰", "👤", "⚙️")):
        await state.clear()
        return

    user_id = message.chat.id
    username = normalize_username(message.text)

    if not username or username == "@":
        await message.answer("❌ সঠিক username দাও।")
        return

    try:
        client = await ensure_client(user_id)
        entity = await client.get_entity(username)
        title = getattr(entity, "title", username)

        db.execute(
            """
            INSERT INTO channels (owner_id, channel_type, channel_username, channel_id, title, active, auto_allow, created_at)
            VALUES (?, ?, ?, ?, ?, 1, 1, ?)
            ON CONFLICT(owner_id, channel_type) DO UPDATE SET channel_username=excluded.channel_username, title=excluded.title, active=1
            """,
            (user_id, channel_type, username, entity.id, title, int(time.time()))
        )
        db.commit()
        await state.clear()
        await message.answer(f"✅ সফলভাবে সেভ হয়েছে!", reply_markup=main_keyboard(user_id))
    except Exception:
        await state.clear()
        await message.answer("❌ চ্যানেল যোগ করা যায়নি। একাউন্টের এক্সেস আছে কিনা যাচাই করুন।")


# =========================================================
# MONITOR & BILLING LOGIC
# =========================================================

async def get_active_call(client, channel_username):
    entity = await client.get_entity(channel_username)
    full = await client(functions.channels.GetFullChannelRequest(channel=entity))
    call = getattr(full.full_chat, "call", None)
    return call, entity


async def get_participants(client, call):
    input_call = types.InputGroupCall(id=call.id, access_hash=call.access_hash)
    result = await client(functions.phone.GetGroupCallRequest(call=input_call, limit=100))
    users = {user.id: user for user in getattr(result, "users", [])}
    participants = list(getattr(result, "participants", []))
    return participants, users


async def is_target_member(client, target_entity, user):
    try:
        input_user = await client.get_input_entity(user)
        result = await client(functions.channels.GetParticipantRequest(channel=target_entity, participant=input_user))
        member = result.participant
        if isinstance(member, (types.ChannelParticipantLeft, types.ChannelParticipantBanned)):
            return False
        return True
    except Exception:
        return False


async def allow_participant(client, call, user):
    input_call = types.InputGroupCall(id=call.id, access_hash=call.access_hash)
    input_user = await client.get_input_entity(user)
    return await client(functions.phone.EditGroupCallParticipantRequest(call=input_call, participant=input_user, muted=False, raise_hand=False))


async def monitor_user(user_id):
    user = db.execute("SELECT bot_active, coins FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not user or not user["bot_active"]:
        return

    # Check balance
    cost_per_hour = float(get_setting("cost_per_hour") or "1000")
    cost_per_interval = (cost_per_hour / 3600) * MONITOR_INTERVAL

    if user["coins"] <= 0:
        db.execute("UPDATE users SET bot_active=0 WHERE user_id=?", (user_id,))
        db.commit()
        with suppress(Exception):
            await bot.send_message(user_id, "⚠️ আপনার ব্যালেন্স শেষ হয়ে গেছে! বট কাজ করা বন্ধ করে দিয়েছে। দয়া করে 💰 Top-up করুন।")
        return

    lock = get_lock(monitor_locks, user_id)
    if lock.locked():
        return

    async with lock:
        target_row = get_channel(user_id, "target")
        live_row = get_channel(user_id, "live")
        if not target_row or not live_row:
            return

        has_session = is_connected_user(user_id) or db.execute("SELECT * FROM user_global_usage WHERE user_id=?", (user_id,)).fetchone()
        if not has_session:
            return

        try:
            client = await ensure_client(user_id)
            call, _ = await get_active_call(client, live_row["channel_username"])
            if not call:
                return

            # Deduct coins for active monitoring
            db.execute("UPDATE users SET coins = MAX(0, coins - ?), total_spent = total_spent + ? WHERE user_id=?", (cost_per_interval, cost_per_interval, user_id))
            db.commit()

            target_entity = await client.get_entity(target_row["channel_username"])
            participants, users = await get_participants(client, call)

            for participant in participants:
                peer = getattr(participant, "peer", None)
                if not isinstance(peer, types.PeerUser):
                    continue

                user_id_in_call = peer.user_id
                user_entity = users.get(user_id_in_call)
                if not user_entity:
                    with suppress(Exception):
                        user_entity = await client.get_entity(user_id_in_call)
                    if not user_entity:
                        continue

                if not await is_target_member(client, target_entity, user_entity):
                    continue

                if not getattr(participant, "muted", False):
                    continue

                with suppress(Exception):
                    await allow_participant(client, call, user_entity)
                await asyncio.sleep(0.15)
        except Exception as e:
            logger.debug(f"Monitor error for {user_id}: {e}")


async def auto_monitor():
    while True:
        try:
            rows = db.execute("SELECT DISTINCT owner_id FROM channels WHERE active=1").fetchall()
            for row in rows:
                await monitor_user(row["owner_id"])
                await asyncio.sleep(0.25)
        except Exception:
            pass
        await asyncio.sleep(MONITOR_INTERVAL)


# =========================================================
# LIVE STATUS
# =========================================================

@router.message(F.text == "📊 Live Status")
async def status_text_handler(message: Message, state: FSMContext):
    await state.clear()
    await live_status_message(message)


@router.callback_query(F.data == "status")
async def status_callback(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await live_status_message(call.message)


async def live_status_message(message: Message):
    user_id = message.chat.id
    target_row = get_channel(user_id, "target")
    live_row = get_channel(user_id, "live")

    if not target_row or not live_row:
        await message.answer("❌ Target বা Live Channel সেট করা হয়নি।")
        return

    try:
        client = await ensure_client(user_id)
        call, _ = await get_active_call(client, live_row["channel_username"])
        if not call:
            await message.answer("🔴 <b>LIVE OFFLINE</b>", parse_mode="HTML")
            return
        await message.answer("🟢 <b>LIVE IS RUNNING & MONITORING</b>", parse_mode="HTML")
    except Exception:
        await message.answer("❌ Status চেক করা যায়নি।")


# =========================================================
# ADMIN PANEL
# =========================================================

@router.message(F.text == "👑 Admin Panel")
async def admin_text_handler(message: Message, state: FSMContext):
    if message.chat.id not in ADMIN_IDS:
        return
    await state.clear()
    await show_admin_panel(message)


@router.callback_query(F.data == "admin_panel")
async def admin_callback(call: CallbackQuery, state: FSMContext):
    if call.message.chat.id not in ADMIN_IDS:
        return
    await state.clear()
    await call.answer()
    await show_admin_panel(call.message)


async def show_admin_panel(message: Message):
    await message.answer("👑 <b>Admin Control Panel</b>", parse_mode="HTML", reply_markup=admin_reply_keyboard())
    await message.answer("👇 Admin Actions:", reply_markup=admin_keyboard())


@router.message(F.text == "🔙 Main Menu")
@router.callback_query(F.data == "admin_back")
async def admin_back(event, state: FSMContext = None):
    if state:
        await state.clear()
    user_id = event.chat.id if isinstance(event, Message) else event.message.chat.id
    msg = event if isinstance(event, Message) else event.message
    if isinstance(event, CallbackQuery):
        await event.answer()
    await msg.answer("👇 Control Panel:", reply_markup=main_keyboard(user_id))
    await msg.answer("Main Menu:", reply_markup=main_menu(user_id))


# Admin Reply Handlers
@router.message(F.text == "➕ Add Global Session")
async def admin_add_session_text(message: Message, state: FSMContext):
    if message.chat.id not in ADMIN_IDS:
        return
    await state.set_state(AdminAddSessionState.waiting_username)
    await message.answer("📱 সেশনের ইউজারনেম দিন (যেমন: @acc):")


@router.message(F.text == "📋 List Sessions")
async def admin_list_sessions_text(message: Message):
    if message.chat.id not in ADMIN_IDS:
        return
    sessions = db.execute("SELECT * FROM global_sessions").fetchall()
    text = "📋 <b>Global Sessions:</b>\n\n" + "".join([f"ID: {s['id']} | @{s['username']}\n" for s in sessions]) if sessions else "❌ কোনো সেশন নেই।"
    await message.answer(text, parse_mode="HTML", reply_markup=admin_reply_keyboard())


@router.message(F.text == "👥 Usage Stats")
async def admin_usage_stats_text(message: Message):
    if message.chat.id not in ADMIN_IDS:
        return
    rows = db.execute("SELECT u.user_id, g.username FROM user_global_usage u JOIN global_sessions g ON u.session_id = g.id").fetchall()
    text = "👥 <b>Users using Global Sessions:</b>\n\n" + "".join([f"User: <code>{r['user_id']}</code> -> @{r['username']}\n" for r in rows]) if rows else "❌ কেউ ব্যবহার করছে না।"
    await message.answer(text, parse_mode="HTML", reply_markup=admin_reply_keyboard())


@router.message(F.text == "🔍 Search User")
async def admin_search_text(message: Message, state: FSMContext):
    if message.chat.id not in ADMIN_IDS:
        return
    await state.set_state(AdminSearchState.waiting_user)
    await message.answer("🔍 ইউজার আইডি দিন:")


@router.message(F.text == "💳 Deposit Requests")
async def admin_deposits_text(message: Message):
    if message.chat.id not in ADMIN_IDS:
        return
    deps = db.execute("SELECT * FROM deposits WHERE status='pending'").fetchall()
    if not deps:
        await message.answer("❌ কোনো পেন্ডিং ডিপোজিট নেই।", reply_markup=admin_reply_keyboard())
        return
    for d in deps:
        admin_markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Accept", callback_data=f"dep_acc_{d['id']}"),
                    InlineKeyboardButton(text="❌ Reject", callback_data=f"dep_rej_{d['id']}")]
            ]
        )
        with suppress(Exception):
            await bot.send_photo(
                chat_id=message.chat.id,
                photo=d["screenshot_file_id"],
                caption=f"💳 Deposit ID: {d['id']}\nUser ID: <code>{d['user_id']}</code>\nAmount: ${d['amount_usd']} ({d['coins']} Coins)",
                parse_mode="HTML",
                reply_markup=admin_markup
            )


@router.message(F.text == "⚙️ Settings")
async def admin_settings_text(message: Message):
    if message.chat.id not in ADMIN_IDS:
        return
    binance = get_setting("binance_pay_id")
    rate = get_setting("coin_rate")
    min_dep = get_setting("min_deposit")
    cost = get_setting("cost_per_hour")

    text = (
        f"⚙️ <b>Current Settings:</b>\n\n"
        f"1️⃣ Binance Pay ID: <code>{binance}</code>\n"
        f"2️⃣ Coin Rate ($1 = x Coins): {rate}\n"
        f"3️⃣ Min Deposit ($): {min_dep}\n"
        f"4️⃣ Cost per Hour (Coins): {cost}\n\n"
        "পরিবর্তন করতে নিচে ক্লিক করুন:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Change Binance ID", callback_data="set_binance")],
        [InlineKeyboardButton(text="✏️ Change Coin Rate", callback_data="set_rate")],
        [InlineKeyboardButton(text="✏️ Change Min Deposit", callback_data="set_mindep")],
        [InlineKeyboardButton(text="✏️ Change Cost/Hour", callback_data="set_cost")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="admin_panel")]
    ])
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(F.text == "📢 Broadcast")
async def admin_broadcast_text(message: Message, state: FSMContext):
    if message.chat.id not in ADMIN_IDS:
        return
    await state.set_state(AdminBroadcastState.waiting_message)
    await message.answer("📢 ব্রডকাস্ট মেসেজটি দিন:")


# Deposit Accept / Reject Callbacks
@router.callback_query(F.data.startswith("dep_acc_"))
async def accept_deposit(call: CallbackQuery):
    if call.message.chat.id not in ADMIN_IDS:
        return
    dep_id = int(call.data.split("_")[2])
    dep = db.execute("SELECT * FROM deposits WHERE id=?", (dep_id,)).fetchone()
    if not dep or dep["status"] != "pending":
        await call.answer("ডিপোজিট ইতিমধ্যে প্রসেস হয়েছে।", show_alert=True)
        return

    db.execute("UPDATE deposits SET status='accepted' WHERE id=?", (dep_id,))
    db.execute("UPDATE users SET coins = coins + ? WHERE user_id=?", (dep["coins"], dep["user_id"]))
    db.commit()

    await call.message.edit_caption(caption=call.message.caption + "\n\n✅ <b>ACCEPTED</b>", parse_mode="HTML")
    with suppress(Exception):
        await bot.send_message(dep["user_id"], f"🎉 আপনার ডিপোজিট গ্রহণ করা হয়েছে! আপনার অ্যাকাউন্টে {dep['coins']} Coins যুক্ত করা হয়েছে।")
    await call.answer("Accepted successfully!")


@router.callback_query(F.data.startswith("dep_rej_"))
async def reject_deposit(call: CallbackQuery):
    if call.message.chat.id not in ADMIN_IDS:
        return
    dep_id = int(call.data.split("_")[2])
    dep = db.execute("SELECT * FROM deposits WHERE id=?", (dep_id,)).fetchone()
    if not dep or dep["status"] != "pending":
        await call.answer("ডিপোজিট ইতিমধ্যে প্রসেস হয়েছে।", show_alert=True)
        return

    db.execute("UPDATE deposits SET status='rejected' WHERE id=?", (dep_id,))
    db.commit()

    await call.message.edit_caption(caption=call.message.caption + "\n\n❌ <b>REJECTED</b>", parse_mode="HTML")
    with suppress(Exception):
        await bot.send_message(dep["user_id"], "❌ আপনার ডিপোজিট রিকোয়েস্টটি বাতিল করা হয়েছে।")
    await call.answer("Rejected!")


# Settings Callbacks
@router.callback_query(F.data.in_({"set_binance", "set_rate", "set_mindep", "set_cost"}))
async def settings_edit_start(call: CallbackQuery, state: FSMContext):
    if call.message.chat.id not in ADMIN_IDS:
        return
    await call.answer()
    key_map = {
        "set_binance": "binance_pay_id",
        "set_rate": "coin_rate",
        "set_mindep": "min_deposit",
        "set_cost": "cost_per_hour"
    }
    await state.set_state(AdminSettingsState.waiting_value)
    await state.update_data(setting_key=key_map[call.data])
    await call.message.answer("✏️ নতুন মান (Value) দিন:")


@router.message(AdminSettingsState.waiting_value)
async def settings_edit_save(message: Message, state: FSMContext):
    if message.chat.id not in ADMIN_IDS:
        return
    data = await state.get_data()
    key = data.get("setting_key")
    val = message.text.strip()

    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, val))
    db.commit()
    await state.clear()
    await message.answer("✅ সেটিংস সফলভাবে আপডেট হয়েছে!", reply_markup=admin_reply_keyboard())


# Add Global Session Flow
@router.callback_query(F.data == "admin_add_session")
async def admin_add_session_start(call: CallbackQuery, state: FSMContext):
    if call.message.chat.id not in ADMIN_IDS:
        return
    await call.answer()
    await state.set_state(AdminAddSessionState.waiting_username)
    await call.message.answer("📱 ইউজারনেম দিন:")


@router.message(AdminAddSessionState.waiting_username)
async def admin_add_session_username(message: Message, state: FSMContext):
    if message.chat.id not in ADMIN_IDS:
        return
    await state.update_data(username=normalize_username(message.text))
    await state.set_state(AdminAddSessionState.waiting_string)
    await message.answer("🔑 String Session দিন:")


@router.message(AdminAddSessionState.waiting_string)
async def admin_add_session_save(message: Message, state: FSMContext):
    if message.chat.id not in ADMIN_IDS:
        return
    data = await state.get_data()
    try:
        db.execute(
            "INSERT INTO global_sessions (username, session_string, added_by, active, created_at) VALUES (?, ?, ?, 1, ?)",
            (data.get("username"), message.text.strip(), message.chat.id, int(time.time()))
        )
        db.commit()
        await state.clear()
        await message.answer("✅ সেশন সফলভাবে সেভ হয়েছে!", reply_markup=admin_reply_keyboard())
    except Exception as e:
        await state.clear()
        await message.answer(f"❌ ত্রুটি: {e}", reply_markup=admin_reply_keyboard())


# Admin Search & Broadcast
@router.message(AdminSearchState.waiting_user)
async def admin_search_process(message: Message, state: FSMContext):
    if message.chat.id not in ADMIN_IDS:
        return
    await state.clear()
    try:
        uid = int(message.text.strip())
    except ValueError:
        return
    user = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if not user:
        await message.answer("❌ ইউজার পাওয়া যায়নি।")
        return
    await message.answer(f"👤 User: {user['user_id']}\nCoins: {user['coins']}\nSpent: {user['total_spent']}\nBlocked: {user['blocked']}")


@router.message(AdminBroadcastState.waiting_message)
async def admin_broadcast_send(message: Message, state: FSMContext):
    if message.chat.id not in ADMIN_IDS:
        return
    await state.clear()
    users = db.execute("SELECT user_id FROM users WHERE blocked=0").fetchall()
    for u in users:
        with suppress(Exception):
            await message.send_copy(chat_id=u["user_id"])
            await asyncio.sleep(0.05)
    await message.answer("📢 Broadcast Complete!", reply_markup=admin_reply_keyboard())


# =========================================================
# FLASK WEB SERVER & KEEP-ALIVE
# =========================================================

web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "Telegram Auto Speaker & SMM Engine: ACTIVE 24/7"

def ping_self():
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "https://telegram-auto-speaker-1.onrender.com")
    while True:
        try:
            time.sleep(280)
            requests.get(render_url, timeout=10)
        except Exception:
            pass

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host='0.0.0.0', port=port, use_reloader=False)

def keep_alive():
    t_web = Thread(target=run_web)
    t_web.daemon = True
    t_web.start()
    t_ping = Thread(target=ping_self)
    t_ping.daemon = True
    t_ping.start()


# =========================================================
# MAIN
# =========================================================

async def main():
    logger.info("Telegram Auto Speaker starting...")
    keep_alive()
    monitor_task = asyncio.create_task(auto_monitor())

    try:
        await dp.start_polling(bot)
    finally:
        monitor_task.cancel()
        with suppress(asyncio.CancelledError):
            await monitor_task
        for user_id in list(clients.keys()):
            await disconnect_client(user_id)
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")
