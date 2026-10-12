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
)
from aiogram.filters import CommandStart
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

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🔐 Connect Telegram"),
                KeyboardButton(text="🎯 Target Channel"),
            ],
            [
                KeyboardButton(text="📺 Live Stream Channel"),
                KeyboardButton(text="📊 Live Status"),
            ],
            [
                KeyboardButton(text="ℹ️ Help"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔐 Connect Telegram",
                    callback_data="connect",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎯 Target Channel",
                    callback_data="target",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📺 Live Stream Channel",
                    callback_data="live",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 Live Status",
                    callback_data="status",
                )
            ],
            [
                InlineKeyboardButton(
                    text="ℹ Help",
                    callback_data="help",
                )
            ],
        ]
    )


# =========================================================
# DATABASE HELPERS
# =========================================================

def is_connected_user(user_id):
    row = db.execute(
        "SELECT connected FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()

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


# =========================================================
# START
# =========================================================

@router.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "🎙 <b>TELEGRAM AUTO SPEAKER</b>\n\n"
        "Target Channel-এর সদস্যরা তোমার Live Stream Channel-এর "
        "লাইভ কলে যোগ দিলে বট তাদের শনাক্ত করার চেষ্টা করবে। "
        "প্রয়োজনীয় Telegram permission থাকলে muted সদস্যকে "
        "unmute করার অনুরোধ পাঠাবে।\n\n"
        "প্রথমে 🔐 Connect Telegram করো।",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

    await message.answer(
        "👇 Control Panel:",
        reply_markup=main_menu(),
    )


# =========================================================
# HELP
# =========================================================

async def send_help(message: Message):
    await message.answer(
        "ℹ️ <b>কিভাবে ব্যবহার করবে</b>\n\n"
        "1️⃣ 🔐 Connect Telegram চাপো।\n\n"
        "2️⃣ নিজের বা অনুমোদিত Telegram String Session সংযুক্ত করো।\n\n"
        "3️⃣ 🎯 Target Channel সেট করো।\n"
        "এই চ্যানেলের সদস্যদের যাচাই করা হবে।\n\n"
        "4️⃣ 📺 Live Stream Channel সেট করো।\n"
        "এখানে তোমার লাইভ চলবে।\n\n"
        "5️⃣ লাইভ চালু রাখো। বট পর্যায়ক্রমে অংশগ্রহণকারীদের পরীক্ষা করবে।\n\n"
        "6️⃣ 📊 Live Status থেকে অবস্থা দেখো।\n\n"
        "⚠️ সব অংশগ্রহণকারী Telegram API-তে দৃশ্যমান নাও হতে পারে। "
        "Unmute করার জন্য প্রয়োজনীয় call/admin permission লাগতে পারে।\n\n"
        "🔐 String Session কখনো অন্য কাউকে দেবে না।",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
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
async def connect_text_handler(
    message: Message,
    state: FSMContext,
):
    await connect_telegram_menu(message, state)


@router.callback_query(F.data == "connect")
async def connect_callback(
    call: CallbackQuery,
    state: FSMContext,
):
    await call.answer()
    await connect_telegram_menu(call.message, state)


async def connect_telegram_menu(
    message: Message,
    state: FSMContext,
):
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
                reply_markup=main_keyboard(),
            )
            return

        except Exception:
            logger.warning(
                "Saved Telegram session invalid for user %s",
                user_id,
            )

            await disconnect_client(user_id)

            db.execute(
                "UPDATE users SET connected=0 WHERE user_id=?",
                (user_id,),
            )
            db.commit()

    await state.set_state(StringSessionState.waiting_session)

    await message.answer(
        "🔐 <b>Connect via String Session</b>\n\n"
        "নিজের বা ব্যবহারের অনুমতি আছে এমন Telegram account-এর "
        "Telethon String Session পাঠাও।\n\n"
        "⚠️ এটি অ্যাকাউন্টের গোপন প্রবেশ-তথ্য। কাউকে শেয়ার করবে না।",
        parse_mode="HTML",
    )


@router.message(StringSessionState.waiting_session)
async def process_string_session(
    message: Message,
    state: FSMContext,
):
    if not message.text:
        await message.answer("❌ String Session টেক্সট হিসেবে পাঠাও।")
        return

    session_string = message.text.strip()
    user_id = message.chat.id

    if len(session_string) < 30:
        await message.answer(
            "❌ String Session অসম্পূর্ণ মনে হচ্ছে। আবার চেষ্টা করো。"
        )
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

            await message.answer(
                "❌ Session অনুমোদিত নয় বা মেয়াদোত্তীর্ণ। "
                "সঠিক session দিয়ে আবার চেষ্টা করো।"
            )
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
                (user_id, session_string, connected, created_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                session_string=excluded.session_string,
                connected=1,
                created_at=excluded.created_at
            """,
            (user_id, session_string, int(time.time())),
        )
        db.commit()

        await state.clear()

        with suppress(Exception):
            await message.delete()

        await message.answer(
            "✅ <b>Telegram Connected Successfully!</b>\n\n"
            f"Account ID: <code>{me.id}</code>\n\n"
            "এখন Target ও Live Channel সেট করো।",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )

    except Exception:
        logger.exception(
            "Telegram session connection failed for user %s",
            user_id,
        )

        with suppress(Exception):
            await client.disconnect()

        await state.clear()

        await message.answer(
            "❌ কানেক্ট করা যায়নি। Session, API_ID এবং API_HASH "
            "যাচাই করে আবার চেষ্টা করো।"
        )


# =========================================================
# TARGET CHANNEL MENU
# =========================================================

@router.message(F.text == "🎯 Target Channel")
async def target_text_handler(
    message: Message,
    state: FSMContext,
):
    await target_start(message, state)


@router.callback_query(F.data == "target")
async def target_callback(
    call: CallbackQuery,
    state: FSMContext,
):
    await call.answer()
    await target_start(call.message, state)


async def target_start(
    message: Message,
    state: FSMContext,
):
    user_id = message.chat.id

    if not is_connected_user(user_id):
        await message.answer("❌ আগে 🔐 Connect Telegram করো।")
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
            "যে চ্যানেলের সদস্যদের যাচাই করতে চাও তার "
            "public username পাঠাও।\n\n"
            "উদাহরণ: <code>@MyTargetChannel</code>",
            parse_mode="HTML",
        )

    await state.set_state(TargetChannelState.waiting_channel)


# =========================================================
# LIVE CHANNEL MENU
# =========================================================

@router.message(F.text == "📺 Live Stream Channel")
async def live_text_handler(
    message: Message,
    state: FSMContext,
):
    await live_start(message, state)


@router.callback_query(F.data == "live")
async def live_callback(
    call: CallbackQuery,
    state: FSMContext,
):
    await call.answer()
    await live_start(call.message, state)


async def live_start(
    message: Message,
    state: FSMContext,
):
    user_id = message.chat.id

    if not is_connected_user(user_id):
        await message.answer("❌ আগে 🔐 Connect Telegram করো।")
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
            "যে public চ্যানেলে তুমি লাইভ করো তার username পাঠাও。\n\n"
            "উদাহরণ: <code>@MyLiveChannel</code>",
            parse_mode="HTML",
        )

    await state.set_state(LiveChannelState.waiting_channel)


# =========================================================
# SAVE CHANNEL
# =========================================================

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


async def save_channel(
    message: Message,
    state: FSMContext,
    channel_type: str,
):
    user_id = message.chat.id
    username = normalize_username(message.text)

    if not username or username == "@":
        await message.answer(
            "❌ সঠিক public Channel username দাও, যেমন @MyChannel"
        )
        return

    try:
        client = await ensure_client(user_id)
        entity = await client.get_entity(username)

        if not isinstance(entity, (types.Channel, types.Chat)):
            await message.answer(
                "❌ এটি Telegram Channel/Group হিসেবে শনাক্ত হয়নি।"
            )
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
            (
                user_id,
                channel_type,
                username,
                entity.id,
                title,
                int(time.time()),
            ),
        )
        db.commit()

        await state.clear()

        if channel_type == "target":
            text = (
                "✅ <b>Target Channel Added</b>\n\n"
                f"📺 {title}\n"
                f"🔗 {username}\n\n"
                "এখন 📺 Live Stream Channel সেট করো।"
            )
        else:
            text = (
                "✅ <b>Live Stream Channel Added</b>\n\n"
                f"📺 {title}\n"
                f"🔗 {username}\n\n"
                "লাইভ চালু হলে 📊 Live Status দিয়ে পরীক্ষা করো।"
            )

        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )

    except FloodWaitError as exc:
        await message.answer(
            f"⏳ Telegram সাময়িক বিরতি চেয়েছে। "
            f"{exc.seconds} সেকেন্ড পরে চেষ্টা করো।"
        )

    except Exception:
        logger.exception(
            "Channel setup failed: user=%s type=%s",
            user_id,
            channel_type,
        )

        await state.clear()

        await message.answer(
            "❌ Channel যোগ করা যায়নি। Username ঠিক আছে কি না এবং "
            "connected account চ্যানেলটি দেখতে পারে কি না যাচাই করো।"
        )


# =========================================================
# LIVE CALL FUNCTIONS
# =========================================================

async def get_active_call(client, channel_username):
    entity = await client.get_entity(channel_username)

    full = await client(
        functions.channels.GetFullChannelRequest(channel=entity)
    )

    call = getattr(full.full_chat, "call", None)

    return call, entity


async def get_participants(client, call):
    input_call = types.InputGroupCall(
        id=call.id,
        access_hash=call.access_hash,
    )

    result = await client(
        functions.phone.GetGroupCallRequest(
            call=input_call,
            limit=100,
        )
    )

    users = {
        user.id: user
        for user in getattr(result, "users", [])
    }

    participants = list(
        getattr(result, "participants", [])
    )

    return participants, users


async def is_target_member(client, target_entity, user):
    try:
        input_user = await client.get_input_entity(user)

        result = await client(
            functions.channels.GetParticipantRequest(
                channel=target_entity,
                participant=input_user,
            )
        )

        member = result.participant

        if isinstance(
            member,
            (
                types.ChannelParticipantLeft,
                types.ChannelParticipantBanned,
            ),
        ):
            return False

        return True

    except FloodWaitError:
        raise

    except RPCError as exc:
        logger.debug(
            "Membership check failed for %s: %s",
            getattr(user, "id", user),
            exc,
        )
        return False

    except Exception as exc:
        logger.debug("Membership check error: %s", exc)
        return False


async def allow_participant(client, call, user):
    input_call = types.InputGroupCall(
        id=call.id,
        access_hash=call.access_hash,
    )

    input_user = await client.get_input_entity(user)

    return await client(
        functions.phone.EditGroupCallParticipantRequest(
            call=input_call,
            participant=input_user,
            muted=False,
        )
    )


# =========================================================
# AUTOMATIC MONITOR
# =========================================================

async def monitor_user(user_id):
    lock = get_lock(monitor_locks, user_id)

    if lock.locked():
        return

    async with lock:
        target_row = get_channel(user_id, "target")
        live_row = get_channel(user_id, "live")

        if not target_row or not live_row:
            return

        if not is_connected_user(user_id):
            return

        try:
            client = await ensure_client(user_id)

            call, _ = await get_active_call(
                client,
                live_row["channel_username"],
            )

            if not call:
                return

            target_entity = await client.get_entity(
                target_row["channel_username"]
            )

            participants, users = await get_participants(client, call)

            for participant in participants:
                peer = getattr(participant, "peer", None)

                if not isinstance(peer, types.PeerUser):
                    continue

                user_id_in_call = peer.user_id

                user_entity = users.get(user_id_in_call)

                if user_entity is None:
                    try:
                        user_entity = await client.get_entity(
                            user_id_in_call
                        )
                    except Exception:
                        logger.debug(
                            "Could not resolve live participant %s",
                            user_id_in_call,
                        )
                        continue

                if not await is_target_member(
                    client,
                    target_entity,
                    user_entity,
                ):
                    continue

                if not getattr(participant, "muted", False):
                    continue

                try:
                    await allow_participant(
                        client,
                        call,
                        user_entity,
                    )

                    logger.info(
                        "Unmute request sent for user=%s target=%s live=%s",
                        user_id_in_call,
                        target_row["channel_username"],
                        live_row["channel_username"],
                    )

                except FloodWaitError as exc:
                    logger.warning(
                        "Telegram FloodWait: %s seconds",
                        exc.seconds,
                    )
                    await asyncio.sleep(min(exc.seconds, 60))

                except RPCError as exc:
                    logger.warning(
                        "Telegram rejected unmute for user %s: %s",
                        user_id_in_call,
                        exc,
                    )

                except Exception:
                    logger.exception(
                        "Could not unmute participant %s",
                        user_id_in_call,
                    )

                await asyncio.sleep(0.15)

        except FloodWaitError as exc:
            logger.warning(
                "Monitor FloodWait user=%s seconds=%s",
                user_id,
                exc.seconds,
            )
            await asyncio.sleep(min(exc.seconds, 60))

        except RPCError as exc:
            logger.warning(
                "Telegram monitor API error user=%s: %s",
                user_id,
                exc,
            )

        except Exception:
            logger.exception("Monitor error for user=%s", user_id)


async def auto_monitor():
    logger.info(
        "Auto Speaker Monitor started; interval=%s seconds",
        MONITOR_INTERVAL,
    )

    while True:
        try:
            rows = db.execute(
                """
                SELECT DISTINCT owner_id
                FROM channels
                WHERE active=1
                """
            ).fetchall()

            for row in rows:
                await monitor_user(row["owner_id"])
                await asyncio.sleep(0.25)

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception("Auto-monitor loop error")

        await asyncio.sleep(MONITOR_INTERVAL)


# =========================================================
# LIVE STATUS
# =========================================================

@router.message(F.text == "📊 Live Status")
async def status_text_handler(message: Message):
    await live_status_message(message)


@router.callback_query(F.data == "status")
async def status_callback(call: CallbackQuery):
    await call.answer()
    await live_status_message(call.message)


async def live_status_message(message: Message):
    user_id = message.chat.id

    if not is_connected_user(user_id):
        await message.answer("❌ আগে 🔐 Connect Telegram করো।")
        return

    target_row = get_channel(user_id, "target")
    live_row = get_channel(user_id, "live")

    if not target_row:
        await message.answer("❌ 🎯 Target Channel সেট করা হয়নি।")
        return

    if not live_row:
        await message.answer("❌ 📺 Live Stream Channel সেট করা হয়নি।")
        return

    try:
        client = await ensure_client(user_id)

        call, _ = await get_active_call(
            client,
            live_row["channel_username"],
        )

        if not call:
            await message.answer(
                "🔴 <b>LIVE OFFLINE</b>\n\n"
                f"🎯 Target: {target_row['title']}\n"
                f"📺 Live: {live_row['title']}",
                parse_mode="HTML",
                reply_markup=main_keyboard(),
            )
            return

        target_entity = await client.get_entity(
            target_row["channel_username"]
        )

        participants, users = await get_participants(client, call)

        members = 0
        muted = 0
        allowed = 0
        unchecked = 0

        for participant in participants:
            peer = getattr(participant, "peer", None)

            if not isinstance(peer, types.PeerUser):
                continue

            user_entity = users.get(peer.user_id)

            if user_entity is None:
                try:
                    user_entity = await client.get_entity(peer.user_id)
                except Exception:
                    unchecked += 1
                    continue

            try:
                member = await is_target_member(
                    client,
                    target_entity,
                    user_entity,
                )
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
            "🎙 <b>LIVE STATUS</b>\n\n"
            f"🎯 Target Channel: {target_row['title']}\n"
            f"📺 Live Channel: {live_row['title']}\n\n"
            f"👥 শনাক্ত Target Members: {members}\n"
            f"🔇 Muted: {muted}\n"
            f"🎤 Unmuted: {allowed}\n"
            f"👤অন্য সদস্য: {unchecked}\n\n"
            "🟢 Monitor: ON\n"
            "",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )

    except FloodWaitError as exc:
        await message.answer(
            f"⏳ Telegram rate limit দিয়েছে। "
            f"{exc.seconds} সেকেন্ড পরে চেষ্টা করো।"
        )

    except Exception:
        logger.exception("Status check failed for user %s", user_id)

        await message.answer(
            "❌ Status পাওয়া যায়নি। Session, channel access এবং "
            "Live call চালু আছে কি না যাচাই করো।"
        )


# =========================================================
# FLASK WEB SERVER & KEEP-ALIVE (FOR RENDER & UPTIMEROBOT)
# =========================================================

web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "Telegram Auto Speaker & SMM Engine: ACTIVE 24/7"

def ping_self():
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "https://telegram-auto-speaker-1.onrender.com")
    while True:
        try:
            time.sleep(280) # Ping every 4 mins 50 secs
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
