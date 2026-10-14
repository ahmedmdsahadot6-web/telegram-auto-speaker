import os
import time
import asyncio
import logging
import sqlite3
from contextlib import suppress
from threading import Thread

from dotenv import load_dotenv
from aiohttp import web as aiohttp_web
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
    InputMediaPhoto,
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, RPCError

=========================================================
CONFIGURATION
=========================================================

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

ADMIN_IDS = [7871224176, 8454401183]

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing in environment variables")

if not API_ID:
    raise RuntimeError("API_ID missing in environment variables")

if not API_HASH:
    raise RuntimeError("API_HASH missing in environment variables")

=========================================================
LOGGING
=========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("AutoSpeaker")

=========================================================
DATABASE
=========================================================

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
        balance REAL DEFAULT 0.0,
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
    CREATE TABLE IF NOT EXISTS admin_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        session_string TEXT NOT NULL,
        added_at INTEGER
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS user_admin_sessions (
        user_id INTEGER PRIMARY KEY,
        session_id INTEGER,
        created_at INTEGER
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS deposits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount_usd REAL NOT NULL,
        coins REAL NOT NULL,
        screenshot_file_id TEXT,
        status TEXT DEFAULT 'pending',
        created_at INTEGER
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    # Default Settings
    default_settings = {
        "binance_pay_id": "839892941",
        "rate_per_usd": "65000",
        "min_deposit": "1",
        "cost_per_minute": "100"  # প্রতি মিনিটে কত কয়েন কাটবে
    }

    for k, v in default_settings.items():
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    db.commit()

init_db()

def get_setting(key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""

def update_setting(key, value):
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    db.commit()

=========================================================
BOT INITIALIZATION
=========================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

dp.include_router(router)

clients = {}
client_locks = {}
monitor_locks = {}

=========================================================
STATES
=========================================================

class StringSessionState(StatesGroup):
    waiting_session = State()

class TargetChannelState(StatesGroup):
    waiting_channel = State()

class LiveChannelState(StatesGroup):
    waiting_channel = State()

class AdminSessionState(StatesGroup):
    waiting_username = State()
    waiting_string = State()

class BroadcastState(StatesGroup):
    waiting_message = State()

class SearchUserState(StatesGroup):
    waiting_user_id = State()

class DepositState(StatesGroup):
    waiting_amount = State()
    waiting_screenshot = State()

class EditSettingsState(StatesGroup):
    waiting_value = State()

=========================================================
LOCKS
=========================================================

def get_lock(lock_map, user_id):
    if user_id not in lock_map:
        lock_map[user_id] = asyncio.Lock()
    return lock_map[user_id]

=========================================================
KEYBOARDS
=========================================================

def main_keyboard(user_id=None):
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
            KeyboardButton(text="💰 Top-up"),
            KeyboardButton(text="👤 Profile"),
        ],
        [
            KeyboardButton(text="🤖 Admin Session"),
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

def admin_keyboard():
    kb = [
        [
            KeyboardButton(text="📢 Broadcast Message"),
            KeyboardButton(text="🔍 Search User"),
        ],
        [
            KeyboardButton(text="📋 User Reports"),
            KeyboardButton(text="⚙️ Manage Settings"),
        ],
        [
            KeyboardButton(text="⚙️ Manage Admin Session"),
            KeyboardButton(text="🔙 Main Menu"),
        ],
    ]
    return ReplyKeyboardMarkup(
        keyboard=kb,
        resize_keyboard=True,
        is_persistent=True,
    )

def main_menu(user_id=None):
    inline_kb = [
        [InlineKeyboardButton(text="🔐 Connect Telegram", callback_data="connect")],
        [InlineKeyboardButton(text="🎯 Target Channel", callback_data="target")],
        [InlineKeyboardButton(text="📺 Live Stream Channel", callback_data="live")],
        [InlineKeyboardButton(text="📊 Live Status", callback_data="status")],
        [InlineKeyboardButton(text="💰 Top-up", callback_data="topup")],
        [InlineKeyboardButton(text="👤 Profile", callback_data="profile")],
        [InlineKeyboardButton(text="🤖 Admin Session", callback_data="admin_session_menu")],
        [InlineKeyboardButton(text="ℹ️ Help", callback_data="help")],
    ]
    if user_id in ADMIN_IDS:
        inline_kb.append([InlineKeyboardButton(text="👑 Admin Panel", callback_data="admin_panel_kb")])

    return InlineKeyboardMarkup(inline_keyboard=inline_kb)

=========================================================
DATABASE HELPERS
=========================================================

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

=========================================================
TELETHON CLIENT
=========================================================

def get_client(user_id):
    if user_id not in clients:
        us_row = db.execute(
            "SELECT session_string FROM admin_sessions JOIN user_admin_sessions ON admin_sessions.id = user_admin_sessions.session_id WHERE user_admin_sessions.user_id=?",
            (user_id,)
        ).fetchone()

        if us_row and us_row["session_string"]:
            session_string = us_row["session_string"]
        else:
            row = db.execute(
                """
                SELECT session_string
                FROM users
                WHERE user_id=?
                AND connected=1
                """,
                (user_id,),
            ).fetchone()

            session_string = (
                row["session_string"]
                if row and row["session_string"]
                else ""
            )

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

=========================================================
START
=========================================================

@router.message(CommandStart())
async def start_handler(message: Message):
    user_id = message.chat.id
    db.execute(
        "INSERT OR IGNORE INTO users (user_id, connected, blocked, balance, total_spent, bot_active, created_at) VALUES (?, 0, 0, 0.0, 0.0, 1, ?)",
        (user_id, int(time.time()))
    )
    db.commit()

    await message.answer(
        "🎙 <b>TELEGRAM AUTO SPEAKER</b>\n\n"
        "Target Channel-এর সদস্যরা তোমার Live Stream Channel-এর "
        "লাইভ কলে যোগ দিলে বট তাদের শনাক্ত করার চেষ্টা করবে। "
        "প্রয়োজনীয় Telegram permission থাকলে muted সদস্যকে "
        "unmute করার অনুরোধ পাঠাবে।\n\n"
        "প্রথমে 🔐 Connect Telegram করো অথবা 🤖 Admin Session ব্যবহার করো।",
        parse_mode="HTML",
        reply_markup=main_keyboard(user_id),
    )

    await message.answer(
        "👇 Control Panel:",
        reply_markup=main_menu(user_id),
    )

@router.message(F.text == "🔙 Main Menu")
async def main_menu_text_back(message: Message):
    user_id = message.chat.id
    await message.answer(
        "👇 Control Panel:",
        reply_markup=main_keyboard(user_id),
    )
    await message.answer(
        "Main Menu:",
        reply_markup=main_menu(user_id),
    )

@router.callback_query(F.data == "main_menu_back")
async def main_menu_back(call: CallbackQuery):
    await call.answer()
    user_id = call.message.chat.id
    await call.message.answer(
        "👇 Control Panel:",
        reply_markup=main_keyboard(user_id),
    )
    await call.message.edit_text(
        "👇 Control Panel:",
        reply_markup=main_menu(user_id),
    )

@router.callback_query(F.data == "admin_panel_kb")
async def admin_panel_kb_callback(call: CallbackQuery):
    if call.message.chat.id not in ADMIN_IDS:
        await call.answer("Access Denied!", show_alert=True)
        return
    await call.answer()
    await call.message.answer(
        "👑 Admin Keyboard Activated:",
        reply_markup=admin_keyboard(),
    )
    await show_admin_panel(call.message)

=========================================================
HELP
=========================================================

async def send_help(message: Message):
    user_id = message.chat.id
    await message.answer(
        "ℹ️ <b>কিভাবে ব্যবহার করবে</b>\n\n"
        "1️⃣ 🔐 Connect Telegram চাপো অথবা 🤖 Admin Session ব্যবহার করো।\n\n"
        "2️⃣ 🎯 Target Channel সেট করো।\n"
        "এই চ্যানেলের সদস্যদের যাচাই করা হবে।\n\n"
        "3️⃣ 📺 Live Stream Channel সেট করো।\n"
        "এখানে তোমার লাইভ চলবে।\n\n"
        "4️⃣ লাইভ চালু রাখো। বট পর্যায়ক্রমে অংশগ্রহণকারীদের পরীক্ষা করবে।\n\n"
        "5️⃣ 📊 Live Status থেকে অবস্থা দেখো।\n\n"
        "⚠️ সব অংশগ্রহণকারী Telegram API-তে দৃশ্যমান নাও হতে পারে। "
        "Unmute করার জন্য প্রয়োজনীয় call/admin permission লাগতে পারে।",
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

=========================================================
TOP-UP & DEPOSIT SYSTEM
=========================================================

@router.message(F.text == "💰 Top-up")
async def topup_text_handler(message: Message, state: FSMContext):
    await topup_handler(message, state)

@router.callback_query(F.data == "topup")
async def topup_callback(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await topup_handler(call.message, state)

async def topup_handler(message: Message, state: FSMContext):
    user_id = message.chat.id
    user = db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
    current_balance = user["balance"] if user else 0.0

    rate = float(get_setting("rate_per_usd"))
    binance_id = get_setting("binance_pay_id")
    min_dep = get_setting("min_deposit")

    current_coins = int(current_balance * rate)

    await state.set_state(DepositState.waiting_amount)
    await message.answer(
        f"💎 আপনার বর্তমান ব্যালেন্স: {current_coins} coins\n"
        f"💵 রেট: $1 = {rate} Coins\n"
        f"📌 সর্ব নিম্ন ডিপোজিট: ${min_dep}\n\n"
        f"🟡 Binance Pay ID: <code>{binance_id}</code>\n\n"
        "👉 ধাপ ১: কত ডলার ডিপোজিট করেছেন তা লিখুন (যেমন: 1, 5, 10):",
        parse_mode="HTML"
    )

@router.message(DepositState.waiting_amount)
async def process_deposit_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.strip())
        min_dep = float(get_setting("min_deposit"))
        if amount < min_dep:
            await message.answer(f"❌ সর্বনিম্ন ডিপোজিট ${min_dep}। সঠিক পরিমাণ লিখুন:")
            return
    except ValueError:
        await message.answer("❌ সঠিক সংখ্যা লিখুন (যেমন: 5 বা 10):")
        return

    await state.update_data(deposit_amount=amount)
    await state.set_state(DepositState.waiting_screenshot)
    await message.answer("👉 ধাপ ২: আপনার পেমেন্টের স্ক্রিনশট (Photo) টি পাঠান:")

@router.message(DepositState.waiting_screenshot, F.photo)
async def process_deposit_screenshot(message: Message, state: FSMContext):
    photo = message.photo[-1]
    file_id = photo.file_id
    data = await state.get_data()
    amount = data.get("deposit_amount")
    rate = float(get_setting("rate_per_usd"))
    coins = amount * rate
    user_id = message.chat.id

    await state.clear()

    cursor = db.execute(
        "INSERT INTO deposits (user_id, amount_usd, coins, screenshot_file_id, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
        (user_id, amount, coins, file_id, int(time.time()))
    )
    dep_id = cursor.lastrowid
    db.commit()

    await message.answer(
        f"💰 আপনার ডিপোজিট: ${amount} = {coins} Coins\n\n"
        "✅ আপনার পেমেন্ট রিকোয়েস্ট অ্যাডমিনের কাছে পাঠানো হয়েছে। অনুমোদন পেলে ব্যালেন্স যোগ হবে।",
        reply_markup=main_keyboard(user_id)
    )

    # Notify Admins
    for admin_id in ADMIN_IDS:
        try:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✅ Approve", callback_data=f"dep_approve_{dep_id}"),
                        InlineKeyboardButton(text="❌ Reject", callback_data=f"dep_reject_{dep_id}")
                    ]
                ]
            )
            await bot.send_photo(
                admin_id,
                photo=file_id,
                caption=f"🔔 <b>New Deposit Request!</b>\n\n"
                        f"👤 User ID: <code>{user_id}</code>\n"
                        f"💵 Amount: ${amount}\n"
                        f"💎 Coins: {coins}",
                parse_mode="HTML",
                reply_markup=kb
            )
        except Exception:
            pass

