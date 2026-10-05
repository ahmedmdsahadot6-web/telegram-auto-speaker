import os
import time
import asyncio
import logging
import sqlite3
from contextlib import suppress

from dotenv import load_dotenv
from aiohttp import web

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
from telethon.errors import (
    RPCError,
    SessionPasswordNeededError,
)

# =========================================================
# ENVIRONMENT
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()

try:
    API_ID = int(os.getenv("API_ID", "0"))
except ValueError:
    API_ID = 0

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")

if not API_ID:
    raise RuntimeError("API_ID missing")

if not API_HASH:
    raise RuntimeError("API_HASH missing")


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("AutoSpeaker")


# =========================================================
# DATABASE
# =========================================================

DB_FILE = "speaker_manager.db"

db = sqlite3.connect(
    DB_FILE,
    check_same_thread=False,
)

db.row_factory = sqlite3.Row


def init_db():
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            session_name TEXT,
            connected INTEGER DEFAULT 0,
            created_at INTEGER
        )
        """
    )

    db.execute(
        """
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
        """
    )

    db.commit()


init_db()


# =========================================================
# DIRECTORIES
# =========================================================

os.makedirs(
    "sessions",
    exist_ok=True,
)


# =========================================================
# BOT
# =========================================================

bot = Bot(
    BOT_TOKEN
)

dp = Dispatcher()
router = Router()

dp.include_router(router)


# =========================================================
# TELETHON CLIENTS
# =========================================================

clients = {}

# Pending QR/deep-link login objects.
# These exist only in memory.
login_tasks = {}


def session_path(user_id: int) -> str:
    return os.path.join(
        "sessions",
        f"user_{user_id}",
    )


def get_client(user_id: int) -> TelegramClient:

    if user_id not in clients:

        clients[user_id] = TelegramClient(
            session_path(user_id),
            API_ID,
            API_HASH,
        )

    return clients[user_id]


# =========================================================
# FSM STATES
# =========================================================

class TargetChannelState(StatesGroup):
    waiting_channel = State()


class LiveChannelState(StatesGroup):
    waiting_channel = State()


# =========================================================
# KEYBOARDS
# =========================================================

def main_keyboard():

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="🔐 Connect Telegram"
                ),
                KeyboardButton(
                    text="🎯 Target Channel"
                ),
            ],
            [
                KeyboardButton(
                    text="📺 Live Stream Channel"
                ),
                KeyboardButton(
                    text="📊 Live Status"
                ),
            ],
            [
                KeyboardButton(
                    text="ℹ️ Help"
                ),
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
                    text="ℹ️ Help",
                    callback_data="help",
                )
            ],
        ]
    )


# =========================================================
# START
# =========================================================

@router.message(CommandStart())
async def start_handler(message: Message):

    await message.answer(
        "🎙 <b>TELEGRAM AUTO SPEAKER</b>\n\n"
        "Target Channel-এর সদস্যরা "
        "Live Stream Channel-এর Live-এ "
        "join করলে তাদের speaking permission "
        "দেওয়ার চেষ্টা করবে।\n\n"
        "প্রথমে নিচের থেকে "
        "🔐 Connect Telegram করুন।",
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
        "ℹ️ <b>কিভাবে কাজ করবে</b>\n\n"

        "1️⃣ 🔐 Connect Telegram করুন\n\n"

        "2️⃣ 🎯 Target Channel সেট করুন\n"
        "এই Channel-এর সদস্যদের শনাক্ত করা হবে।\n\n"

        "3️⃣ 📺 Live Stream Channel সেট করুন\n"
        "এই Channel-এ Telegram Live চলবে।\n\n"

        "4️⃣ Live শুরু করুন\n\n"

        "5️⃣ Target Channel-এর কোনো সদস্য "
        "Live-এ join করলে bot তাকে শনাক্ত করবে।\n\n"

        "6️⃣ প্রয়োজনীয় permission থাকলে "
        "speaking permission দেওয়ার চেষ্টা করবে।\n\n"

        "⚠️ Connected Telegram account-এর "
        "Live management permission প্রয়োজন।\n\n"

        "⚠️ RTMP livestream হলে Telegram API "
        "দিয়ে participant unmute করা নাও যেতে পারে।",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


@router.message(F.text == "ℹ️ Help")
async def help_text_handler(message: Message):

    await send_help(message)


@router.callback_query(F.data == "help")
async def help_callback(call: CallbackQuery):

    await call.answer()

    await send_help(
        call.message
    )


# =========================================================
# CONNECTION DATABASE HELPERS
# =========================================================

def set_user_connected(user_id: int, connected: bool):

    db.execute(
        """
        INSERT INTO users
        (
            user_id,
            session_name,
            connected,
            created_at
        )
        VALUES (?, ?, ?, ?)

        ON CONFLICT(user_id)
        DO UPDATE SET
            session_name=excluded.session_name,
            connected=excluded.connected,
            created_at=excluded.created_at
        """,
        (
            user_id,
            session_path(user_id),
            1 if connected else 0,
            int(time.time()),
        ),
    )

    db.commit()


def is_connected_user(user_id: int) -> bool:

    row = db.execute(
        """
        SELECT connected
        FROM users
        WHERE user_id=?
        """,
        (user_id,),
    ).fetchone()

    return bool(
        row and row["connected"]
    )


# =========================================================
# CONNECT TELEGRAM
# =========================================================

@router.message(F.text == "🔐 Connect Telegram")
async def connect_text_handler(
    message: Message
):

    await connect_telegram(
        message
    )


@router.callback_query(F.data == "connect")
async def connect_callback(
    call: CallbackQuery
):

    await call.answer()

    await connect_telegram(
        call.message
    )


async def connect_telegram(message: Message):

    user_id = message.from_user.id

    client = get_client(
        user_id
    )

    try:

        await client.connect()

        # -------------------------------------------------
        # ALREADY AUTHORIZED
        # -------------------------------------------------

        if await client.is_user_authorized():

            set_user_connected(
                user_id,
                True,
            )

            await message.answer(
                "✅ <b>Telegram Already Connected</b>\n\n"
                "এখন 🎯 Target Channel এবং "
                "📺 Live Stream Channel সেট করুন।",
                parse_mode="HTML",
                reply_markup=main_keyboard(),
            )

            return

        # -------------------------------------------------
        # CREATE TELEGRAM QR LOGIN
        # -------------------------------------------------

        login = await client.qr_login()

        login_tasks[user_id] = login

        # -------------------------------------------------
        # MOBILE FRIENDLY LOGIN
        # -------------------------------------------------

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📱 Connect via Telegram",
                        url=login.url,
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🔄 Check Connection",
                        callback_data="check_connection",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Cancel",
                        callback_data="cancel_login",
                    )
                ],
            ]
        )

        await message.answer(
            "🔐 <b>Connect Telegram</b>\n\n"

            "এই ফোন থেকেই connect করতে চাইলে "
            "নিচের <b>📱 Connect via Telegram</b> "
            "button চাপুন।\n\n"

            "Telegram app খুললে login/confirmation "
            "সম্পন্ন করুন।\n\n"

            "অন্য কোনো device থাকলে Telegram-এর "
            "QR scanner দিয়েও এই login ব্যবহার করা যায়।\n\n"

            "⚠️ এই bot আপনার Telegram OTP বা "
            "2FA password সংরক্ষণ করে না।",
            parse_mode="HTML",
            reply_markup=keyboard,
        )

        # -------------------------------------------------
        # WAIT IN BACKGROUND
        # -------------------------------------------------

        asyncio.create_task(
            wait_for_login(
                user_id
            )
        )

    except Exception as e:

        logger.exception(
            "Connect error"
        )

        await message.answer(
            "❌ Telegram connect করা যায়নি।\n\n"
            f"{e}",
            reply_markup=main_keyboard(),
        )


# =========================================================
# WAIT FOR TELEGRAM LOGIN
# =========================================================

async def wait_for_login(user_id: int):

    login = login_tasks.get(
        user_id
    )

    if not login:
        return

    client = get_client(
        user_id
    )

    try:

        await login.wait(
            timeout=180
        )

        if await client.is_user_authorized():

            set_user_connected(
                user_id,
                True,
            )

            await bot.send_message(
                user_id,
                "✅ <b>Telegram Connected!</b>\n\n"
                "এখন 🎯 Target Channel সেট করুন।",
                parse_mode="HTML",
                reply_markup=main_keyboard(),
            )

        else:

            await bot.send_message(
                user_id,
                "❌ Telegram authentication সম্পন্ন হয়নি।",
                reply_markup=main_keyboard(),
            )

    except asyncio.TimeoutError:

        await bot.send_message(
            user_id,
            "⏱ Login session-এর সময় শেষ হয়ে গেছে।\n\n"
            "আবার 🔐 Connect Telegram চাপুন।",
            reply_markup=main_keyboard(),
        )

    except SessionPasswordNeededError:

        await bot.send_message(
            user_id,
            "⚠️ এই Telegram account-এ 2-Step "
            "Verification চালু আছে।\n\n"
            "এই bot-এর মাধ্যমে 2FA password "
            "সংগ্রহ করা হয় না।\n\n"
            "Telegram-এর official authentication "
            "flow ব্যবহার করে login সম্পন্ন করুন।",
            reply_markup=main_keyboard(),
        )

    except Exception as e:

        logger.exception(
            "Telegram login failed"
        )

        with suppress(Exception):

            await bot.send_message(
                user_id,
                "❌ Telegram login failed.\n\n"
                f"{e}",
                reply_markup=main_keyboard(),
            )

    finally:

        login_tasks.pop(
            user_id,
            None,
        )


# =========================================================
# CHECK CONNECTION
# =========================================================

@router.callback_query(
    F.data == "check_connection"
)
async def check_connection(
    call: CallbackQuery
):

    await call.answer()

    user_id = call.from_user.id

    client = get_client(
        user_id
    )

    try:

        await client.connect()

        if await client.is_user_authorized():

            set_user_connected(
                user_id,
                True,
            )

            login_tasks.pop(
                user_id,
                None,
            )

            await call.message.answer(
                "✅ <b>Telegram Connected!</b>\n\n"
                "এখন Target এবং Live Channel সেট করুন।",
                parse_mode="HTML",
                reply_markup=main_keyboard(),
            )

        else:

            await call.message.answer(
                "⏳ এখনও Connected হয়নি।\n\n"
                "আগে Telegram authentication সম্পন্ন করুন।"
            )

    except Exception as e:

        await call.message.answer(
            "❌ Connection check failed:\n\n"
            f"{e}"
        )


# =========================================================
# CANCEL LOGIN
# =========================================================

@router.callback_query(
    F.data == "cancel_login"
)
async def cancel_login(
    call: CallbackQuery
):

    await call.answer()

    user_id = call.from_user.id

    login_tasks.pop(
        user_id,
        None,
    )

    await call.message.answer(
        "❌ Login cancelled.",
        reply_markup=main_keyboard(),
    )


# =========================================================
# TARGET CHANNEL
# =========================================================

@router.message(
    F.text == "🎯 Target Channel"
)
async def target_text_handler(
    message: Message,
    state: FSMContext,
):

    await target_start(
        message,
        state,
    )


@router.callback_query(
    F.data == "target"
)
async def target_callback(
    call: CallbackQuery,
    state: FSMContext,
):

    await call.answer()

    await target_start(
        call.message,
        state,
    )


async def target_start(
    message,
    state: FSMContext,
):

    user_id = message.from_user.id

    if not is_connected_user(
        user_id
    ):

        await message.answer(
            "❌ আগে 🔐 Connect Telegram করুন।"
        )

        return

    old = db.execute(
        """
        SELECT *
        FROM channels
        WHERE owner_id=?
        AND channel_type='target'
        AND active=1
        """,
        (user_id,),
    ).fetchone()

    if old:

        await message.answer(
            "🎯 <b>Target Channel</b>\n\n"
            f"📺 {old['title']}\n"
            f"🔗 {old['channel_username']}\n\n"
            "পরিবর্তন করতে নতুন @username পাঠান।",
            parse_mode="HTML",
        )

    else:

        await message.answer(
            "🎯 <b>Target Channel</b>\n\n"
            "যে Channel-এর সদস্যদের "
            "Live-এ শনাক্ত করতে চান "
            "সেই Channel-এর username পাঠান।\n\n"
            "উদাহরণ:\n"
            "<code>@MyTargetChannel</code>",
            parse_mode="HTML",
        )

    await state.set_state(
        TargetChannelState.waiting_channel
    )


@router.message(
    TargetChannelState.waiting_channel
)
async def save_target_channel(
    message: Message,
    state: FSMContext,
):

    await save_channel(
        message,
        state,
        "target",
    )


# =========================================================
# LIVE CHANNEL
# =========================================================

@router.message(
    F.text == "📺 Live Stream Channel"
)
async def live_text_handler(
    message: Message,
    state: FSMContext,
):

    await live_start(
        message,
        state,
    )


@router.callback_query(
    F.data == "live"
)
async def live_callback(
    call: CallbackQuery,
    state: FSMContext,
):

    await call.answer()

    await live_start(
        call.message,
        state,
    )


async def live_start(
    message,
    state: FSMContext,
):

    user_id = message.from_user.id

    if not is_connected_user(
        user_id
    ):

        await message.answer(
            "❌ আগে 🔐 Connect Telegram করুন।"
        )

        return

    old = db.execute(
        """
        SELECT *
        FROM channels
        WHERE owner_id=?
        AND channel_type='live'
        AND active=1
        """,
        (user_id,),
    ).fetchone()

    if old:

        await message.answer(
            "📺 <b>Live Stream Channel</b>\n\n"
            f"📺 {old['title']}\n"
            f"🔗 {old['channel_username']}\n\n"
            "পরিবর্তন করতে নতুন @username পাঠান।",
            parse_mode="HTML",
        )

    else:

        await message.answer(
            "📺 <b>Live Stream Channel</b>\n\n"
            "যে Channel-এ Telegram Live চলবে "
            "সেই Channel-এর username পাঠান।\n\n"
            "উদাহরণ:\n"
            "<code>@MyLiveChannel</code>",
            parse_mode="HTML",
        )

    await state.set_state(
        LiveChannelState.waiting_channel
    )


@router.message(
    LiveChannelState.waiting_channel
)
async def save_live_channel(
    message: Message,
    state: FSMContext,
):

    await save_channel(
        message,
        state,
        "live",
    )


# =========================================================
# SAVE CHANNEL
# =========================================================

async def save_channel(
    message,
    state: FSMContext,
    channel_type: str,
):

    username = (
        message.text or ""
    ).strip()

    if not username:

        await message.answer(
            "❌ Channel username দিন।"
        )

        return

    if not username.startswith("@"):

        username = "@" + username

    user_id = message.from_user.id

    if not is_connected_user(
        user_id
    ):

        await message.answer(
            "❌ Telegram account connected নেই।"
        )

        await state.clear()

        return

    client = get_client(
        user_id
    )

    try:

        await client.connect()

        if not await client.is_user_authorized():

            set_user_connected(
                user_id,
                False,
            )

            await message.answer(
                "❌ Telegram account connected নেই।"
            )

            await state.clear()

            return

        entity = await client.get_entity(
            username
        )

        # -------------------------------------------------
        # VERIFY CHANNEL
        # -------------------------------------------------

        if not isinstance(
            entity,
            types.Channel,
        ):

            await message.answer(
                "❌ এটি একটি valid Telegram Channel নয়।"
            )

            await state.clear()

            return

        await client(
            functions.channels.GetFullChannelRequest(
                channel=entity
            )
        )

        title = getattr(
            entity,
            "title",
            username,
        )

        # -------------------------------------------------
        # SAVE
        # -------------------------------------------------

        db.execute(
            """
            INSERT INTO channels
            (
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

            ON CONFLICT(owner_id, channel_type)
            DO UPDATE SET
                channel_username=excluded.channel_username,
                channel_id=excluded.channel_id,
                title=excluded.title,
                active=1,
                auto_allow=1,
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

            await message.answer(
                "✅ <b>Target Channel Added</b>\n\n"
                f"📺 {title}\n"
                f"🔗 {username}\n\n"
                "এখন 📺 Live Stream Channel সেট করুন।",
                parse_mode="HTML",
                reply_markup=main_keyboard(),
            )

        else:

            await message.answer(
                "✅ <b>Live Stream Channel Added</b>\n\n"
                f"📺 {title}\n"
                f"🔗 {username}\n\n"
                "🎙 Live monitor করার জন্য channel সেট হয়েছে।",
                parse_mode="HTML",
                reply_markup=main_keyboard(),
            )

    except Exception as e:

        logger.exception(
            "Channel save failed"
        )

        await message.answer(
            "❌ Channel add করা যায়নি।\n\n"
            f"{e}"
        )

        await state.clear()


# =========================================================
# GET CHANNEL
# =========================================================

def get_channel(
    owner_id: int,
    channel_type: str,
):

    return db.execute(
        """
        SELECT *
        FROM channels
        WHERE owner_id=?
        AND channel_type=?
        AND active=1
        """,
        (
            owner_id,
            channel_type,
        ),
    ).fetchone()


# =========================================================
# ACTIVE GROUP CALL
# =========================================================

async def get_active_call(
    client: TelegramClient,
    channel_username: str,
):

    entity = await client.get_entity(
        channel_username
    )

    full = await client(
        functions.channels.GetFullChannelRequest(
            channel=entity
        )
    )

    call = getattr(
        full.full_chat,
        "call",
        None,
    )

    return call, entity


# =========================================================
# PARTICIPANTS
# =========================================================

async def get_participants(
    client: TelegramClient,
    call,
):

    input_call = types.InputGroupCall(
        id=call.id,
        access_hash=call.access_hash,
    )

    result = await client(
        functions.phone.GetGroupCallRequest(
            call=input_call,
            limit=100,
            offset=0,
        )
    )

    users = {
        user.id: user
        for user in result.users
    }

    return result.participants, users


# =========================================================
# TARGET MEMBER CHECK
# =========================================================

async def is_target_member(
    client: TelegramClient,
    target_entity,
    user,
):

    try:

        if not getattr(
            user,
            "access_hash",
            None,
        ):
            return False

        participant = await client(
            functions.channels.GetParticipantRequest(
                channel=target_entity,
                participant=types.InputPeerUser(
                    user_id=user.id,
                    access_hash=user.access_hash,
                ),
            )
        )

        member = participant.participant

        if isinstance(
            member,
            (
                types.ChannelParticipantLeft,
                types.ChannelParticipantBanned,
            ),
        ):
            return False

        return True

    except Exception as e:

        logger.warning(
            "Membership check failed for %s: %s",
            getattr(user, "id", "unknown"),
            e,
        )

        return False


# =========================================================
# ALLOW PARTICIPANT
# =========================================================

async def allow_participant(
    client: TelegramClient,
    call,
    user,
):

    input_call = types.InputGroupCall(
        id=call.id,
        access_hash=call.access_hash,
    )

    participant = await client.get_input_entity(
        user
    )

    await client(
        functions.phone.EditGroupCallParticipantRequest(
            call=input_call,
            participant=participant,
            muted=False,
        )
    )


# =========================================================
# MONITOR ONE USER
# =========================================================

async def monitor_user(
    user_id: int,
):

    target_row = get_channel(
        user_id,
        "target",
    )

    live_row = get_channel(
        user_id,
        "live",
    )

    if not target_row or not live_row:
        return

    client = get_client(
        user_id
    )

    try:

        if not client.is_connected():

            await client.connect()

        if not await client.is_user_authorized():

            set_user_connected(
                user_id,
                False,
            )

            return

        # -------------------------------------------------
        # LIVE CALL
        # -------------------------------------------------

        call, live_entity = await get_active_call(
            client,
            live_row["channel_username"],
        )

        if not call:
            return

        # -------------------------------------------------
        # TARGET
        # -------------------------------------------------

        target_entity = await client.get_entity(
            target_row["channel_username"]
        )

        # -------------------------------------------------
        # PARTICIPANTS
        # -------------------------------------------------

        participants, users = await get_participants(
            client,
            call,
        )

        for participant in participants:

            peer = participant.peer

            if not isinstance(
                peer,
                types.PeerUser,
            ):
                continue

            user = users.get(
                peer.user_id
            )

            if not user:
                continue

            # -------------------------------------------------
            # CHECK MEMBER
            # -------------------------------------------------

            member = await is_target_member(
                client,
                target_entity,
                user,
            )

            if not member:
                continue

            # -------------------------------------------------
            # ONLY MUTED PARTICIPANTS
            # -------------------------------------------------

            muted = getattr(
                participant,
                "muted",
                False,
            )

            if not muted:
                continue

            # -------------------------------------------------
            # ALLOW
            # -------------------------------------------------

            try:

                await allow_participant(
                    client,
                    call,
                    user,
                )

                logger.info(
                    "Target member allowed | "
                    "user=%s | target=%s | live=%s",
                    user.id,
                    target_row["channel_username"],
                    live_row["channel_username"],
                )

            except RPCError as e:

                logger.warning(
                    "Telegram API rejected participant %s: %s",
                    user.id,
                    e,
                )

            except Exception as e:

                logger.warning(
                    "Allow participant failed: %s",
                    e,
                )

            await asyncio.sleep(
                0.15
            )

    except RPCError as e:

        logger.warning(
            "Monitor RPC error user=%s: %s",
            user_id,
            e,
        )

    except Exception as e:

        logger.warning(
            "Monitor error user=%s: %s",
            user_id,
            e,
        )


# =========================================================
# GLOBAL MONITOR
# =========================================================

async def auto_monitor():

    logger.info(
        "Auto Speaker Monitor Started"
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

                try:

                    await monitor_user(
                        row["owner_id"]
                    )

                except Exception as e:

                    logger.warning(
                        "User monitor error: %s",
                        e,
                    )

                await asyncio.sleep(
                    0.5
                )

        except Exception as e:

            logger.exception(
                "Global monitor error"
            )

        await asyncio.sleep(
            3
        )


# =========================================================
# LIVE STATUS
# =========================================================

@router.message(
    F.text == "📊 Live Status"
)
async def status_text_handler(
    message: Message
):

    await live_status_message(
        message
    )


@router.callback_query(
    F.data == "status"
)
async def status_callback(
    call: CallbackQuery
):

    await call.answer()

    await live_status_message(
        call.message
    )


async def live_status_message(
    message: Message
):

    user_id = message.from_user.id

    target_row = get_channel(
        user_id,
        "target",
    )

    live_row = get_channel(
        user_id,
        "live",
    )

    if not target_row:

        await message.answer(
            "❌ 🎯 Target Channel সেট করা হয়নি।"
        )

        return

    if not live_row:

        await message.answer(
            "❌ 📺 Live Stream Channel সেট করা হয়নি।"
        )

        return

    client = get_client(
        user_id
    )

    try:

        await client.connect()

        if not await client.is_user_authorized():

            await message.answer(
                "❌ Telegram account connected নেই।"
            )

            return

        call, live_entity = await get_active_call(
            client,
            live_row["channel_username"],
        )

        if not call:

            await message.answer(
                "🔴 <b>LIVE OFFLINE</b>\n\n"
                f"🎯 Target: {target_row['title']}\n"
                f"📺 Live: {live_row['title']}",
                parse_mode="HTML",
            )

            return

        participants, users = await get_participants(
            client,
            call,
        )

        target_entity = await client.get_entity(
            target_row["channel_username"]
        )

        members = 0
        muted = 0
        allowed = 0

        for participant in participants:

            peer = participant.peer

            if not isinstance(
                peer,
                types.PeerUser,
            ):
                continue

            user = users.get(
                peer.user_id
            )

            if not user:
                continue

            if await is_target_member(
                client,
                target_entity,
                user,
            ):

                members += 1

                if getattr(
                    participant,
                    "muted",
                    False,
                ):
                    muted += 1
                else:
                    allowed += 1

        await message.answer(
            "🎙 <b>LIVE STATUS</b>\n\n"

            f"🎯 Target Channel:\n"
            f"{target_row['title']}\n\n"

            f"📺 Live Channel:\n"
            f"{live_row['title']}\n\n"

            f"👥 Target Members in Live: {members}\n"
            f"🔇 Muted: {muted}\n"
            f"🎤 Allowed: {allowed}\n\n"

            "🟢 Monitor: ON",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )

    except Exception as e:

        logger.exception(
            "Status error"
        )

        await message.answer(
            f"❌ Status Error:\n\n{e}"
        )


# =========================================================
# BACK
# =========================================================

@router.callback_query(
    F.data == "back"
)
async def back_handler(
    call: CallbackQuery
):

    await call.answer()

    await call.message.answer(
        "🎙 <b>TELEGRAM AUTO SPEAKER</b>\n\n"
        "Control Panel:",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


# =========================================================
# HEALTH SERVER
# =========================================================

async def health_handler(
    request
):

    return web.json_response(
        {
            "status": "online",
            "service": "Telegram Auto Speaker",
            "time": int(time.time()),
        }
    )


async def start_health_server():

    app = web.Application()

    app.router.add_get(
        "/",
        health_handler,
    )

    app.router.add_get(
        "/health",
        health_handler,
    )

    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )

    runner = web.AppRunner(
        app
    )

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port,
    )

    await site.start()

    logger.info(
        "Health server running on port %s",
        port,
    )


# =========================================================
# SHUTDOWN
# =========================================================

async def shutdown():

    logger.info(
        "Shutting down..."
    )

    for user_id, client in list(
        clients.items()
    ):

        try:

            if client.is_connected():

                await client.disconnect()

        except Exception:

            pass

    try:

        await bot.session.close()

    except Exception:

        pass


# =========================================================
# MAIN
# =========================================================

async def main():

    logger.info(
        "Telegram Auto Speaker Starting..."
    )

    await start_health_server()

    monitor_task = asyncio.create_task(
        auto_monitor()
    )

    try:

        await dp.start_polling(
            bot
        )

    finally:

        monitor_task.cancel()

        with suppress(
            asyncio.CancelledError
        ):

            await monitor_task

        await shutdown()


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "Bot stopped"
        )
