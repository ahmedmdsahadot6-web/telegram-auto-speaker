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
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, InputMediaPhoto
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
ADMIN_IDS = [7871224176, 8454401183]

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing in environment variables")
if not API_ID:
    raise RuntimeError("API_ID missing in environment variables")
if not API_HASH:
    raise RuntimeError("API_HASH missing in environment variables")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("AutoSpeaker")

# =========================================================
# DATABASE
# =========================================================
db = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=30)
db.row_factory = sqlite3.Row
db_lock = asyncio.Lock()


def column_exists(table, column):
    return any(row["name"] == column for row in db.execute(f"PRAGMA table_info({table})"))


def init_db():
    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            session_string TEXT,
            connected INTEGER DEFAULT 0,
            blocked INTEGER DEFAULT 0,
            created_at INTEGER,
            balance INTEGER NOT NULL DEFAULT 0,
            total_spent INTEGER NOT NULL DEFAULT 0,
            bot_enabled INTEGER NOT NULL DEFAULT 0,
            billing_seconds INTEGER NOT NULL DEFAULT 0,
            billing_last_ts INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Upgrade an existing database without deleting any stored users.
    for col, definition in (
        ("balance", "INTEGER NOT NULL DEFAULT 0"),
        ("total_spent", "INTEGER NOT NULL DEFAULT 0"),
        ("bot_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ("billing_seconds", "INTEGER NOT NULL DEFAULT 0"),
        ("billing_last_ts", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if not column_exists("users", col):
            db.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")

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
        CREATE TABLE IF NOT EXISTS deposit_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount_usd REAL NOT NULL,
            coins INTEGER NOT NULL,
            photo_file_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL,
            reviewed_at INTEGER,
            reviewed_by INTEGER
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    defaults = {
        "binance_pay_id": "839892941",
        "coins_per_dollar": "65000",
        "min_deposit_usd": "1",
        "coins_per_minute": "1000",
    }
    for key, value in defaults.items():
        db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
    db.commit()


init_db()


def setting(key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""


def set_setting(key, value):
    db.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    db.commit()


def ensure_user(user_id):
    db.execute(
        "INSERT OR IGNORE INTO users(user_id, connected, blocked, created_at) VALUES(?,0,0,?)",
        (user_id, int(time.time())),
    )
    db.commit()


def user_row(user_id):
    ensure_user(user_id)
    return db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def is_admin(user_id):
    return user_id in ADMIN_IDS


def is_blocked(user_id):
    row = user_row(user_id)
    return bool(row["blocked"])


def is_connected_user(user_id):
    row = user_row(user_id)
    return bool(row["connected"]) and not bool(row["blocked"])


def get_channel(owner_id, channel_type):
    return db.execute(
        "SELECT * FROM channels WHERE owner_id=? AND channel_type=? AND active=1",
        (owner_id, channel_type),
    ).fetchone()


def has_admin_session(user_id):
    return db.execute(
        "SELECT 1 FROM user_admin_sessions WHERE user_id=?", (user_id,)
    ).fetchone() is not None


# =========================================================
# BOT / TELETHON CLIENTS
# =========================================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

clients = {}
client_locks = {}
monitor_locks = {}


def get_lock(lock_map, user_id):
    if user_id not in lock_map:
        lock_map[user_id] = asyncio.Lock()
    return lock_map[user_id]


def get_client(user_id):
    if user_id not in clients:
        # Admin sessions are shared credentials; only use sessions you own
        # and have explicit permission to operate. Prefer personal sessions.
        us_row = db.execute(
            """SELECT session_string FROM admin_sessions
               JOIN user_admin_sessions ON admin_sessions.id=user_admin_sessions.session_id
               WHERE user_admin_sessions.user_id=?""",
            (user_id,),
        ).fetchone()
        if us_row and us_row["session_string"]:
            session_string = us_row["session_string"]
        else:
            row = db.execute(
                "SELECT session_string FROM users WHERE user_id=? AND connected=1",
                (user_id,),
            ).fetchone()
            session_string = row["session_string"] if row and row["session_string"] else ""

        clients[user_id] = TelegramClient(
            StringSession(session_string), API_ID, API_HASH,
            device_model="Telegram Auto Speaker",
            system_version="Python", app_version="1.0",
            connection_retries=5, timeout=30, auto_reconnect=True,
        )
    return clients[user_id]

async def ensure_client(user_id):
    client = get_client(user_id)
    async with get_lock(client_locks, user_id):
        if not client.is_connected():
            await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session invalid. Connect Telegram again.")
    return client


async def disconnect_client(user_id):
    client = clients.pop(user_id, None)
    if client:
        with suppress(Exception):
            if client.is_connected():
                await client.disconnect()


# =========================================================
# STATES
# =========================================================
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
    waiting_photo = State()


class DepositSettingState(StatesGroup):
    waiting_pay_id = State()
    waiting_rate = State()
    waiting_minimum = State()
    waiting_charge = State()


# =========================================================
# KEYBOARDS
# =========================================================
def user_keyboard(user_id):
    rows = [
        [KeyboardButton(text="💰 Top-up"), KeyboardButton(text="👤 Profile")],
        [KeyboardButton(text="⏯️ Bot ON/OFF"), KeyboardButton(text="🔐 Connect Telegram")],
        [KeyboardButton(text="🎯 Target Channel"), KeyboardButton(text="📺 Live Stream Channel")],
        [KeyboardButton(text="📊 Live Status"), KeyboardButton(text="🤖 Admin Session")],
        [KeyboardButton(text="ℹ️ Help")],
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton(text="👑 Admin Panel")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def admin_reply_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💰 Deposit Requests"), KeyboardButton(text="📊 User Reports")],
            [KeyboardButton(text="⚙️ Deposit Settings"), KeyboardButton(text="🔍 Search User")],
            [KeyboardButton(text="📢 Broadcast Message"), KeyboardButton(text="🧩 Manage Admin Session")],
            [KeyboardButton(text="🔙 Main Menu")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_menu_inline(user_id):
    rows = [
        [InlineKeyboardButton(text="💰 Top-up", callback_data="topup")],
        [InlineKeyboardButton(text="👤 Profile", callback_data="profile")],
        [InlineKeyboardButton(text="⏯️ Bot ON/OFF", callback_data="bot_toggle")],
        [InlineKeyboardButton(text="🔐 Connect Telegram", callback_data="connect")],
        [InlineKeyboardButton(text="🎯 Target Channel", callback_data="target")],
        [InlineKeyboardButton(text="📺 Live Stream Channel", callback_data="live")],
        [InlineKeyboardButton(text="📊 Live Status", callback_data="status")],
        [InlineKeyboardButton(text="🤖 Admin Session", callback_data="admin_session_menu")],
        [InlineKeyboardButton(text="ℹ️ Help", callback_data="help")],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton(text="👑 Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_inline_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Deposit Requests", callback_data="admin_deposits")],
        [InlineKeyboardButton(text="📊 User Reports", callback_data="admin_reports")],

[InlineKeyboardButton(text="⚙️ Deposit Settings", callback_data="admin_settings")],
        [InlineKeyboardButton(text="🔍 Search User", callback_data="admin_search")],
        [InlineKeyboardButton(text="📢 Broadcast Message", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🧩 Manage Admin Session", callback_data="admin_manage_session")],
        [InlineKeyboardButton(text="🔙 Main Menu", callback_data="main_menu_back")],
    ])


def deposit_review_keyboard(request_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Accept", callback_data=f"dep_accept_{request_id}"),
        InlineKeyboardButton(text="❌ Reject", callback_data=f"dep_reject_{request_id}"),
    ]])


def admin_only(user_id):
    return is_admin(user_id)


# =========================================================
# START / MENUS
# =========================================================
@router.message(CommandStart())
async def start_handler(message: Message):
    user_id = message.chat.id
    ensure_user(user_id)
    if is_blocked(user_id):
        await message.answer("🚫 আপনার অ্যাকাউন্ট ব্লক করা হয়েছে।")
        return
    row = user_row(user_id)
    await message.answer(
        "🎙 <b>TELEGRAM AUTO SPEAKER</b>\n\n"
        "Target Channel-এর সদস্যরা Live Stream Channel-এর লাইভ কলে যোগ দিলে "
        "বট তাদের শনাক্ত করার চেষ্টা করবে। প্রয়োজনীয় Telegram permission থাকলে "
        "muted সদস্যকে unmute করার অনুরোধ পাঠাবে।\n\n"
        f"💎 Balance: {row['balance']} coins",
        parse_mode="HTML", reply_markup=user_keyboard(user_id),
    )
    await message.answer("👇 Control Panel:", reply_markup=main_menu_inline(user_id))


@router.message(F.text == "👑 Admin Panel")
async def admin_panel_text(message: Message):
    if not admin_only(message.chat.id):
        return
    await show_admin_panel(message)


@router.callback_query(F.data == "admin_panel")
async def admin_panel_cb(call: CallbackQuery):
    if not admin_only(call.from_user.id):
        await call.answer("Access denied", show_alert=True)
        return
    await call.answer()
    await show_admin_panel(call.message)


async def show_admin_panel(message: Message):
    total_users = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    pending = db.execute("SELECT COUNT(*) c FROM deposit_requests WHERE status='pending'").fetchone()["c"]
    text = (
        "👑 <b>ADMIN PANEL</b>\n\n"
        f"👥 Total Users: {total_users}\n"
        f"💰 Pending Deposits: {pending}\n\n"
        "নিচের কিবোর্ড থেকে অপশন নির্বাচন করুন।"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=admin_reply_keyboard())
    await message.answer("Admin Control Panel:", reply_markup=admin_inline_keyboard())


@router.message(F.text == "🔙 Main Menu")
async def main_menu_reply(message: Message, state: FSMContext):
    await state.clear()
    if is_blocked(message.chat.id):
        await message.answer("🚫 আপনার অ্যাকাউন্ট ব্লক করা হয়েছে।")
        return
    await message.answer("👇 User Main Menu", reply_markup=user_keyboard(message.chat.id))
    await message.answer("Control Panel:", reply_markup=main_menu_inline(message.chat.id))


@router.callback_query(F.data == "main_menu_back")
async def main_menu_back(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    uid = call.from_user.id
    await call.message.answer("👇 User Main Menu", reply_markup=user_keyboard(uid))
    await call.message.answer("Control Panel:", reply_markup=main_menu_inline(uid))


# =========================================================
# PROFILE / BOT TOGGLE / TOP-UP
# =========================================================
def profile_text(user_id):
    u = user_row(user_id)
    channels = db.execute(
        "SELECT channel_type, title, channel_username FROM channels WHERE owner_id=? AND active=1",

(user_id,),
    ).fetchall()
    channel_text = "\n".join(
        f"• {c['channel_type'].upper()}: {c['title'] or 'Unknown'} ({c['channel_username']})"
        for c in channels
    ) or "কোনো চ্যানেল সেট করা হয়নি।"
    return (
        "👤 <b>আপনার প্রোফাইল</b>\n\n"
        f"🆔 User ID: <code>{user_id}</code>\n"
        f"🎯/📺 Channels:\n{channel_text}\n\n"
        f"💎 Balance: <b>{u['balance']:,} coins</b>\n"
        f"🔥 Total Spent: <b>{u['total_spent']:,} coins</b>\n"
        f"⏯️ Bot: {'🟢 ON' if u['bot_enabled'] else '🔴 OFF'}"
    )


@router.message(F.text == "👤 Profile")
async def profile_message(message: Message):
    if is_blocked(message.chat.id):
        await message.answer("🚫 আপনার অ্যাকাউন্ট ব্লক করা হয়েছে।")
        return
    await message.answer(profile_text(message.chat.id), parse_mode="HTML", reply_markup=user_keyboard(message.chat.id))


@router.callback_query(F.data == "profile")
async def profile_callback(call: CallbackQuery):
    await call.answer()
    if is_blocked(call.from_user.id):
        await call.message.answer("🚫 আপনার অ্যাকাউন্ট ব্লক করা হয়েছে।")
        return
    await call.message.answer(profile_text(call.from_user.id), parse_mode="HTML", reply_markup=user_keyboard(call.from_user.id))


async def toggle_bot(user_id, message):
    if is_blocked(user_id):
        await message.answer("🚫 আপনার অ্যাকাউন্ট ব্লক করা হয়েছে।")
        return
    u = user_row(user_id)
    new_value = 0 if u["bot_enabled"] else 1
    if new_value:
        if not get_channel(user_id, "target") or not get_channel(user_id, "live"):
            await message.answer("❌ আগে Target Channel এবং Live Stream Channel সেট করুন।")
            return
        if u["balance"] <= 0:
            await message.answer("❌ আপনার Coin Balance নেই। প্রথমে 💰 Top-up করুন।")
            return
    db.execute("UPDATE users SET bot_enabled=?, billing_seconds=0, billing_last_ts=0 WHERE user_id=?", (new_value, user_id))
    db.commit()
    await message.answer(
        f"{'🟢 Bot ON করা হয়েছে' if new_value else '🔴 Bot OFF করা হয়েছে'}.\n"
        "শুধু লাইভ চালু থাকলেই চার্জ কাটা হবে।",
        reply_markup=user_keyboard(user_id),
    )


@router.message(F.text == "⏯️ Bot ON/OFF")
async def bot_toggle_message(message: Message):
    await toggle_bot(message.chat.id, message)


@router.callback_query(F.data == "bot_toggle")
async def bot_toggle_callback(call: CallbackQuery):
    await call.answer()
    await toggle_bot(call.from_user.id, call.message)


async def start_topup(message: Message, state: FSMContext):
    uid = message.chat.id
    if is_blocked(uid):
        await message.answer("🚫 আপনার অ্যাকাউন্ট ব্লক করা হয়েছে।")
        return
    u = user_row(uid)
    await state.set_state(DepositState.waiting_amount)
    await message.answer(
        "💰 <b>Top-up</b>\n\n"
        f"💎 আপনার বর্তমান ব্যালেন্স: {u['balance']:,} coins\n"
        f"💵 রেট: $1 = {int(setting('coins_per_dollar')):,} Coins\n\n"
        f"🟡 Binance Pay ID: <code>{setting('binance_pay_id')}</code>\n\n"
        f"👉 ধাপ ১: কত ডলার ডিপোজিট করেছেন তা লিখুন (সর্বনিম্ন ${setting('min_deposit_usd')}):",
        parse_mode="HTML",
    )


@router.message(F.text == "💰 Top-up")
async def topup_message(message: Message, state: FSMContext):
    await start_topup(message, state)


@router.callback_query(F.data == "topup")
async def topup_callback(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await start_topup(call.message, state)


@router.message(DepositState.waiting_amount)
async def deposit_amount(message: Message, state: FSMContext):
    try:
        amount = float((message.text or "").strip())
        if amount <= 0 or amount > 1000000:
            raise ValueError
    except (ValueError, TypeError):
        await message.answer("❌ সঠিক ডলারের পরিমাণ লিখুন, যেমন 1, 5 বা 10।")

return
    minimum = float(setting("min_deposit_usd"))
    if amount < minimum:
        await message.answer(f"❌ সর্বনিম্ন ডিপোজিট ${minimum:g}।")
        return
    coins = int(round(amount * int(setting("coins_per_dollar"))))
    await state.update_data(deposit_amount=amount, deposit_coins=coins)
    await state.set_state(DepositState.waiting_photo)
    await message.answer(
        f"💰 আপনার ডিপোজিট: ${amount:.2f} = {coins:,} Coins\n\n"
        "👉 ধাপ ২: আপনার পেমেন্টের স্ক্রিনশট (Photo) পাঠান:"
    )


@router.message(DepositState.waiting_photo, F.photo)
async def deposit_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    amount = float(data["deposit_amount"])
    coins = int(data["deposit_coins"])
    cur = db.execute(
        """INSERT INTO deposit_requests
           (user_id, amount_usd, coins, photo_file_id, status, created_at)
           VALUES(?,?,?,?, 'pending', ?)""",
        (message.from_user.id, amount, coins, message.photo[-1].file_id, int(time.time())),
    )
    request_id = cur.lastrowid
    db.commit()
    await state.clear()
    await message.answer(
        f"✅ ডিপোজিট রিকোয়েস্ট জমা হয়েছে।\nRequest ID: {request_id}\n"
        "অ্যাডমিন যাচাই করে Accept বা Reject করবেন।",
        reply_markup=user_keyboard(message.chat.id),
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_photo(
                admin_id, message.photo[-1].file_id,
                caption=(
                    f"💰 <b>Deposit Request #{request_id}</b>\n\n"
                    f"👤 User ID: <code>{message.from_user.id}</code>\n"
                    f"💵 Amount: ${amount:.2f}\n"
                    f"💎 Coins: {coins:,}\n"
                    f"🟡 Pay ID: {setting('binance_pay_id')}\n"
                    "Status: PENDING"
                ),
                parse_mode="HTML",
                reply_markup=deposit_review_keyboard(request_id),
            )
        except Exception:
            logger.exception("Could not notify admin %s", admin_id)


@router.message(DepositState.waiting_photo)
async def deposit_photo_required(message: Message):
    await message.answer("❌ অনুগ্রহ করে পেমেন্টের স্ক্রিনশট Photo হিসেবে পাঠান।")


@router.callback_query(F.data.startswith("dep_accept_"))
async def deposit_accept(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Access denied", show_alert=True)
        return
    await call.answer()
    request_id = int(call.data.rsplit("_", 1)[1])
    # Transaction + conditional status update prevents duplicate credit.
    try:
        db.execute("BEGIN IMMEDIATE")
        req = db.execute("SELECT * FROM deposit_requests WHERE id=?", (request_id,)).fetchone()
        if not req or req["status"] != "pending":
            db.rollback()
            await call.message.reply("এই রিকোয়েস্টটি ইতোমধ্যে প্রসেস করা হয়েছে বা পাওয়া যায়নি।")
            return
        db.execute("UPDATE deposit_requests SET status='accepted', reviewed_at=?, reviewed_by=? WHERE id=? AND status='pending'",
                   (int(time.time()), call.from_user.id, request_id))
        if db.execute("SELECT changes()").fetchone()[0] != 1:
            db.rollback()
            await call.message.reply("রিকোয়েস্টটি ইতোমধ্যে প্রসেস করা হয়েছে।")
            return
        ensure_user(req["user_id"])
        db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (req["coins"], req["user_id"]))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Deposit acceptance failed")
        await call.message.reply("❌ ডিপোজিট গ্রহণ করা যায়নি।")
        return
    with suppress(Exception):
        await bot.send_message(
            req["user_id"],
            f"✅ আপনার Deposit #{request_id} Accept হয়েছে।\n💎 যোগ হয়েছে: {req['coins']:,} Coins",
        )
    await call.message.edit_caption(
        caption=f"✅ Deposit #{request_id} ACCEPTED\nUser: {req['user_id']}\nCoins added: {req['coins']:,}",
        reply_markup=None,
    )

@router.callback_query(F.data.startswith("dep_reject_"))
async def deposit_reject(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Access denied", show_alert=True)
        return
    await call.answer()
    request_id = int(call.data.rsplit("_", 1)[1])
    cur = db.execute(
        """UPDATE deposit_requests SET status='rejected', reviewed_at=?, reviewed_by=?
           WHERE id=? AND status='pending'""",
        (int(time.time()), call.from_user.id, request_id),
    )
    db.commit()
    if cur.rowcount != 1:
        await call.message.reply("রিকোয়েস্টটি ইতোমধ্যে প্রসেস করা হয়েছে বা পাওয়া যায়নি।")
        return
    req = db.execute("SELECT user_id FROM deposit_requests WHERE id=?", (request_id,)).fetchone()
    with suppress(Exception):
        await bot.send_message(req["user_id"], f"❌ আপনার Deposit #{request_id} Reject করা হয়েছে। প্রয়োজনে অ্যাডমিনের সঙ্গে যোগাযোগ করুন।")
    await call.message.edit_caption(caption=f"❌ Deposit #{request_id} REJECTED", reply_markup=None)


# =========================================================
# HELP / TELEGRAM SESSION CONNECTION
# =========================================================
async def send_help(message: Message):
    await message.answer(
        "ℹ️ <b>কিভাবে ব্যবহার করবে</b>\n\n"
        "1️⃣ Connect Telegram অথবা অনুমোদিত Admin Session নির্বাচন করুন।\n"
        "2️⃣ Target Channel সেট করুন।\n"
        "3️⃣ Live Stream Channel সেট করুন।\n"
        "4️⃣ Bot ON করুন।\n"
        "5️⃣ লাইভ চললে নির্ধারিত Coin rate অনুযায়ী চার্জ হবে। লাইভ বন্ধ থাকলে চার্জ হবে না।\n"
        "6️⃣ Top-up করতে পেমেন্টের স্ক্রিনশট পাঠান।\n\n"
        "⚠️ সব অংশগ্রহণকারী Telegram API-তে দৃশ্যমান নাও হতে পারে। Unmute করার জন্য প্রয়োজনীয় call/admin permission লাগতে পারে।",
        parse_mode="HTML", reply_markup=user_keyboard(message.chat.id),
    )


@router.message(F.text == "ℹ️ Help")
async def help_text_handler(message: Message):
    await send_help(message)


@router.callback_query(F.data == "help")
async def help_callback(call: CallbackQuery):
    await call.answer()
    await send_help(call.message)


@router.message(F.text == "🔐 Connect Telegram")
async def connect_text_handler(message: Message, state: FSMContext):
    await connect_telegram_menu(message, state)


@router.callback_query(F.data == "connect")
async def connect_callback(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await connect_telegram_menu(call.message, state)


async def connect_telegram_menu(message: Message, state: FSMContext):
    user_id = message.chat.id
    if is_blocked(user_id):
        await message.answer("🚫 আপনার অ্যাকাউন্ট ব্লক করা হয়েছে।")
        return
    if is_connected_user(user_id):
        try:
            client = await ensure_client(user_id)
            me = await client.get_me()
            await message.answer(
                f"✅ Telegram Already Connected\nAccount ID: {me.id}",
                reply_markup=user_keyboard(user_id),
            )
            return
        except Exception:
            await disconnect_client(user_id)
            db.execute("UPDATE users SET connected=0 WHERE user_id=?", (user_id,))
            db.commit()
    await state.set_state(StringSessionState.waiting_session)
    await message.answer(
        "🔐 <b>Connect via String Session</b>\n\n"
        "শুধু নিজের বা ব্যবহারের অনুমতি আছে এমন Telegram account-এর session পাঠান।\n"
        "⚠️ String Session অ্যাকাউন্টের গোপন প্রবেশ-তথ্য। কারও সঙ্গে শেয়ার করবেন না।",
        parse_mode="HTML",
    )


@router.message(StringSessionState.waiting_session)
async def process_string_session(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ String Session টেক্সট হিসেবে পাঠান।")
        return
    session_string = message.text.strip()

user_id = message.chat.id
    if len(session_string) < 30:
        await message.answer("❌ Session অসম্পূর্ণ মনে হচ্ছে। আবার চেষ্টা করুন।")
        return
    await disconnect_client(user_id)
    client = TelegramClient(
        StringSession(session_string), API_ID, API_HASH,
        device_model="Telegram Auto Speaker", system_version="Python",
        app_version="1.0", connection_retries=3, timeout=30, auto_reconnect=True,
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            await message.answer("❌ Session অনুমোদিত নয় বা মেয়াদোত্তীর্ণ।")
            return
        me = await client.get_me()
        if not me:
            await client.disconnect()
            await message.answer("❌ Telegram account যাচাই করা যায়নি।")
            return
        clients[user_id] = client
        db.execute(
            """INSERT INTO users(user_id,session_string,connected,blocked,created_at)
               VALUES(?,?,1,0,?)
               ON CONFLICT(user_id) DO UPDATE SET session_string=excluded.session_string,
               connected=1, created_at=excluded.created_at""",
            (user_id, session_string, int(time.time())),
        )
        db.execute("DELETE FROM user_admin_sessions WHERE user_id=?", (user_id,))
        db.commit()
        await state.clear()
        with suppress(Exception):
            await message.delete()
        await message.answer(
            f"✅ Telegram Connected Successfully!\nAccount ID: {me.id}",
            reply_markup=user_keyboard(user_id),
        )
    except Exception:
        logger.exception("Telegram session connection failed for user %s", user_id)
        with suppress(Exception):
            await client.disconnect()
        await state.clear()
        await message.answer("❌ কানেক্ট করা যায়নি। Session, API_ID ও API_HASH যাচাই করুন।")


# =========================================================
# ADMIN SESSION SELECTOR (legacy feature)
# =========================================================
@router.message(F.text == "🤖 Admin Session")
async def admin_session_text_handler(message: Message):
    await show_admin_session_menu(message)


@router.callback_query(F.data == "admin_session_menu")
async def admin_session_callback(call: CallbackQuery):
    await call.answer()
    await show_admin_session_menu(call.message)


async def show_admin_session_menu(message: Message):
    uid = message.chat.id
    sessions = db.execute("SELECT id, username FROM admin_sessions").fetchall()
    if not sessions:
        await message.answer("🤖 বর্তমানে কোনো Admin Session যোগ করা হয়নি।", reply_markup=user_keyboard(uid))
        return
    kb = [[InlineKeyboardButton(text=f"📱 @{s['username']}", callback_data=f"use_admin_sess_{s['id']}")] for s in sessions]
    kb.append([InlineKeyboardButton(text="🔙 Main Menu", callback_data="main_menu_back")])
    await message.answer(
        "নিচের তালিকা থেকে সেশন নির্বাচন করুন। শুধুমাত্র যথাযথ অনুমতি থাকা সেশন ব্যবহার করুন:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )


@router.callback_query(F.data.startswith("use_admin_sess_"))
async def select_admin_session(call: CallbackQuery):
    await call.answer()
    uid = call.from_user.id
    sid = int(call.data.rsplit("_", 1)[1])
    row = db.execute("SELECT id, username FROM admin_sessions WHERE id=?", (sid,)).fetchone()
    if not row:
        await call.message.answer("❌ সেশনটি পাওয়া যায়নি।")
        return
    db.execute(
        """INSERT INTO user_admin_sessions(user_id,session_id,created_at) VALUES(?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET session_id=excluded.session_id,created_at=excluded.created_at""",
        (uid, sid, int(time.time())),
    )
    db.commit()
    await disconnect_client(uid)
    await call.message.answer(f"✅ Admin Session নির্বাচিত: @{row['username']}", reply_markup=user_keyboard(uid))

# =========================================================
# TARGET / LIVE CHANNEL SETUP
# =========================================================
@router.message(F.text == "🎯 Target Channel")
async def target_text_handler(message: Message, state: FSMContext):
    await target_start(message, state)


@router.callback_query(F.data == "target")
async def target_callback(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await target_start(call.message, state)


async def target_start(message: Message, state: FSMContext):
    uid = message.chat.id
    if is_blocked(uid):
        await message.answer("🚫 আপনার অ্যাকাউন্ট ব্লক করা হয়েছে।")
        return
    if not is_connected_user(uid) and not has_admin_session(uid):
        await message.answer("❌ আগে Connect Telegram অথবা Admin Session নির্বাচন করুন।")
        return
    old = get_channel(uid, "target")
    await message.answer(
        ("🎯 বর্তমান Target: " + f"{old['title']} ({old['channel_username']})\n" if old else "")
        + "Target Channel-এর public username পাঠান, যেমন @MyTargetChannel"
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
    uid = message.chat.id
    if is_blocked(uid):
        await message.answer("🚫 আপনার অ্যাকাউন্ট ব্লক করা হয়েছে।")
        return
    if not is_connected_user(uid) and not has_admin_session(uid):
        await message.answer("❌ আগে Connect Telegram অথবা Admin Session নির্বাচন করুন।")
        return
    old = get_channel(uid, "live")
    await message.answer(
        ("📺 বর্তমান Live: " + f"{old['title']} ({old['channel_username']})\n" if old else "")
        + "Live Stream Channel-এর public username পাঠান, যেমন @MyLiveChannel"
    )
    await state.set_state(LiveChannelState.waiting_channel)


def normalize_username(value):
    value = (value or "").strip()
    for prefix in ("https://t.me/", "http://t.me/"):
        if value.startswith(prefix):
            value = value[len(prefix):].split("/", 1)[0]
            break
    if value and not value.startswith("@"):
        value = "@" + value
    return value


@router.message(TargetChannelState.waiting_channel)
async def save_target_channel(message: Message, state: FSMContext):
    await save_channel(message, state, "target")


@router.message(LiveChannelState.waiting_channel)
async def save_live_channel(message: Message, state: FSMContext):
    await save_channel(message, state, "live")


async def save_channel(message: Message, state: FSMContext, channel_type: str):
    uid = message.chat.id
    username = normalize_username(message.text)
    if not username or username == "@":
        await message.answer("❌ সঠিক public Channel username দিন, যেমন @MyChannel")
        return
    try:
        client = await ensure_client(uid)
        entity = await client.get_entity(username)
        if not isinstance(entity, (types.Channel, types.Chat)):
            await message.answer("❌ এটি Telegram Channel/Group হিসেবে শনাক্ত হয়নি।")
            return
        title = getattr(entity, "title", username)
        db.execute(
            """INSERT INTO channels(owner_id,channel_type,channel_username,channel_id,title,active,auto_allow,created_at)
               VALUES(?,?,?,?,?,1,1,?)
               ON CONFLICT(owner_id,channel_type) DO UPDATE SET
               channel_username=excluded.channel_username,channel_id=excluded.channel_id,
               title=excluded.title,active=1,created_at=excluded.created_at""",

(uid, channel_type, username, entity.id, title, int(time.time())),
        )
        db.commit()
        await state.clear()
        await message.answer(
            f"✅ {channel_type.title()} Channel Added\n📺 {title}\n🔗 {username}",
            reply_markup=user_keyboard(uid),
        )
    except FloodWaitError as exc:
        await message.answer(f"⏳ Telegram বিরতি চেয়েছে। {exc.seconds} সেকেন্ড পরে চেষ্টা করুন।")
    except Exception:
        logger.exception("Channel setup failed: user=%s type=%s", uid, channel_type)
        await state.clear()
        await message.answer("❌ Channel যোগ করা যায়নি। Username ও account access যাচাই করুন।")


# =========================================================
# LIVE CALL HELPERS
# =========================================================
async def get_active_call(client, channel_username):
    entity = await client.get_entity(channel_username)
    full = await client(functions.channels.GetFullChannelRequest(channel=entity))
    return getattr(full.full_chat, "call", None), entity


async def get_participants(client, call):
    input_call = types.InputGroupCall(id=call.id, access_hash=call.access_hash)
    result = await client(functions.phone.GetGroupCallRequest(call=input_call, limit=100))
    users = {user.id: user for user in getattr(result, "users", [])}
    return list(getattr(result, "participants", [])), users


async def is_target_member(client, target_entity, user):
    try:
        input_user = await client.get_input_entity(user)
        result = await client(functions.channels.GetParticipantRequest(channel=target_entity, participant=input_user))
        return not isinstance(result.participant, (types.ChannelParticipantLeft, types.ChannelParticipantBanned))
    except FloodWaitError:
        raise
    except RPCError as exc:
        logger.debug("Membership check failed: %s", exc)
        return False
    except Exception as exc:
        logger.debug("Membership check error: %s", exc)
        return False


async def allow_participant(client, call, user):
    input_call = types.InputGroupCall(id=call.id, access_hash=call.access_hash)
    input_user = await client.get_input_entity(user)
    return await client(functions.phone.EditGroupCallParticipantRequest(
        call=input_call, participant=input_user, muted=False
    ))


# =========================================================
# LIVE BILLING + MONITOR
# =========================================================
async def charge_live_minute(user_id):
    """Charge only full live minutes. Atomic conditional update prevents negative balances."""
    rate = max(0, int(float(setting("coins_per_minute"))))
    u = user_row(user_id)
    if rate <= 0:
        return True
    db.execute("BEGIN IMMEDIATE")
    try:
        fresh = db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not fresh or fresh["balance"] < rate:
            db.execute("UPDATE users SET bot_enabled=0,billing_seconds=0,billing_last_ts=0 WHERE user_id=?", (user_id,))
            db.commit()
            return False
        db.execute(
            "UPDATE users SET balance=balance-?, total_spent=total_spent+? WHERE user_id=?",
            (rate, rate, user_id),
        )
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise


async def bill_if_live(user_id, is_live):
    # Use wall-clock timestamps, not loop-count estimates, so slow API calls
    # do not distort the charged live duration. Billing pauses while offline/OFF.
    u = user_row(user_id)
    now = int(time.time())
    if not u["bot_enabled"] or not is_live or is_blocked(user_id):
        db.execute(
            "UPDATE users SET billing_seconds=0, billing_last_ts=0 WHERE user_id=?",
            (user_id,),
        )
        db.commit()
        return True

last_ts = int(u["billing_last_ts"] or 0)
    elapsed = max(0, min(now - last_ts, 2 * 60)) if last_ts else 0
    seconds = int(u["billing_seconds"] or 0) + elapsed
    db.execute("UPDATE users SET billing_last_ts=? WHERE user_id=?", (now, user_id))
    db.commit()

    while seconds >= 60:
        if not await charge_live_minute(user_id):
            return False
        seconds -= 60
    db.execute("UPDATE users SET billing_seconds=? WHERE user_id=?", (seconds, user_id))
    db.commit()
    return True


async def monitor_user(user_id):
    lock = get_lock(monitor_locks, user_id)
    if lock.locked():
        return
    async with lock:
        u = user_row(user_id)
        if is_blocked(user_id) or not u["bot_enabled"]:
            return
        target_row = get_channel(user_id, "target")
        live_row = get_channel(user_id, "live")
        if not target_row or not live_row:
            return
        if not is_connected_user(user_id) and not has_admin_session(user_id):
            return
        try:
            client = await ensure_client(user_id)
            call, _ = await get_active_call(client, live_row["channel_username"])
            if not call:
                await bill_if_live(user_id, False)
                return
            if not await bill_if_live(user_id, True):
                with suppress(Exception):
                    await bot.send_message(user_id, "💸 Balance অপর্যাপ্ত। Bot OFF করা হয়েছে; Top-up করুন।",
                                           reply_markup=user_keyboard(user_id))
                return
            target_entity = await client.get_entity(target_row["channel_username"])
            participants, users = await get_participants(client, call)
            for participant in participants:
                peer = getattr(participant, "peer", None)
                if not isinstance(peer, types.PeerUser):
                    continue
                uid = peer.user_id
                user_entity = users.get(uid)
                if user_entity is None:
                    try:
                        user_entity = await client.get_entity(uid)
                    except Exception:
                        continue
                if not await is_target_member(client, target_entity, user_entity):
                    continue
                if not getattr(participant, "muted", False):
                    continue
                try:
                    await allow_participant(client, call, user_entity)
                    logger.info("Unmute request sent for user=%s", uid)
                except FloodWaitError as exc:
                    logger.warning("FloodWait %s seconds", exc.seconds)
                    await asyncio.sleep(min(exc.seconds, 60))
                except RPCError as exc:
                    logger.warning("Telegram rejected unmute for %s: %s", uid, exc)
                except Exception:
                    logger.exception("Could not unmute participant %s", uid)
                await asyncio.sleep(0.15)
        except FloodWaitError as exc:
            logger.warning("Monitor FloodWait user=%s seconds=%s", user_id, exc.seconds)
        except RPCError as exc:
            logger.warning("Monitor API error user=%s: %s", user_id, exc)
        except Exception:
            logger.exception("Monitor error for user=%s", user_id)


async def auto_monitor():
    logger.info("Auto Speaker Monitor started; interval=%s seconds", MONITOR_INTERVAL)
    while True:
        try:
            rows = db.execute("SELECT user_id FROM users WHERE bot_enabled=1 AND blocked=0").fetchall()
            for row in rows:
                await monitor_user(row["user_id"])
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Auto-monitor loop error")
        await asyncio.sleep(MONITOR_INTERVAL)


@router.message(F.text == "📊 Live Status")
async def status_text_handler(message: Message):
    await live_status_message(message)

@router.callback_query(F.data == "status")
async def status_callback(call: CallbackQuery):
    await call.answer()
    await live_status_message(call.message)


async def live_status_message(message: Message):
    uid = message.chat.id
    if is_blocked(uid):
        await message.answer("🚫 আপনার অ্যাকাউন্ট ব্লক করা হয়েছে।")
        return
    target = get_channel(uid, "target")
    live = get_channel(uid, "live")
    if not target or not live:
        await message.answer("❌ আগে Target Channel এবং Live Stream Channel সেট করুন।")
        return
    u = user_row(uid)
    try:
        client = await ensure_client(uid)
        call, _ = await get_active_call(client, live["channel_username"])
        if not call:
            await bill_if_live(uid, False)
            await message.answer(
                f"🔴 LIVE OFFLINE\n🎯 Target: {target['title']}\n📺 Live: {live['title']}\n"
                f"💎 Balance: {u['balance']:,}\n⏯️ Bot: {'ON' if u['bot_enabled'] else 'OFF'}",
                reply_markup=user_keyboard(uid),
            )
            return
        target_entity = await client.get_entity(target["channel_username"])
        participants, users = await get_participants(client, call)
        members = muted = allowed = unchecked = 0
        for participant in participants:
            peer = getattr(participant, "peer", None)
            if not isinstance(peer, types.PeerUser):
                continue
            entity = users.get(peer.user_id)
            if entity is None:
                try:
                    entity = await client.get_entity(peer.user_id)
                except Exception:
                    unchecked += 1
                    continue
            try:
                member = await is_target_member(client, target_entity, entity)
            except FloodWaitError:
                unchecked += 1
                continue
            if not member:
                unchecked += 1
                continue
            members += 1
            if getattr(participant, "muted", False):
                muted += 1
            else:
                allowed += 1
            await asyncio.sleep(0.05)
        await message.answer(
            f"🎙 <b>LIVE STATUS</b>\n\n🎯 Target: {target['title']}\n📺 Live: {live['title']}\n"
            f"👥 শনাক্ত Target Members: {members}\n🔇 Muted: {muted}\n🎤 Unmuted: {allowed}\n"
            f"👤 অন্য সদস্য: {unchecked}\n\n"
            f"⏯️ Bot: {'🟢 ON' if u['bot_enabled'] else '🔴 OFF'}\n"
            f"💎 Balance: {u['balance']:,} Coins",
            parse_mode="HTML", reply_markup=user_keyboard(uid),
        )
    except Exception:
        logger.exception("Status check failed for user %s", uid)
        await message.answer("❌ Status পাওয়া যায়নি। Session, channel access ও live call যাচাই করুন।")


# =========================================================
# ADMIN: DEPOSIT REQUESTS / SETTINGS / REPORTS
# =========================================================
@router.message(F.text == "💰 Deposit Requests")
async def deposits_text(message: Message):
    await show_deposit_requests(message, message.from_user.id)


@router.callback_query(F.data == "admin_deposits")
async def deposits_callback(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Access denied", show_alert=True)
        return
    await call.answer()
    await show_deposit_requests(call.message, call.from_user.id)


async def show_deposit_requests(message, actor_id):
    if not is_admin(actor_id):
        return
    rows = db.execute("SELECT * FROM deposit_requests WHERE status='pending' ORDER BY id ASC LIMIT 20").fetchall()
    if not rows:
        await message.answer("💰 কোনো Pending Deposit Request নেই।", reply_markup=admin_reply_keyboard())
        return
    for r in rows:

try:
            await bot.send_photo(
                actor_id, r["photo_file_id"],
                caption=(f"💰 <b>Deposit #{r['id']}</b>\nUser: <code>{r['user_id']}</code>\n"
                         f"Amount: ${r['amount_usd']:.2f}\nCoins: {r['coins']:,}\nStatus: PENDING"),
                parse_mode="HTML", reply_markup=deposit_review_keyboard(r["id"]),
            )
        except Exception:
            logger.exception("Could not send deposit request %s", r["id"])


@router.message(F.text == "⚙️ Deposit Settings")
async def settings_text(message: Message):
    await show_settings(message, message.from_user.id)


@router.callback_query(F.data == "admin_settings")
async def settings_callback(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Access denied", show_alert=True)
        return
    await call.answer()
    await show_settings(call.message, call.from_user.id)


async def show_settings(message, actor_id):
    if not is_admin(actor_id):
        return
    text = (
        "⚙️ <b>Deposit & Billing Settings</b>\n\n"
        f"🟡 Binance Pay ID: <code>{setting('binance_pay_id')}</code>\n"
        f"💵 Coins per $1: {int(setting('coins_per_dollar')):,}\n"
        f"🔻 Minimum deposit: ${float(setting('min_deposit_usd')):g}\n"
        f"⏱️ Coins per live minute: {int(setting('coins_per_minute')):,}\n\n"
        "নিচের অপশন থেকে পরিবর্তন করুন:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟡 Change Binance Pay ID", callback_data="set_payid")],
        [InlineKeyboardButton(text="💵 Change Coins per Dollar", callback_data="set_rate")],
        [InlineKeyboardButton(text="🔻 Change Minimum Deposit", callback_data="set_minimum")],
        [InlineKeyboardButton(text="⏱️ Change Coins per Minute", callback_data="set_charge")],
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")],
    ])
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.in_({"set_payid", "set_rate", "set_minimum", "set_charge"}))
async def setting_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Access denied", show_alert=True)
        return
    await call.answer()
    mapping = {
        "set_payid": (DepositSettingState.waiting_pay_id, "নতুন Binance Pay ID পাঠান:"),
        "set_rate": (DepositSettingState.waiting_rate, "প্রতি $1-এ কত Coins হবে তা লিখুন (পূর্ণ সংখ্যা):"),
        "set_minimum": (DepositSettingState.waiting_minimum, "সর্বনিম্ন Deposit কত USD হবে লিখুন:"),
        "set_charge": (DepositSettingState.waiting_charge, "প্রতি Live মিনিটে কত Coins কাটবে লিখুন (0 দিলে billing বন্ধ):"),
    }
    next_state, prompt = mapping[call.data]
    await state.set_state(next_state)
    await call.message.answer(prompt)


@router.message(DepositSettingState.waiting_pay_id)
async def save_pay_id(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    value = (message.text or "").strip()
    if not value or len(value) > 100:
        await message.answer("❌ সঠিক Binance Pay ID দিন।")
        return
    set_setting("binance_pay_id", value)
    await state.clear()
    await message.answer("✅ Binance Pay ID আপডেট হয়েছে।", reply_markup=admin_reply_keyboard())


@router.message(DepositSettingState.waiting_rate)
async def save_rate(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        value = int((message.text or "").strip())
        if value <= 0 or value > 10**12:
            raise ValueError
    except ValueError:
        await message.answer("❌ 0-এর চেয়ে বড় পূর্ণ সংখ্যা লিখুন।")
        return
    set_setting("coins_per_dollar", value)
    await state.clear()
    await message.answer("✅ Coins per dollar আপডেট হয়েছে।", reply_markup=admin_reply_keyboard())

@router.message(DepositSettingState.waiting_minimum)
async def save_minimum(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        value = float((message.text or "").strip())
        if value <= 0 or value > 1000000:
            raise ValueError
    except ValueError:
        await message.answer("❌ 0-এর চেয়ে বড় সঠিক USD পরিমাণ লিখুন।")
        return
    set_setting("min_deposit_usd", value)
    await state.clear()
    await message.answer("✅ Minimum Deposit আপডেট হয়েছে।", reply_markup=admin_reply_keyboard())


@router.message(DepositSettingState.waiting_charge)
async def save_charge(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        value = int((message.text or "").strip())
        if value < 0 or value > 10**12:
            raise ValueError
    except ValueError:
        await message.answer("❌ 0 অথবা ধনাত্মক পূর্ণ সংখ্যা লিখুন।")
        return
    set_setting("coins_per_minute", value)
    await state.clear()
    await message.answer("✅ Per-minute charge আপডেট হয়েছে।", reply_markup=admin_reply_keyboard())


@router.message(F.text == "📊 User Reports")
async def reports_text(message: Message):
    await show_user_reports(message, message.from_user.id)


@router.callback_query(F.data == "admin_reports")
async def reports_callback(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Access denied", show_alert=True)
        return
    await call.answer()
    await show_user_reports(call.message, call.from_user.id)


async def show_user_reports(message, actor_id):
    if not is_admin(actor_id):
        return
    rows = db.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT 100").fetchall()
    if not rows:
        await message.answer("কোনো ইউজার নেই।", reply_markup=admin_reply_keyboard())
        return
    await message.answer(f"📊 User Report — মোট {len(rows)} জন (সর্বোচ্চ 100 জন):", reply_markup=admin_reply_keyboard())
    for u in rows:
        channels = db.execute(
            "SELECT channel_type, channel_username FROM channels WHERE owner_id=? AND active=1",
            (u["user_id"],),
        ).fetchall()
        channel_text = "\n".join(f"• {c['channel_type']}: {c['channel_username']}" for c in channels) or "কোনো চ্যানেল নেই"
        report = (
            f"👤 <b>User</b>\n🆔 <code>{u['user_id']}</code>\n"
            f"📺 Channels:\n{channel_text}\n\n"
            f"💎 Balance: {u['balance']:,} Coins\n"
            f"🔥 Spent: {u['total_spent']:,} Coins\n"
            f"⏯️ Bot: {'ON' if u['bot_enabled'] else 'OFF'}\n"
            f"📌 Status: {'BLOCKED' if u['blocked'] else 'Active'}"
        )
        await message.answer(report, parse_mode="HTML")


@router.message(F.text == "🔍 Search User")
async def search_start_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(SearchUserState.waiting_user_id)
    await message.answer("খোঁজার জন্য Telegram User ID দিন:")


@router.callback_query(F.data == "admin_search")
async def search_start_callback(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Access denied", show_alert=True)
        return
    await call.answer()
    await state.set_state(SearchUserState.waiting_user_id)
    await call.message.answer("খোঁজার জন্য Telegram User ID দিন:")


@router.message(SearchUserState.waiting_user_id)
async def search_user_result(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    try:
        target_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("❌ সঠিক User ID দিন।")
        return

u = db.execute("SELECT * FROM users WHERE user_id=?", (target_id,)).fetchone()
    if not u:
        await message.answer("❌ ইউজার পাওয়া যায়নি।", reply_markup=admin_reply_keyboard())
        return
    channels = db.execute("SELECT channel_type,channel_username,title FROM channels WHERE owner_id=?", (target_id,)).fetchall()
    ctext = "\n".join(f"• {c['channel_type']}: {c['channel_username']} ({c['title'] or 'Unknown'})" for c in channels) or "কোনো চ্যানেল নেই"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚫 Block" if not u["blocked"] else "🔓 Unblock",
                             callback_data=f"admin_block_{target_id}" if not u["blocked"] else f"admin_unblock_{target_id}")
    ], [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]])
    await message.answer(
        f"👤 <b>User Info</b>\n🆔 <code>{target_id}</code>\n"
        f"📌 Status: {'Blocked' if u['blocked'] else 'Active'}\n"
        f"📺 Channels:\n{ctext}\n💎 Balance: {u['balance']:,}\n🔥 Spent: {u['total_spent']:,}",
        parse_mode="HTML", reply_markup=kb,
    )


@router.callback_query(F.data.startswith("admin_block_"))
async def admin_block_user(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Access denied", show_alert=True)
        return
    await call.answer()
    uid = int(call.data.rsplit("_", 1)[1])
    db.execute("UPDATE users SET blocked=1,bot_enabled=0,billing_seconds=0,billing_last_ts=0 WHERE user_id=?", (uid,))
    db.commit()
    await disconnect_client(uid)
    await call.message.edit_text(f"🚫 User <code>{uid}</code> ব্লক করা হয়েছে।", parse_mode="HTML", reply_markup=admin_inline_keyboard())


@router.callback_query(F.data.startswith("admin_unblock_"))
async def admin_unblock_user(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Access denied", show_alert=True)
        return
    await call.answer()
    uid = int(call.data.rsplit("_", 1)[1])
    db.execute("UPDATE users SET blocked=0 WHERE user_id=?", (uid,))
    db.commit()
    await call.message.edit_text(f"🔓 User <code>{uid}</code> আনব্লক করা হয়েছে।", parse_mode="HTML", reply_markup=admin_inline_keyboard())


# =========================================================
# BROADCAST / MANAGE ADMIN SESSIONS
# =========================================================
@router.message(F.text == "📢 Broadcast Message")
async def broadcast_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(BroadcastState.waiting_message)
    await message.answer("সব ইউজারকে পাঠানোর জন্য মেসেজ লিখুন:")


@router.callback_query(F.data == "admin_broadcast")
async def broadcast_callback(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Access denied", show_alert=True)
        return
    await call.answer()
    await state.set_state(BroadcastState.waiting_message)
    await call.message.answer("সব ইউজারকে পাঠানোর জন্য মেসেজ লিখুন:")


@router.message(BroadcastState.waiting_message)
async def broadcast_send(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = message.text
    await state.clear()
    if not text:
        await message.answer("❌ শুধু Text broadcast সমর্থিত।")
        return
    rows = db.execute("SELECT user_id FROM users WHERE blocked=0").fetchall()
    success = failed = 0
    for row in rows:
        try:
            await bot.send_message(row["user_id"], text, parse_mode=None)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    await message.answer(f"📢 Broadcast Complete\nসফল: {success}\nব্যর্থ: {failed}", reply_markup=admin_reply_keyboard())


@router.message(F.text == "🧩 Manage Admin Session")
async def manage_sessions_text(message: Message):
    await show_manage_sessions(message, message.from_user.id)

@router.callback_query(F.data == "admin_manage_session")
async def manage_sessions_callback(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Access denied", show_alert=True)
        return
    await call.answer()
    await show_manage_sessions(call.message, call.from_user.id)


async def show_manage_sessions(message, actor_id):
    if not is_admin(actor_id):
        return
    rows = db.execute("SELECT id,username FROM admin_sessions").fetchall()
    text = "🧩 <b>Manage Admin Sessions</b>\n\n"
    text += "\n".join(f"ID {r['id']}: @{r['username']}" for r in rows) if rows else "কোনো সেশন নেই।"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add Session", callback_data="admin_add_session")],
        [InlineKeyboardButton(text="🗑 Delete Session", callback_data="admin_del_session_list")],
        [InlineKeyboardButton(text="👥 View Session Users", callback_data="admin_view_sess_users")],
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")],
    ])
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "admin_add_session")
async def admin_add_session_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Access denied", show_alert=True)
        return
    await call.answer()
    await state.set_state(AdminSessionState.waiting_username)
    await call.message.answer("অ্যাকাউন্টের username দিন (যেমন @myaccount):")


@router.message(AdminSessionState.waiting_username)
async def admin_add_session_username(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    username = normalize_username(message.text)
    if not username or username == "@":
        await message.answer("❌ সঠিক username দিন।")
        return
    await state.update_data(username=username.lstrip("@"))
    await state.set_state(AdminSessionState.waiting_string)
    await message.answer("String Session দিন। এটি গোপন credential—শুধু আপনার অনুমোদিত অ্যাকাউন্ট ব্যবহার করুন।")


@router.message(AdminSessionState.waiting_string)
async def admin_add_session_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if not message.text or len(message.text.strip()) < 30:
        await message.answer("❌ Session অসম্পূর্ণ।")
        return
    data = await state.get_data()
    username = data.get("username", "unknown")
    # Validate before saving; never echo the session back.
    test_client = TelegramClient(StringSession(message.text.strip()), API_ID, API_HASH)
    try:
        await test_client.connect()
        if not await test_client.is_user_authorized():
            await message.answer("❌ Session authorized নয়; সংরক্ষণ করা হয়নি।")
            return
        me = await test_client.get_me()
        username = (me.username or username).lstrip("@")
        db.execute(
            "INSERT INTO admin_sessions(username,session_string,added_at) VALUES(?,?,?)",
            (username, message.text.strip(), int(time.time())),
        )
        db.commit()
        await state.clear()
        with suppress(Exception):
            await message.delete()
        await message.answer(f"✅ Admin Session @{username} যোগ হয়েছে।", reply_markup=admin_reply_keyboard())
    except Exception:
        logger.exception("Admin session validation failed")
        await message.answer("❌ Session যাচাই করা যায়নি।")
    finally:
        with suppress(Exception):
            await test_client.disconnect()


@router.callback_query(F.data == "admin_del_session_list")
async def admin_del_session_list(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Access denied", show_alert=True)

return
    await call.answer()
    rows = db.execute("SELECT id,username FROM admin_sessions").fetchall()
    if not rows:
        await call.message.answer("কোনো Admin Session নেই।", reply_markup=admin_inline_keyboard())
        return
    kb = [[InlineKeyboardButton(text=f"🗑 Delete @{r['username']}", callback_data=f"del_sess_{r['id']}")] for r in rows]
    kb.append([InlineKeyboardButton(text="🔙 Back", callback_data="admin_manage_session")])
    await call.message.answer("মুছতে সেশন নির্বাচন করুন:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@router.callback_query(F.data.startswith("del_sess_"))
async def admin_delete_session(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Access denied", show_alert=True)
        return
    await call.answer()
    sid = int(call.data.rsplit("_", 1)[1])
    db.execute("DELETE FROM admin_sessions WHERE id=?", (sid,))
    db.execute("DELETE FROM user_admin_sessions WHERE session_id=?", (sid,))
    db.commit()
    for uid in list(clients.keys()):
        if not is_connected_user(uid) and not has_admin_session(uid):
            await disconnect_client(uid)
    await call.message.edit_text("✅ Session মুছে ফেলা হয়েছে।", reply_markup=admin_inline_keyboard())


@router.callback_query(F.data == "admin_view_sess_users")
async def admin_view_sess_users(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Access denied", show_alert=True)
        return
    await call.answer()
    rows = db.execute(
        """SELECT uas.user_id, a.username FROM user_admin_sessions uas
           JOIN admin_sessions a ON a.id=uas.session_id ORDER BY uas.user_id"""
    ).fetchall()
    text = "👥 <b>Admin Session ব্যবহারকারী</b>\n\n"
    text += "\n".join(f"• <code>{r['user_id']}</code> — @{r['username']}" for r in rows) if rows else "কেউ ব্যবহার করছে না।"
    await call.message.answer(text, parse_mode="HTML", reply_markup=admin_inline_keyboard())


# =========================================================
# WEB SERVER / KEEP-ALIVE
# =========================================================
web_app = Flask(name)


@web_app.route("/")
def home():
    return "Telegram Auto Speaker: ACTIVE"


def ping_self():
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
    if not render_url:
        return
    while True:
        try:
            time.sleep(280)
            requests.get(render_url, timeout=10)
            logger.info("Keep-alive ping sent")
        except Exception as exc:
            logger.warning("Self ping warning: %s", exc)


def run_web():
    web_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", PORT)), use_reloader=False)


def keep_alive():
    Thread(target=run_web, daemon=True).start()
    if os.environ.get("RENDER_EXTERNAL_URL", "").strip():
        Thread(target=ping_self, daemon=True).start()


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
        for uid in list(clients.keys()):
            await disconnect_client(uid)
        await bot.session.close()
        db.close()


if name == "main":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")