@router.callback_query(F.data.startswith("dep_approve_"))
async def approve_deposit(call: CallbackQuery):
    if call.message.chat.id not in ADMIN_IDS:
        return
    await call.answer()
    dep_id = int(call.data.split("_")[-1])
    dep = db.execute("SELECT * FROM deposits WHERE id=? AND status='pending'", (dep_id,)).fetchone()
    if not dep:
        await call.message.edit_caption(caption="❌ রিকোয়েস্টটি ইতিমধ্যে প্রসেস করা হয়েছে।")
        return

    db.execute("UPDATE deposits SET status='approved' WHERE id=?", (dep_id,))
    db.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (dep["coins"], dep["user_id"]))
    db.commit()

    await call.message.edit_caption(
        caption=f"{call.message.caption}\n\n✅ <b>Approved by Admin</b>",
        parse_mode="HTML"
    )

    with suppress(Exception):
        await bot.send_message(
            dep["user_id"],
            f"✅ আপনার ${dep['amount_usd']} ডিপোজিট সফল হয়েছে! অ্যাকাউন্টে {dep['coins']} Coins যোগ করা হয়েছে।"
        )

@router.callback_query(F.data.startswith("dep_reject_"))
async def reject_deposit(call: CallbackQuery):
    if call.message.chat.id not in ADMIN_IDS:
        return
    await call.answer()
    dep_id = int(call.data.split("_")[-1])
    dep = db.execute("SELECT * FROM deposits WHERE id=? AND status='pending'", (dep_id,)).fetchone()
    if not dep:
        await call.message.edit_caption(caption="❌ রিকোয়েস্টটি ইতিমধ্যে প্রসেস করা হয়েছে।")
        return

    db.execute("UPDATE deposits SET status='rejected' WHERE id=?", (dep_id,))
    db.commit()

    await call.message.edit_caption(
        caption=f"{call.message.caption}\n\n❌ <b>Rejected by Admin</b>",
        parse_mode="HTML"
    )

    with suppress(Exception):
        await bot.send_message(
            dep["user_id"],
            f"❌ আপনার ${dep['amount_usd']} ডিপোজিট রিকোয়েস্টটি বাতিল করা হয়েছে।"
        )

