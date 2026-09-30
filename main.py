import os
import io
import time
import asyncio
import logging
import sqlite3

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from telethon import TelegramClient, functions, types
from telethon.errors import RPCError, SessionPasswordNeededError

import qrcode

from aiohttp import web


# =========================================================
# ENVIRONMENT
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ID_RAW = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")

if not API_ID_RAW:
    raise RuntimeError("API_ID missing")

if not API_HASH:
    raise RuntimeError("API_HASH missing")

try:
    API_ID = int(API_ID_RAW)
except ValueError:
    raise RuntimeError("API_ID must be a number")


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("AutoSpeaker")


# =========================================================
# DATABASE
# =========================================================

DB_FILE = "speaker_manager.db"

db = sqlite3.connect(
    DB_FILE,
    check_same_thread=False
)

db.row_factory = sqlite3.Row


def init_db():

    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            session_name TEXT,
            connected INTEGER DEFAULT 0,
            created_at INTEGER
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            channel_username TEXT NOT NULL,
            channel_id INTEGER,
            title TEXT,
            active INTEGER DEFAULT 1,
            auto_allow INTEGER DEFAULT 1,
            created_at INTEGER,
            UNIQUE(owner_id, channel_username)
        )
    """)

    db.commit()


init_db()


# =========================================================
# SESSION DIRECTORY
# =========================================================

os.makedirs(
    "sessions",
    exist_ok=True
)


# =========================================================
# BOT
# =========================================================

bot = Bot(
    token=BOT_TOKEN
)

dp = Dispatcher()

router = Router()

dp.include_router(router)


# =========================================================
# TELETHON CLIENTS
# =========================================================

clients = {}

login_tasks = {}


def session_path(user_id):

    return os.path.join(
        "sessions",
        f"user_{user_id}"
    )


def get_client(user_id):

    if user_id not in clients:

        clients[user_id] = TelegramClient(
            session_path(user_id),
            API_ID,
            API_HASH
        )

    return clients[user_id]


# =========================================================
# STATES
# =========================================================

class ChannelState(StatesGroup):

    waiting_channel = State()


# =========================================================
# REPLY KEYBOARD
# =========================================================

def main_keyboard():

    return ReplyKeyboardMarkup(
        keyboard=[

            [
                KeyboardButton(
                    text="🔐 Connect Telegram"
                ),
                KeyboardButton(
                    text="➕ Add Channel"
                )
            ],

            [
                KeyboardButton(
                    text="📺 My Channels"
                ),
                KeyboardButton(
                    text="ℹ️ Help"
                )
            ]

        ],
        resize_keyboard=True,
        is_persistent=True
    )


# =========================================================
# INLINE MAIN MENU
# =========================================================

def main_menu():

    return InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="🔐 Connect Telegram",
                    callback_data="connect"
                )
            ],

            [
                InlineKeyboardButton(
                    text="➕ Add Channel",
                    callback_data="add_channel"
                )
            ],

            [
                InlineKeyboardButton(
                    text="📺 My Channels",
                    callback_data="my_channels"
                )
            ],

            [
                InlineKeyboardButton(
                    text="ℹ️ Help",
                    callback_data="help"
                )
            ]

        ]
    )


# =========================================================
# START
# =========================================================

@router.message(CommandStart())
async def start_handler(message: Message):

    await message.answer(
        "🎙 <b>TELEGRAM AUTO SPEAKER</b>\n\n"
        "Channel Live-এ কোনো member join করলে "
        "তার speaking permission দেওয়ার চেষ্টা করবে "
        "এই system।\n\n"
        "প্রথমে নিচের:\n"
        "🔐 <b>Connect Telegram</b>\n"
        "চাপুন।",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


# =========================================================
# REPLY KEYBOARD HANDLERS
# =========================================================

@router.message(F.text == "🔐 Connect Telegram")
async def connect_button_handler(message: Message):

    await start_telegram_connection(
        message.from_user.id,
        message
    )


@router.message(F.text == "➕ Add Channel")
async def add_channel_button_handler(
    message: Message,
    state: FSMContext
):

    await start_add_channel(
        message.from_user.id,
        message,
        state
    )


@router.message(F.text == "📺 My Channels")
async def my_channels_button_handler(
    message: Message
):

    await show_my_channels_message(
        message
    )


@router.message(F.text == "ℹ️ Help")
async def help_button_handler(
    message: Message
):

    await send_help_message(
        message
    )


# =========================================================
# HELP
# =========================================================

async def send_help_message(message: Message):

    await message.answer(
        "ℹ️ <b>কিভাবে কাজ করবে</b>\n\n"
        "1️⃣ 🔐 Connect Telegram চাপুন\n"
        "2️⃣ QR Code Scan করুন\n"
        "3️⃣ ➕ Add Channel চাপুন\n"
        "4️⃣ নিজের Channel username দিন\n"
        "5️⃣ Channel-এ Live চালু করুন\n"
        "6️⃣ কোনো Channel member Live-এ join করলে\n"
        "7️⃣ Bot তাকে শনাক্ত করবে\n"
        "8️⃣ Muted থাকলে speaking permission দেওয়ার "
        "চেষ্টা করবে\n\n"
        "⚠️ Bot শুধু Live-এ join করা participant-দের "
        "নিয়ে কাজ করবে।\n\n"
        "⚠️ Channel-এর উপর প্রয়োজনীয় admin permission "
        "থাকতে হবে।",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


@router.callback_query(F.data == "help")
async def help_callback(call: CallbackQuery):

    await call.answer()

    await send_help_message(
        call.message
    )


# =========================================================
# TELEGRAM CONNECT
# =========================================================

async def start_telegram_connection(
    user_id,
    message
):

    client = get_client(user_id)

    try:

        await client.connect()

        # Already connected
        if await client.is_user_authorized():

            db.execute(
                """
                INSERT OR REPLACE INTO users
                (user_id, session_name, connected, created_at)
                VALUES (?, ?, 1, ?)
                """,
                (
                    user_id,
                    session_path(user_id),
                    int(time.time())
                )
            )

            db.commit()

            await message.answer(
                "✅ <b>Telegram Already Connected</b>\n\n"
                "আপনার Telegram account আগে থেকেই connected আছে।\n\n"
                "এখন ➕ Add Channel চাপুন।",
                parse_mode="HTML",
                reply_markup=main_keyboard()
            )

            return

        # Create QR login
        login = await client.qr_login()

        login_tasks[user_id] = login

        # Generate QR image
        qr_image = qrcode.make(
            login.url
        )

        buffer = io.BytesIO()

        qr_image.save(
            buffer,
            format="PNG"
        )

        buffer.seek(0)

        photo = BufferedInputFile(
            buffer.read(),
            filename="telegram_login_qr.png"
        )

        # Send QR
        await message.answer_photo(
            photo=photo,
            caption=(
                "🔐 <b>Connect Telegram</b>\n\n"
                "📱 Telegram App খুলুন\n\n"
                "➡️ Settings\n"
                "➡️ Devices\n"
                "➡️ Link Desktop Device\n\n"
                "📷 তারপর উপরের QR Code Scan করুন।\n\n"
                "🔗 <b>QR Scan না হলে Login Link:</b>\n\n"
                f"<code>{login.url}</code>\n\n"
                "⚠️ OTP বা 2FA password এই bot-এ পাঠাবেন না।"
            ),
            parse_mode="HTML"
        )

        await message.answer(
            "⏳ QR Scan করার অপেক্ষায় আছি...\n\n"
            "QR Scan সফল হলে automatically connected হয়ে যাবে।"
        )

        asyncio.create_task(
            wait_for_qr_login(
                user_id
            )
        )

    except Exception as e:

        logger.exception(
            "Telegram connect error"
        )

        await message.answer(
            "❌ <b>Telegram connect করা যায়নি</b>\n\n"
            f"<code>{str(e)}</code>",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )


# =========================================================
# INLINE CONNECT BUTTON
# =========================================================

@router.callback_query(F.data == "connect")
async def connect_callback(
    call: CallbackQuery
):

    await call.answer()

    await start_telegram_connection(
        call.from_user.id,
        call.message
    )


# =========================================================
# WAIT QR LOGIN
# =========================================================

async def wait_for_qr_login(
    user_id
):

    try:

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

        except SessionPasswordNeededError:

            await bot.send_message(
                user_id,
                "⚠️ <b>2-Step Verification চালু আছে</b>\n\n"
                "QR login-এর পরে Telegram আপনার 2FA "
                "password চাইতে পারে।\n\n"
                "Password এই bot-এ পাঠাবেন না।",
                parse_mode="HTML"
            )

            return

        db.execute(
            """
            INSERT OR REPLACE INTO users
            (user_id, session_name, connected, created_at)
            VALUES (?, ?, 1, ?)
            """,
            (
                user_id,
                session_path(user_id),
                1,
                int(time.time())
            )
        )

        db.commit()

        await bot.send_message(
            user_id,
            "✅ <b>Telegram Connected Successfully!</b>\n\n"
            "এখন ➕ Add Channel চাপুন।",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )

    except asyncio.TimeoutError:

        await bot.send_message(
            user_id,
            "⌛ QR Code-এর সময় শেষ হয়ে গেছে।\n\n"
            "আবার 🔐 Connect Telegram চাপুন।",
            reply_markup=main_keyboard()
        )

    except Exception as e:

        logger.exception(
            "QR login error"
        )

        try:

            await bot.send_message(
                user_id,
                "❌ <b>Telegram Login Failed</b>\n\n"
                f"<code>{str(e)}</code>",
                parse_mode="HTML"
            )

        except Exception:
            pass

    finally:

        login_tasks.pop(
            user_id,
            None
        )


# =========================================================
# ADD CHANNEL
# =========================================================

async def start_add_channel(
    user_id,
    message,
    state
):

    row = db.execute(
        """
        SELECT connected
        FROM users
        WHERE user_id=?
        """,
        (user_id,)
    ).fetchone()

    if not row or not row["connected"]:

        await message.answer(
            "❌ আগে 🔐 Connect Telegram করুন।",
            reply_markup=main_keyboard()
        )

        return

    await state.set_state(
        ChannelState.waiting_channel
    )

    await message.answer(
        "➕ <b>Add Channel</b>\n\n"
        "আপনার Channel username পাঠান।\n\n"
        "উদাহরণ:\n"
        "<code>@MyChannel</code>\n\n"
        "অথবা:\n"
        "<code>MyChannel</code>",
        parse_mode="HTML"
    )


# =========================================================
# INLINE ADD CHANNEL
# =========================================================

@router.callback_query(F.data == "add_channel")
async def add_channel_callback(
    call: CallbackQuery,
    state: FSMContext
):

    await call.answer()

    await start_add_channel(
        call.from_user.id,
        call.message,
        state
    )


# =========================================================
# SAVE CHANNEL
# =========================================================

@router.message(ChannelState.waiting_channel)
async def save_channel(
    message: Message,
    state: FSMContext
):

    if not message.text:

        await message.answer(
            "❌ শুধু Channel username পাঠান।\n\n"
            "উদাহরণ: @MyChannel"
        )

        return

    username = message.text.strip()

    if not username.startswith("@"):

        username = "@" + username

    user_id = message.from_user.id

    client = get_client(
        user_id
    )

    try:

        await client.connect()

        if not await client.is_user_authorized():

            await message.answer(
                "❌ Telegram account connected নেই।\n\n"
                "আগে 🔐 Connect Telegram করুন।"
            )

            await state.clear()

            return

        entity = await client.get_entity(
            username
        )

        title = getattr(
            entity,
            "title",
            username
        )

        await client(
            functions.channels.GetFullChannelRequest(
                channel=entity
            )
        )

        db.execute(
            """
            INSERT OR REPLACE INTO channels
            (
                owner_id,
                channel_username,
                channel_id,
                title,
                active,
                auto_allow,
                created_at
            )
            VALUES (?, ?, ?, ?, 1, 1, ?)
            """,
            (
                user_id,
                username,
                entity.id,
                title,
                1,
                1,
                int(time.time())
            )
        )

        db.commit()

        await state.clear()

        await message.answer(
            f"✅ <b>Channel Added</b>\n\n"
            f"📺 {title}\n"
            f"🔗 {username}\n\n"
            "🎤 Auto Allow: <b>ON</b>\n\n"
            "এখন Channel-এ Live চালু করলে "
            "monitoring শুরু হবে।",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )

    except Exception as e:

        logger.exception(
            "Channel add error"
        )

        await message.answer(
            "❌ <b>Channel add করা যায়নি</b>\n\n"
            f"<code>{str(e)}</code>",
            parse_mode="HTML"
        )

        await state.clear()


# =========================================================
# MY CHANNELS
# =========================================================

async def show_my_channels_message(
    message: Message
):

    rows = db.execute(
        """
        SELECT id, title, channel_username, auto_allow
        FROM channels
        WHERE owner_id=? AND active=1
        ORDER BY id DESC
        """,
        (
            message.from_user.id,
        )
    ).fetchall()

    if not rows:

        await message.answer(
            "📺 <b>My Channels</b>\n\n"
            "কোনো Channel নেই।\n\n"
            "➕ Add Channel চাপুন।",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )

        return

    text = "📺 <b>My Channels</b>\n\n"

    for row in rows:

        status = (
            "🟢 ON"
            if row["auto_allow"]
            else
            "🔴 OFF"
        )

        text += (
            f"📺 <b>{row['title']}</b>\n"
            f"🔗 {row['channel_username']}\n"
            f"🎤 Auto Allow: {status}\n\n"
        )

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"⚙️ {row['title']}",
                        callback_data=f"channel:{row['id']}"
                    )
                ]
                for row in rows
            ] + [
                [
                    InlineKeyboardButton(
                        text="➕ Add Channel",
                        callback_data="add_channel"
                    )
                ]
            ]
        )
    )


# =========================================================
# INLINE MY CHANNELS
# =========================================================

@router.callback_query(F.data == "my_channels")
async def my_channels_callback(
    call: CallbackQuery
):

    await call.answer()

    await show_my_channels_message(
        call.message
    )


# =========================================================
# CHANNEL MENU
# =========================================================

@router.callback_query(
    F.data.startswith("channel:")
)
async def channel_menu(
    call: CallbackQuery
):

    await call.answer()

    try:

        channel_id = int(
            call.data.split(":")[1]
        )

    except:

        return

    row = db.execute(
        """
        SELECT *
        FROM channels
        WHERE id=? AND owner_id=? AND active=1
        """,
        (
            channel_id,
            call.from_user.id
        )
    ).fetchone()

    if not row:

        await call.message.answer(
            "❌ Channel পাওয়া যায়নি।"
        )

        return

    status = (
        "🟢 ON"
        if row["auto_allow"]
        else
        "🔴 OFF"
    )

    await call.message.edit_text(
        f"📺 <b>{row['title']}</b>\n\n"
        f"🔗 {row['channel_username']}\n"
        f"🎤 Auto Allow: <b>{status}</b>\n\n"
        "Auto Allow ON থাকলে Live participant-এর "
        "মধ্যে Channel member পাওয়া গেলে muted "
        "থাকলে speaking permission দেওয়ার চেষ্টা করবে।",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[

                [
                    InlineKeyboardButton(
                        text="🎤 Toggle Auto Allow",
                        callback_data=f"toggle:{row['id']}"
                    )
                ],

                [
                    InlineKeyboardButton(
                        text="📊 Live Status",
                        callback_data=f"statuslive:{row['id']}"
                    )
                ],

                [
                    InlineKeyboardButton(
                        text="❌ Remove Channel",
                        callback_data=f"remove:{row['id']}"
                    )
                ],

                [
                    InlineKeyboardButton(
                        text="⬅️ Back",
                        callback_data="my_channels"
                    )
                ]

            ]
        )
    )


# =========================================================
# TOGGLE
# =========================================================

@router.callback_query(
    F.data.startswith("toggle:")
)
async def toggle_auto_allow(
    call: CallbackQuery
):

    await call.answer()

    channel_id = int(
        call.data.split(":")[1]
    )

    row = db.execute(
        """
        SELECT auto_allow
        FROM channels
        WHERE id=? AND owner_id=?
        """,
        (
            channel_id,
            call.from_user.id
        )
    ).fetchone()

    if not row:
        return

    new_value = (
        0
        if row["auto_allow"]
        else
        1
    )

    db.execute(
        """
        UPDATE channels
        SET auto_allow=?
        WHERE id=? AND owner_id=?
        """,
        (
            new_value,
            channel_id,
            call.from_user.id
        )
    )

    db.commit()

    await channel_menu(
        call
    )


# =========================================================
# REMOVE CHANNEL
# =========================================================

@router.callback_query(
    F.data.startswith("remove:")
)
async def remove_channel(
    call: CallbackQuery
):

    await call.answer()

    channel_id = int(
        call.data.split(":")[1]
    )

    db.execute(
        """
        UPDATE channels
        SET active=0
        WHERE id=? AND owner_id=?
        """,
        (
            channel_id,
            call.from_user.id
        )
    )

    db.commit()

    await call.message.edit_text(
        "✅ <b>Channel Removed</b>",
        parse_mode="HTML"
    )

    await call.message.answer(
        "Main Menu:",
        reply_markup=main_keyboard()
    )


# =========================================================
# GET ACTIVE GROUP CALL
# =========================================================

async def get_active_call(
    client,
    channel_username
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
        None
    )

    return call, entity


# =========================================================
# GET LIVE PARTICIPANTS
# =========================================================

async def get_participants(
    client,
    call
):

    input_call = types.InputGroupCall(
        id=call.id,
        access_hash=call.access_hash
    )

    result = await client(
        functions.phone.GetGroupCallRequest(
            call=input_call,
            limit=100
        )
    )

    users = {
        user.id: user
        for user in result.users
    }

    return result.participants, users


# =========================================================
# CHECK CHANNEL MEMBER
# =========================================================

async def is_channel_member(
    client,
    channel_entity,
    user
):

    try:

        if not getattr(
            user,
            "access_hash",
            None
        ):
            return False

        result = await client(
            functions.channels.GetParticipantRequest(
                channel=channel_entity,
                participant=types.InputPeerUser(
                    user_id=user.id,
                    access_hash=user.access_hash
                )
            )
        )

        participant = result.participant

        if isinstance(
            participant,
            (
                types.ChannelParticipantLeft,
                types.ChannelParticipantBanned
            )
        ):

            return False

        return True

    except Exception:

        return False


# =========================================================
# ALLOW SPEAKING
# =========================================================

async def allow_participant(
    client,
    call,
    user
):

    input_call = types.InputGroupCall(
        id=call.id,
        access_hash=call.access_hash
    )

    participant = types.InputPeerUser(
        user_id=user.id,
        access_hash=user.access_hash
    )

    await client(
        functions.phone.EditGroupCallParticipantRequest(
            call=input_call,
            participant=participant,
            muted=False
        )
    )


# =========================================================
# MONITOR ONE CHANNEL
# =========================================================

async def monitor_channel(
    row
):

    owner_id = row["owner_id"]

    client = get_client(
        owner_id
    )

    if not client.is_connected():

        await client.connect()

    if not await client.is_user_authorized():

        return

    try:

        call, channel_entity = \
            await get_active_call(
                client,
                row["channel_username"]
            )

        # No Live
        if not call:

            return

        participants, users = \
            await get_participants(
                client,
                call
            )

        for participant in participants:

            peer = getattr(
                participant,
                "peer",
                None
            )

            if not isinstance(
                peer,
                types.PeerUser
            ):

                continue

            user = users.get(
                peer.user_id
            )

            if not user:

                continue

            # Check if participant is channel member
            is_member = await is_channel_member(
                client,
                channel_entity,
                user
            )

            if not is_member:

                continue

            # Check mute status
            muted = getattr(
                participant,
                "muted",
                False
            )

            if muted:

                try:

                    await allow_participant(
                        client,
                        call,
                        user
                    )

                    logger.info(
                        "Auto allowed user %s in %s",
                        user.id,
                        row["channel_username"]
                    )

                except Exception as e:

                    logger.warning(
                        "Allow failed for %s: %s",
                        user.id,
                        e
                    )

    except RPCError as e:

        logger.warning(
            "Telegram RPC error for %s: %s",
            row["channel_username"],
            e
        )

    except Exception as e:

        logger.warning(
            "Monitor error for %s: %s",
            row["channel_username"],
            e
        )


# =========================================================
# AUTO MONITOR
# =========================================================

async def auto_monitor():

    logger.info(
        "Auto Speaker Monitor started"
    )

    while True:

        try:

            rows = db.execute(
                """
                SELECT *
                FROM channels
                WHERE active=1
                AND auto_allow=1
                """
            ).fetchall()

            for row in rows:

                try:

                    await monitor_channel(
                        row
                    )

                except Exception as e:

                    logger.warning(
                        "Channel monitor failed: %s",
                        e
                    )

                await asyncio.sleep(
                    0.5
                )

        except Exception as e:

            logger.exception(
                "Monitor loop error"
            )

        await asyncio.sleep(
            3
        )


# =========================================================
# LIVE STATUS
# =========================================================

@router.callback_query(
    F.data.startswith("statuslive:")
)
async def live_status(
    call: CallbackQuery
):

    await call.answer()

    channel_id = int(
        call.data.split(":")[1]
    )

    row = db.execute(
        """
        SELECT *
        FROM channels
        WHERE id=? AND owner_id=?
        """,
        (
            channel_id,
            call.from_user.id
        )
    ).fetchone()

    if not row:

        return

    client = get_client(
        call.from_user.id
    )

    try:

        await client.connect()

        active_call, channel_entity = \
            await get_active_call(
                client,
                row["channel_username"]
            )

        if not active_call:

            await call.message.edit_text(
                "🔴 <b>এখন কোনো Active Live নেই।</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="⬅️ Back",
                                callback_data=f"channel:{channel_id}"
                            )
                        ]
                    ]
                )
            )

            return

        participants, users = \
            await get_participants(
                client,
                active_call
            )

        members = 0
        allowed = 0
        muted = 0

        for participant in participants:

            peer = getattr(
                participant,
                "peer",
                None
            )

            if not isinstance(
                peer,
                types.PeerUser
            ):

                continue

            user = users.get(
                peer.user_id
            )

            if not user:

                continue

            if await is_channel_member(
                client,
                channel_entity,
                user
            ):

                members += 1

                if getattr(
                    participant,
                    "muted",
                    False
                ):

                    muted += 1

                else:

                    allowed += 1

        await call.message.edit_text(
            "🎙 <b>LIVE STATUS</b>\n\n"
            f"📺 {row['title']}\n\n"
            f"👥 Channel Members: {members}\n"
            f"🔇 Muted: {muted}\n"
            f"🎤 Allowed: {allowed}\n\n"
            f"Auto Allow: "
            f"{'🟢 ON' if row['auto_allow'] else '🔴 OFF'}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[

                    [
                        InlineKeyboardButton(
                            text="🔄 Refresh",
                            callback_data=f"statuslive:{channel_id}"
                        )
                    ],

                    [
                        InlineKeyboardButton(
                            text="⬅️ Back",
                            callback_data=f"channel:{channel_id}"
                        )
                    ]

                ]
            )
        )

    except Exception as e:

        await call.message.answer(
            "❌ <b>Status Error</b>\n\n"
            f"<code>{str(e)}</code>",
            parse_mode="HTML"
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
        "Main Menu:",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


# =========================================================
# HEALTH SERVER FOR RENDER / UPTIMEROBOT
# =========================================================

async def health_handler(request):

    return web.Response(
        text="Telegram Auto Speaker is running ✅",
        status=200
    )


async def start_health_server():

    app = web.Application()

    app.router.add_get(
        "/",
        health_handler
    )

    app.router.add_get(
        "/health",
        health_handler
    )

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    runner = web.AppRunner(
        app
    )

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    logger.info(
        "Health server running on port %s",
        port
    )

    return runner


# =========================================================
# MAIN
# =========================================================

async def main():

    logger.info(
        "Telegram Auto Speaker starting..."
    )

    # Start Render health server
    await start_health_server()

    # Start monitor
    asyncio.create_task(
        auto_monitor()
    )

    # Start Telegram bot
    await dp.start_polling(
        bot
    )


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
            "Bot stopped."
        )