=========================================================
PROFILE & BOT ON/OFF SYSTEM
=========================================================

@router.message(F.text == "👤 Profile")
async def profile_text_handler(message: Message):
    await show_profile(message)

@router.callback_query(F.data == "profile")
async def profile_callback(call: CallbackQuery):
    await call.answer()
    await show_profile(call.message)

async def show_profile(message: Message):
    user_id = message.chat.id
    user = db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not user:
        return

    channels = db.execute("SELECT * FROM channels WHERE owner_id=?", (user_id,)).fetchall()
    chan_text = ""
    for c in channels:
        chan_text += f"• {c['channel_type'].upper()}: {c['channel_username']} ({c['title']})\n"

    rate = float(get_setting("rate_per_usd"))
    coins = user["balance"] * rate
    spent_coins = user["total_spent"] * rate
    bot_status = "🟢 ON" if user["bot_active"] else "🔴 OFF"
    toggle_text = "Turn OFF Bot" if user["bot_active"] else "Turn ON Bot"
    toggle_cb = "toggle_bot_off" if user["bot_active"] else "toggle_bot_on"

    text = (
        f"👤 <b>USER PROFILE</b>\n\n"
        f"🆔 User ID: <code>{user['user_id']}</code>\n"
        f"💎 Balance: {coins:.0f} Coins (${user['balance']:.2f})\n"
        f"💸 Spent: {spent_coins:.0f} Coins (${user['total_spent']:.2f})\n"
        f"🤖 Bot Status: {bot_status}\n\n"
        f"📺 <b>Your Channels:</b>\n{chan_text or 'কোনো চ্যানেল সেট করা নেই'}"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_text, callback_data=toggle_cb)],
            [InlineKeyboardButton(text="💰 Top-up", callback_data="topup")],
            [InlineKeyboardButton(text="🔙 Main Menu", callback_data="main_menu_back")]
        ]
    )
    await message.answer(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.in_({"toggle_bot_on", "toggle_bot_off"}))
async def toggle_bot_status(call: CallbackQuery):
    await call.answer()
    user_id = call.message.chat.id
    new_status = 1 if call.data == "toggle_bot_on" else 0
    db.execute("UPDATE users SET bot_active=? WHERE user_id=?", (new_status, user_id))
    db.commit()
    await show_profile(call.message)

=========================================================
CONNECT TELEGRAM
=========================================================

@router.message(F.text == "🔐 Connect Telegram")
async def connect_text_handler(message: Message, state: FSMContext):
    await connect_telegram_menu(message, state)

@router.callback_query(F.data == "connect")
async def connect_callback(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await connect_telegram_menu(call.message, state)

async def connect_telegram_menu(message: Message, state: FSMContext):
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
            logger.warning("Saved Telegram session invalid for user %s", user_id)
            await disconnect_client(user_id)
            db.execute("UPDATE users SET connected=0 WHERE user_id=?", (user_id,))
            db.commit()

    await state.set_state(StringSessionState.waiting_session)
    await message.answer(
        "🔐 <b>Connect via String Session</b>\n\n"
        "নিজের বা ব্যবহারের অনুমতি আছে এমন Telegram account-এর "
        "Telethon String Session পাঠাও。\n\n"
        "⚠️ এটি অ্যাকাউন্টের গোপন প্রবেশ-তথ্য। কাউকে শেয়ার করবে না।",
        parse_mode="HTML",
    )

@router.message(StringSessionState.waiting_session)
async def process_string_session(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ String Session টেক্সট হিসেবে পাঠাও।")
        return

    session_string = message.text.strip()
    user_id = message.chat.id

    if len(session_string) < 30:
        await message.answer("❌ String Session অসম্পূর্ণ মনে হচ্ছে। আবার চেষ্টা করো。")
        return

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
            await message.answer("❌ Session অনুমোদিত নয় বা মেয়াদোত্তীর্ণ। সঠিক session দিয়ে আবার চেষ্টা করো।")
            return

        me = await client.get_me()
        if not me:
            await client.disconnect()
            await message.answer("❌ Telegram account যাচাই করা যায়নি।")
            return

        clients[user_id] = client

        db.execute(
            """
            INSERT INTO users
                (user_id, session_string, connected, blocked, created_at)
            VALUES (?, ?, 1, 0, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                session_string=excluded.session_string,
                connected=1,
                created_at=excluded.created_at
            """,
            (user_id, session_string, int(time.time())),
        )
        db.execute("DELETE FROM user_admin_sessions WHERE user_id=?", (user_id,))
        db.commit()

        await state.clear()
        with suppress(Exception):
            await message.delete()

        await message.answer(
            "✅ <b>Telegram Connected Successfully!</b>\n\n"
            f"Account ID: <code>{me.id}</code>\n\n"
            "এখন Target ও Live Channel সেট করো।",
            parse_mode="HTML",
            reply_markup=main_keyboard(user_id),
        )
    except Exception:
        logger.exception("Telegram session connection failed for user %s", user_id)
        with suppress(Exception):
            await client.disconnect()
        await state.clear()
        await message.answer("❌ কানেক্ট করা যায়নি। Session, API_ID এবং API_HASH যাচাই করে আবার চেষ্টা করো।")

=========================================================
ADMIN SESSION USER MENU
=========================================================

@router.message(F.text == "🤖 Admin Session")
async def admin_session_text_handler(message: Message):
    await show_admin_session_menu(message)

@router.callback_query(F.data == "admin_session_menu")
async def admin_session_callback(call: CallbackQuery):
    await call.answer()
    await show_admin_session_menu(call.message)

async def show_admin_session_menu(message: Message):
    user_id = message.chat.id
    sessions = db.execute("SELECT * FROM admin_sessions").fetchall()

    if not sessions:
        await message.answer(
            "🤖 <b>Admin Session</b>\n\n"
            "বর্তমানে কোনো অ্যাডমিন সেশন যুক্ত করা হয়নি।",
            parse_mode="HTML",
            reply_markup=main_keyboard(user_id),
        )
        return

    keyboard = []
    for s in sessions:
        keyboard.append([InlineKeyboardButton(text=f"📱 @{s['username']}", callback_data=f"use_admin_sess_{s['id']}")])

    keyboard.append([InlineKeyboardButton(text="🔙 Back", callback_data="main_menu_back")])

    await message.answer(
        "🤖 <b>Admin Session List</b>\n\n"
        "নিচের তালিকা থেকে একটি অ্যাডমিন সেশন সিলেক্ট করুন:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
    )

@router.callback_query(F.data.startswith("use_admin_sess_"))
async def select_admin_session(call: CallbackQuery):
    await call.answer()
    user_id = call.message.chat.id
    session_id = int(call.data.split("_")[-1])

    session_row = db.execute("SELECT * FROM admin_sessions WHERE id=?", (session_id,)).fetchone()
    if not session_row:
        await call.message.answer("❌ সেশনটি পাওয়া যায়নি।")
        return

    db.execute(
        """
        INSERT INTO user_admin_sessions (user_id, session_id, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET session_id=excluded.session_id
        """,
        (user_id, session_id, int(time.time()))
    )
    db.commit()
    await disconnect_client(user_id)

    await call.message.edit_text(
        f"✅ <b>Admin Session Selected:</b> @{session_row['username']}\n\n"
        "এখন Target ও Live Channel সেট করুন।",
        parse_mode="HTML",
        reply_markup=main_menu(user_id),
    )

=========================================================
TARGET & LIVE CHANNEL MENUS
=========================================================

@router.message(F.text == "🎯 Target Channel")
async def target_text_handler(message: Message, state: FSMContext):
    await target_start(message, state)

@router.callback_query(F.data == "target")
async def target_callback(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await target_start(call.message, state)

async def target_start(message: Message, state: FSMContext):
    user_id = message.chat.id
    has_admin_sess = db.execute("SELECT * FROM user_admin_sessions WHERE user_id=?", (user_id,)).fetchone()
    if not is_connected_user(user_id) and not has_admin_sess:
        await message.answer("❌ আগে 🔐 Connect Telegram অথবা 🤖 Admin Session সিলেক্ট করো।")
        return

    old = get_channel(user_id, "target")
    if old:
        await message.answer(
            "🎯 <b>Target Channel</b>\n\n"
            f"📺 {old['title'] or 'Unknown'}\n"
            f"🔗 {old['channel_username']}\n\n"
            "পরিবর্তন করতে নতুন @username পাঠাও।",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "🎯 <b>Target Channel</b>\n\n"
            "যে চ্যানেলের সদস্যদের যাচাই করতে চাও তার public username পাঠাও。\n"
            "উদাহরণ: <code>@MyTargetChannel</code>",
            parse_mode="HTML",
        )
    await state.set_state(TargetChannelState.waiting_channel)

@router.message(F.text == "📺 Live Stream Channel")
async def live_text_handler(message: Message, state: FSMContext):
    await live_start(message, state)

@router.callback_query(F.data == "live")
async def live_callback(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await live_start(call.message, state)

async def live_start(message: Message, state: FSMContext):
    user_id = message.chat.id
    has_admin_sess = db.execute("SELECT * FROM user_admin_sessions WHERE user_id=?", (user_id,)).fetchone()
    if not is_connected_user(user_id) and not has_admin_sess:
        await message.answer("❌ আগে 🔐 Connect Telegram অথবা 🤖 Admin Session সিলেক্ট করো।")
        return

    old = get_channel(user_id, "live")
    if old:
        await message.answer(
            "📺 <b>Live Stream Channel</b>\n\n"
            f"📺 {old['title'] or 'Unknown'}\n"
            f"🔗 {old['channel_username']}\n\n"
            "পরিবর্তন করতে নতুন @username পাঠাও।",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "📺 <b>Live Stream Channel</b>\n\n"
            "যে public চ্যানেলে তুমি লাইভ করো তার username পাঠাও。\n"
            "উদাহরণ: <code>@MyLiveChannel</code>",
            parse_mode="HTML",
        )
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
    user_id = message.chat.id
    username = normalize_username(message.text)

    if not username or username == "@":
        await message.answer("❌ সঠিক public Channel username দাও, যেমন @MyChannel")
        return

    try:
        client = await ensure_client(user_id)
        entity = await client.get_entity(username)

        if not isinstance(entity, (types.Channel, types.Chat)):
            await message.answer("❌ এটি Telegram Channel/Group হিসেবে শনাক্ত হয়নি।")
            return

        title = getattr(entity, "title", username)

        db.execute(
            """
            INSERT INTO channels (
                owner_id,
                channel_type,
                channel_username,
                channel_id,
                title,
                active,
                auto_allow,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, 1, 1, ?)
            ON CONFLICT(owner_id, channel_type) DO UPDATE SET
                channel_username=excluded.channel_username,
                channel_id=excluded.channel_id,
                title=excluded.title,
                active=1,
                created_at=excluded.created_at
            """,
            (user_id, channel_type, username, entity.id, title, int(time.time())),
        )
        db.commit()
        await state.clear()

        if channel_type == "target":
            text = f"✅ <b>Target Channel Added</b>\n\n📺 {title}\n🔗 {username}\n\nএখন 📺 Live Stream Channel সেট করো।"
        else:
            text = f"✅ <b>Live Stream Channel Added</b>\n\n📺 {title}\n🔗 {username}\n\nলাইভ চালু হলে 📊 Live Status দিয়ে পরীক্ষা করো।"

        await message.answer(text, parse_mode="HTML", reply_markup=main_keyboard(user_id))
    except Exception:
        logger.exception("Channel setup failed")
        await state.clear()
        await message.answer("❌ Channel যোগ করা যায়নি।")

=========================================================
LIVE CALL FUNCTIONS & MONITOR (WITH BALANCE DEDUCTION)
=========================================================

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
    return await client(functions.phone.EditGroupCallParticipantRequest(call=input_call, participant=input_user, muted=False))

async def monitor_user(user_id):
    lock = get_lock(monitor_locks, user_id)
    if lock.locked():
        return

    async with lock:
        user_row = db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user_row or not user_row["bot_active"]:
            return

        target_row = get_channel(user_id, "target")
        live_row = get_channel(user_id, "live")

        if not target_row or not live_row:
            return

        has_admin_sess = db.execute("SELECT * FROM user_admin_sessions WHERE user_id=?", (user_id,)).fetchone()
        if not is_connected_user(user_id) and not has_admin_sess:
            return

        try:
            client = await ensure_client(user_id)
            call, _ = await get_active_call(client, live_row["channel_username"])

            if not call:
                return

            # লাইভ কল চালু আছে, তাই ব্যালেন্স কাটবে (સમয় অনুযায়ী)
            cost_per_min = float(get_setting("cost_per_minute"))
            rate = float(get_setting("rate_per_usd"))
            # MONITOR_INTERVAL সেকেন্ড হিসেবে খরচ হিসাব
            cost_usd = (cost_per_min / rate) * (MONITOR_INTERVAL / 60.0)

            if user_row["balance"] < cost_usd:
                db.execute("UPDATE users SET bot_active=0 WHERE user_id=?", (user_id,))
                db.commit()
                with suppress(Exception):
                    await bot.send_message(user_id, "⚠️ আপনার ব্যালেন্স শেষ হয়ে যাওয়ায় বট অটোমেটিক অফ করা হয়েছে। দয়া করে টপ-আপ করুন।")
                return

            db.execute("UPDATE users SET balance = balance - ?, total_spent = total_spent + ? WHERE user_id=?", (cost_usd, cost_usd, user_id))
            db.commit()

            target_entity = await client.get_entity(target_row["channel_username"])
            participants, users = await get_participants(client, call)

            for participant in participants:
                peer = getattr(participant, "peer", None)
                if not isinstance(peer, types.PeerUser):
                    continue

                user_id_in_call = peer.user_id
                user_entity = users.get(user_id_in_call)
                if user_entity is None:
                    with suppress(Exception):
                        user_entity = await client.get_entity(user_id_in_call)

                if user_entity is None:
                    continue

                if not await is_target_member(client, target_entity, user_entity):
                    continue

                if not getattr(participant, "muted", False):
                    continue

                with suppress(Exception):
                    await allow_participant(client, call, user_entity)
                await asyncio.sleep(0.15)

        except Exception:
            logger.exception("Monitor error for user=%s", user_id)

async def auto_monitor():
    logger.info("Auto Speaker Monitor started; interval=%s seconds", MONITOR_INTERVAL)
    while True:
        try:
            rows = db.execute("SELECT DISTINCT owner_id FROM channels WHERE active=1").fetchall()
            for row in rows:
                await monitor_user(row["owner_id"])
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Auto-monitor loop error")

        await asyncio.sleep(MONITOR_INTERVAL)

=========================================================
LIVE STATUS
=========================================================

@router.message(F.text == "📊 Live Status")
async def status_text_handler(message: Message):
    await live_status_message(message)

@router.callback_query(F.data == "status")
async def status_callback(call: CallbackQuery):
    await call.answer()
    await live_status_message(call.message)

async def live_status_message(message: Message):
    user_id = message.chat.id
    has_admin_sess = db.execute("SELECT * FROM user_admin_sessions WHERE user_id=?", (user_id,)).fetchone()
    if not is_connected_user(user_id) and not has_admin_sess:
        await message.answer("❌ আগে 🔐 Connect Telegram করো অথবা 🤖 Admin Session সিলেক্ট করো。")
        return

    target_row = get_channel(user_id, "target")
    live_row = get_channel(user_id, "live")

    if not target_row or not live_row:
        await message.answer("❌ Target বা Live Channel সেট করা হয়নি।")
        return

    try:
        client = await ensure_client(user_id)
        call, _ = await get_active_call(client, live_row["channel_username"])

        if not call:
            await message.answer("🔴 <b>LIVE OFFLINE</b>", parse_mode="HTML", reply_markup=main_keyboard(user_id))
            return

        await message.answer("🎙 <b>LIVE ONLINE & MONITORING ACTIVE</b>", parse_mode="HTML", reply_markup=main_keyboard(user_id))
    except Exception:
        await message.answer("❌ Status পাওয়া যায়নি।")

=========================================================
ADMIN PANEL & USER REPORTS
=========================================================

@router.message(F.text == "👑 Admin Panel")
async def admin_panel_text(message: Message):
    if message.chat.id not in ADMIN_IDS:
        return
    await show_admin_panel(message)

async def show_admin_panel(message: Message):
    total_users = db.execute("SELECT COUNT() as c FROM users").fetchone()["c"]
    active_sessions = db.execute("SELECT COUNT() as c FROM admin_sessions").fetchone()["c"]

    text = (
        "👑 <b>ADMIN PANEL</b>\n\n"
        f"👥 Total Users: {total_users}\n"
        f"🤖 Admin Sessions: {active_sessions}\n\n"
        "নিচের অপশনগুলো থেকে ম্যানেজ করুন:"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=admin_keyboard())

@router.message(F.text == "📋 User Reports")
async def admin_user_reports(message: Message):
    if message.chat.id not in ADMIN_IDS:
        return

    users = db.execute("SELECT * FROM users").fetchall()
    rate = float(get_setting("rate_per_usd"))

    text = "📋 <b>USER REPORTS</b>\n\n"
    for u in users:
        channels = db.execute("SELECT * FROM channels WHERE owner_id=?", (u["user_id"],)).fetchall()
        chan_list = ", ".join([c["channel_username"] for c in channels]) or "কোনো চ্যানেল নেই"
        balance_coins = u["balance"] * rate
        spent_coins = u["total_spent"] * rate

        text += (
            f"🆔 ID: <code>{u['user_id']}</code>\n"
            f"📺 Channels: {chan_list}\n"
            f"💎 Balance: {balance_coins:.0f} Coins\n"
            f"💸 Spent: {spent_coins:.0f} Coins\n"
            f"-----------------------------------\n"
        )

    # যদি লেখা বড় হয় তাহলে ভাগ করে পাঠাতে হবে
    if len(text) > 4000:
        for x in range(0, len(text), 4000):
            await message.answer(text[x:x+4000], parse_mode="HTML")
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=admin_keyboard())

@router.message(F.text == "⚙️ Manage Settings")
async def admin_manage_settings(message: Message):
    if message.chat.id not in ADMIN_IDS:
        return

    binance_id = get_setting("binance_pay_id")
    rate = get_setting("rate_per_usd")
    min_dep = get_setting("min_deposit")
    cost_min = get_setting("cost_per_minute")

    text = (
        "⚙️ <b>Manage Settings</b>\n\n"
        f"1️⃣ Binance Pay ID: <code>{binance_id}</code>\n"
        f"2️⃣ Rate ($1 = Coins): {rate}\n"
        f"3️⃣ Min Deposit ($): {min_dep}\n"
        f"4️⃣ Cost per Minute (Coins): {cost_min}\n\n"
        "পরিবর্তন করতে নিচের বাটনে ক্লিক করুন:"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Change Binance Pay ID", callback_data="set_binance_id")],
            [InlineKeyboardButton(text="✏️ Change Rate", callback_data="set_rate")],
            [InlineKeyboardButton(text="✏️ Change Min Deposit", callback_data="set_min_dep")],
            [InlineKeyboardButton(text="✏️ Change Cost/Minute", callback_data="set_cost_min")],
            [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel_back_to_menu")]
        ]
    )
    await message.answer(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("set_"))
async def start_edit_setting(call: CallbackQuery, state: FSMContext):
    if call.message.chat.id not in ADMIN_IDS:
        return
    await call.answer()
    key_map = {
        "set_binance_id": ("binance_pay_id", "নতুন Binance Pay ID দিন:"),
        "set_rate": ("rate_per_usd", "নতুন রেট দিন (যেমন: 65000):"),
        "set_min_dep": ("min_deposit", "সর্বনিম্ন ডিপোজিট ($) দিন:"),
        "set_cost_min": ("cost_per_minute", "প্রতি মিনিটের খরচ (Coins) দিন:")
    }
    setting_key, prompt_text = key_map[call.data]
    await state.update_data(setting_key=setting_key)
    await state.set_state(EditSettingsState.waiting_value)
    await call.message.answer(prompt_text)

@router.message(EditSettingsState.waiting_value)
async def save_new_setting(message: Message, state: FSMContext):
    if message.chat.id not in ADMIN_IDS:
        return
    data = await state.get_data()
    key = data.get("setting_key")
    value = message.text.strip()
    await state.clear()

    update_setting(key, value)
    await message.answer(f"✅ সফলভাবে আপডেট করা হয়েছে!", reply_markup=admin_keyboard())

@router.callback_query(F.data == "admin_panel_back_to_menu")
async def admin_panel_back_cb(call: CallbackQuery):
    if call.message.chat.id not in ADMIN_IDS:
        return
    await call.answer()
    await show_admin_panel(call.message)

# Admin Session Management
@router.message(F.text == "⚙️ Manage Admin Session")
async def admin_manage_session_text(message: Message):
    if message.chat.id not in ADMIN_IDS:
        return
    sessions = db.execute("SELECT * FROM admin_sessions").fetchall()
    text = "⚙️ <b>Manage Admin Sessions</b>\n\n"
    if sessions:
        for s in sessions:
            users_using = db.execute("SELECT COUNT(*) as c FROM user_admin_sessions WHERE session_id=?", (s['id'],)).fetchone()["c"]
            text += f"🆔 ID: {s['id']} | @{s['username']} | ব্যবহারকারী: {users_using} জন\n"
    else:
        text += "কোনো অ্যাডমিন সেশন যুক্ত করা হয়নি。\n"

    kb = [
        [InlineKeyboardButton(text="➕ Add New Session", callback_data="admin_add_session")],
        [InlineKeyboardButton(text="🗑 Delete Session", callback_data="admin_del_session_list")],
        [InlineKeyboardButton(text="👥 View Session Users", callback_data="admin_view_sess_users")],
    ]
    await message.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "admin_manage_session")
async def admin_manage_session_cb(call: CallbackQuery):
    if call.message.chat.id not in ADMIN_IDS:
        return
    await call.answer()
    sessions = db.execute("SELECT * FROM admin_sessions").fetchall()
    text = "⚙️ <b>Manage Admin Sessions</b>\n\n"
    if sessions:
        for s in sessions:
            users_using = db.execute("SELECT COUNT(*) as c FROM user_admin_sessions WHERE session_id=?", (s['id'],)).fetchone()["c"]
            text += f"🆔 ID: {s['id']} | @{s['username']} | ব্যবহারকারী: {users_using} জন\n"
    else:
        text += "কোনো অ্যাডমিন সেশন যুক্ত করা হয়নি。\n"

    kb = [
        [InlineKeyboardButton(text="➕ Add New Session", callback_data="admin_add_session")],
        [InlineKeyboardButton(text="🗑 Delete Session", callback_data="admin_del_session_list")],
        [InlineKeyboardButton(text="👥 View Session Users", callback_data="admin_view_sess_users")],
    ]
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "admin_add_session")
async def admin_add_session_start(call: CallbackQuery, state: FSMContext):
    if call.message.chat.id not in ADMIN_IDS:
        return
    await call.answer()
    await state.set_state(AdminSessionState.waiting_username)
    await call.message.answer("টেলিগ্রাম অ্যাকাউন্টের ইউজারনেম দিন (যেমন: @myaccount):")

@router.message(AdminSessionState.waiting_username)
async def admin_add_session_username(message: Message, state: FSMContext):
    if message.chat.id not in ADMIN_IDS:
        return
    username = normalize_username(message.text)
    await state.update_data(username=username)
    await state.set_state(AdminSessionState.waiting_string)
    await message.answer("এবার ঐ অ্যাকাউন্টের Telethon String Session দিন:")

@router.message(AdminSessionState.waiting_string)
async def admin_add_session_save(message: Message, state: FSMContext):
    if message.chat.id not in ADMIN_IDS:
        return
    data = await state.get_data()
    username = data.get("username")
    session_string = message.text.strip()

    db.execute("INSERT INTO admin_sessions (username, session_string, added_at) VALUES (?, ?, ?)", (username, session_string, int(time.time())))
    db.commit()
    await state.clear()
    await message.answer(f"✅ সফলভাবে অ্যাডমিন সেশন @{username} যুক্ত করা হয়েছে!", reply_markup=admin_keyboard())

@router.callback_query(F.data == "admin_del_session_list")
async def admin_del_session_list(call: CallbackQuery):
    if call.message.chat.id not in ADMIN_IDS:
        return
    await call.answer()
    sessions = db.execute("SELECT * FROM admin_sessions").fetchall()
    if not sessions:
        await call.message.edit_text("কোনো সেশন নেই।")
        return

    kb = []
    for s in sessions:
        kb.append([InlineKeyboardButton(text=f"🗑 Delete @{s['username']}", callback_data=f"del_sess_{s['id']}")])
    await call.message.edit_text("মুছে ফেলার জন্য সেশন সিলেক্ট করুন:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("del_sess_"))
async def admin_delete_session(call: CallbackQuery):
    if call.message.chat.id not in ADMIN_IDS:
        return
    await call.answer()
    sess_id = int(call.data.split("_")[-1])
    db.execute("DELETE FROM admin_sessions WHERE id=?", (sess_id,))
    db.execute("DELETE FROM user_admin_sessions WHERE session_id=?", (sess_id,))
    db.commit()
    await call.message.edit_text("✅ সেশন সফলভাবে মুছে ফেলা হয়েছে।", reply_markup=admin_keyboard())

@router.callback_query(F.data == "admin_view_sess_users")
async def admin_view_sess_users(call: CallbackQuery):
    if call.message.chat.id not in ADMIN_IDS:
        return
    await call.answer()
    rows = db.execute("SELECT uas.user_id, asess.username FROM user_admin_sessions uas JOIN admin_sessions asess ON uas.session_id = asess.id").fetchall()
    text = "👥 <b>অ্যাডমিন সেশন ব্যবহারকারীগণ:</b>\n\n"
    if rows:
        for r in rows:
            text += f"• ইউজার আইডি: <code>{r['user_id']}</code> | সেশন: @{r['username']}\n"
    else:
        text += "কেউ এখনো ব্যবহার করছে না。"
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=admin_keyboard())

# Broadcast Message
@router.message(F.text == "📢 Broadcast Message")
async def admin_broadcast_start_text(message: Message, state: FSMContext):
    if message.chat.id not in ADMIN_IDS:
        return
    await state.set_state(BroadcastState.waiting_message)
    await message.answer("সকল ব্যবহারকারীকে পাঠানোর জন্য মেসেজটি লিখুন:")

@router.message(BroadcastState.waiting_message)
async def admin_broadcast_send(message: Message, state: FSMContext):
    if message.chat.id not in ADMIN_IDS:
        return
    text = message.text
    await state.clear()

    users = db.execute("SELECT user_id FROM users").fetchall()
    success = 0
    failed = 0

    for u in users:
        try:
            await bot.send_message(u["user_id"], text, parse_mode="HTML")
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await message.answer(f"📢 <b>Broadcast Complete!</b>\n\nসফল: {success}\nব্যর্থ: {failed}", parse_mode="HTML", reply_markup=admin_keyboard())

# Search User & Block/Unblock
@router.message(F.text == "🔍 Search User")
async def admin_search_start_text(message: Message, state: FSMContext):
    if message.chat.id not in ADMIN_IDS:
        return
    await state.set_state(SearchUserState.waiting_user_id)
    await message.answer("খোঁজার জন্য ইউজারের Telegram User ID দিন:")

@router.message(SearchUserState.waiting_user_id)
async def admin_search_result(message: Message, state: FSMContext):
    if message.chat.id not in ADMIN_IDS:
        return
    await state.clear()
    try:
        target_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ সঠিক ইউজার আইডি দিন।")
        return

    user = db.execute("SELECT * FROM users WHERE user_id=?", (target_id,)).fetchone()
    if not user:
        await message.answer("❌ এই ইউজার আইডি ডাটাবেজে পাওয়া যায়নি।", reply_markup=admin_keyboard())
        return

    channels = db.execute("SELECT * FROM channels WHERE owner_id=?", (target_id,)).fetchall()
    chan_text = ""
    for c in channels:
        chan_text += f"- {c['channel_type'].upper()}: {c['channel_username']} ({c['title']})\n"

    status_str = "ব্নকড 🚫" if user["blocked"] else "অ্যাক্টিভ ✅"
    rate = float(get_setting("rate_per_usd"))

    text = (
        f"👤 <b>User Info</b>\n\n"
        f"🆔 ID: <code>{user['user_id']}</code>\n"
        f"📌 Status: {status_str}\n"
        f"💎 Balance: {user['balance']*rate:.0f} Coins\n"
        f"💸 Spent: {user['total_spent']*rate:.0f} Coins\n"
        f"📅 Joined: {time.strftime('%Y-%m-%d %H:%M', time.localtime(user['created_at']))}\n\n"
        f"📺 <b>Channels:</b>\n{chan_text or 'কোনো চ্যানেল নেই'}"
    )

    block_btn_text = "🔓 Unblock User" if user["blocked"] else "🚫 Block User"
    block_cb = f"admin_unblock_{target_id}" if user["blocked"] else f"admin_block_{target_id}"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=block_btn_text, callback_data=block_cb)],
        ]
    )
    await message.answer(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("admin_block_"))
async def admin_block_user(call: CallbackQuery):
    if call.message.chat.id not in ADMIN_IDS:
        return
    await call.answer()
    target_id = int(call.data.split("_")[-1])
    db.execute("UPDATE users SET blocked=1 WHERE user_id=?", (target_id,))
    db.commit()
    await call.message.edit_text(f"🚫 ইউজার <code>{target_id}</code> ব্লক করা হয়েছে।", parse_mode="HTML")

@router.callback_query(F.data.startswith("admin_unblock_"))
async def admin_unblock_user(call: CallbackQuery):
    if call.message.chat.id not in ADMIN_IDS:
        return
    await call.answer()
    target_id = int(call.data.split("_")[-1])
    db.execute("UPDATE users SET blocked=0 WHERE user_id=?", (target_id,))
    db.commit()
    await call.message.edit_text(f"🔓 ইউজার <code>{target_id}</code> আনব্লক করা হয়েছে।", parse_mode="HTML")

=========================================================
FLASK WEB SERVER & KEEP-ALIVE
=========================================================

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
            logger.info("📡 Keeping bot server active 24/7...")
        except Exception as e:
            logger.warning(f"Self Ping Warning: {e}")

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

=========================================================
MAIN
=========================================================

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
