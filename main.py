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
from telethon.errors import (
    SessionPasswordNeededError
)

import qrcode
from aiohttp import web


# =========================================================
# ENV
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv(
    "BOT_TOKEN",
    ""
).strip()

API_HASH = os.getenv(
    "API_HASH",
    ""
).strip()

try:
    API_ID = int(
        os.getenv(
            "API_ID",
            "0"
        ).strip()
    )
except ValueError:
    API_ID = 0


if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN missing"
    )

if not API_ID:
    raise RuntimeError(
        "API_ID missing"
    )

if not API_HASH:
    raise RuntimeError(
        "API_HASH missing"
    )


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(
    "AutoSpeaker"
)


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
# TELEGRAM CLIENTS
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
# FSM
# =========================================================

class ChannelState(
    StatesGroup
):

    waiting_channel = State()


# =========================================================
# MAIN MENU
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
# REPLY KEYBOARD
# =========================================================

def reply_keyboard():

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
# START
# =========================================================

@router.message(
    CommandStart()
)
async def start_handler(
    message: Message
):

    await message.answer(

        "🎙 <b>TELEGRAM AUTO SPEAKER</b>\n\n"

        "আপনার Channel Live-এ member "
        "join করলে member যাচাই করে "
        "speaking permission দেওয়ার "
        "চেষ্টা করবে।\n\n"

        "প্রথমে নিচের "
        "🔐 Connect Telegram চাপুন।",

        parse_mode="HTML",

        reply_markup=reply_keyboard()
    )

    await message.answer(

        "👇 <b>Main Menu</b>",

        parse_mode="HTML",

        reply_markup=main_menu()
    )


# =========================================================
# CREATE QR + LOGIN
# =========================================================

async def start_telegram_login(
    user_id,
    send_func
):

    # আগের login থাকলে remove
    old_task = login_tasks.pop(
        user_id,
        None
    )

    client = get_client(
        user_id
    )

    try:

        if not client.is_connected():

            await client.connect()

        # Already authorized
        if await client.is_user_authorized():

            db.execute(
                """
                INSERT OR REPLACE INTO users
                (
                    user_id,
                    session_name,
                    connected,
                    created_at
                )
                VALUES (?, ?, 1, ?)
                """,
                (
                    user_id,
                    session_path(user_id),
                    int(time.time())
                )
            )

            db.commit()

            await send_func(

                "✅ <b>Telegram account already connected!</b>\n\n"
                "এখন ➕ Add Channel চাপুন।",

                parse_mode="HTML"
            )

            return

        # =================================================
        # QR LOGIN
        # =================================================

        login = await client.qr_login()

        login_tasks[user_id] = login

        login_url = login.url

        # =================================================
        # CREATE QR IMAGE
        # =================================================

        qr_image = qrcode.make(
            login_url
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

        # =================================================
        # BUTTONS
        # =================================================

        keyboard = InlineKeyboardMarkup(

            inline_keyboard=[

                [
                    InlineKeyboardButton(

                        text="📲 Open / Connect Telegram",

                        url=login_url

                    )
                ],

                [
                    InlineKeyboardButton(

                        text="🔄 Generate New QR",

                        callback_data="connect"

                    )
                ],

                [
                    InlineKeyboardButton(

                        text="❌ Cancel",

                        callback_data="cancel_login"

                    )
                ]

            ]

        )

        # =================================================
        # SEND QR IMAGE
        # =================================================

        await send_func(

            photo=photo,

            caption=(

                "🔐 <b>Connect Telegram</b>\n\n"

                "🖼️ উপরের QR Code অন্য একটি "
                "ডিভাইস দিয়ে scan করতে পারবেন।\n\n"

                "📲 অথবা নিচের "
                "<b>Open / Connect Telegram</b> "
                "button চাপুন।\n\n"

                "⏱️ Login session সীমিত সময়ের জন্য valid।\n\n"

                "⚠️ শুধুমাত্র নিজের Telegram account "
                "connect করুন।"

            ),

            parse_mode="HTML",

            reply_markup=keyboard

        )

        # =================================================
        # START LOGIN MONITOR
        # =================================================

        asyncio.create_task(

            wait_for_qr_login(
                user_id
            )

        )

    except Exception as e:

        logger.exception(
            "Telegram login error"
        )

        await send_func(

            f"❌ Telegram connect করা যায়নি:\n\n"
            f"{e}"

        )


# =========================================================
# CONNECT BUTTON
# =========================================================

@router.callback_query(
    F.data == "connect"
)
async def connect_telegram(
    call: CallbackQuery
):

    await call.answer()

    await start_telegram_login(

        call.from_user.id,

        call.message.answer

    )


# =========================================================
# CONNECT REPLY BUTTON
# =========================================================

@router.message(
    F.text == "🔐 Connect Telegram"
)
async def keyboard_connect(
    message: Message
):

    await start_telegram_login(

        message.from_user.id,

        message.answer

    )


# =========================================================
# WAIT FOR QR LOGIN
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

                "আপনার Telegram account-এ "
                "2FA password চালু আছে।\n\n"

                "এই version আপনার password "
                "সংরক্ষণ করে না, তাই login "
                "সম্পূর্ণ করা যায়নি।",

                parse_mode="HTML"

            )

            return

        # =================================================
        # SAVE USER
        # =================================================

        db.execute(

            """
            INSERT OR REPLACE INTO users
            (
                user_id,
                session_name,
                connected,
                created_at
            )
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

            "✅ <b>Telegram account connected!</b>\n\n"
            "এখন ➕ Add Channel চাপুন।",

            parse_mode="HTML",

            reply_markup=reply_keyboard()

        )

    except asyncio.TimeoutError:

        await bot.send_message(

            user_id,

            "⏱️ Login session-এর সময় শেষ হয়েছে।\n\n"
            "আবার 🔐 Connect Telegram চাপুন।"

        )

    except Exception as e:

        logger.exception(
            "Login monitor error"
        )

        try:

            await bot.send_message(

                user_id,

                f"❌ Login failed:\n\n{e}"

            )

        except Exception:
            pass

    finally:

        login_tasks.pop(
            user_id,
            None
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

    await call.answer(
        "Login cancelled"
    )

    login_tasks.pop(
        call.from_user.id,
        None
    )

    await call.message.answer(

        "❌ Telegram login cancelled.\n\n"
        "প্রয়োজনে আবার 🔐 Connect Telegram চাপুন।",

        reply_markup=main_menu()

    )


# =========================================================
# HELP
# =========================================================

@router.callback_query(
    F.data == "help"
)
async def help_callback(
    call: CallbackQuery
):

    await call.answer()

    await call.message.edit_text(

        "ℹ️ <b>কিভাবে কাজ করবে</b>\n\n"

        "1️⃣ Telegram account connect করুন\n"
        "2️⃣ নিজের Channel add করুন\n"
        "3️⃣ Live শুরু করুন\n"
        "4️⃣ Member Live-এ join করবে\n"
        "5️⃣ Bot member যাচাই করবে\n"
        "6️⃣ Auto Allow করার চেষ্টা করবে\n\n"

        "⚠️ Connected Telegram account-এর "
        "Channel management permission থাকতে হবে।",

        parse_mode="HTML",

        reply_markup=main_menu()

    )


@router.message(
    F.text == "ℹ️ Help"
)
async def keyboard_help(
    message: Message
):

    await message.answer(

        "ℹ️ <b>কিভাবে কাজ করবে</b>\n\n"

        "1️⃣ Telegram account connect করুন\n"
        "2️⃣ নিজের Channel add করুন\n"
        "3️⃣ Live শুরু করুন\n"
        "4️⃣ Member Live-এ join করবে\n"
        "5️⃣ Bot member যাচাই করবে\n"
        "6️⃣ Auto Allow করার চেষ্টা করবে।",

        parse_mode="HTML"

    )


# =========================================================
# ADD CHANNEL
# =========================================================

@router.callback_query(
    F.data == "add_channel"
)
async def add_channel_start(

    call: CallbackQuery,

    state: FSMContext

):

    await call.answer()

    row = db.execute(

        """
        SELECT connected
        FROM users
        WHERE user_id=?
        """,

        (
            call.from_user.id,
        )

    ).fetchone()

    if not row or not row["connected"]:

        await call.message.answer(

            "❌ আগে 🔐 Connect Telegram করুন।"

        )

        return

    await state.set_state(

        ChannelState.waiting_channel

    )

    await call.message.answer(

        "➕ <b>Add Channel</b>\n\n"

        "Channel username পাঠান।\n\n"

        "উদাহরণ:\n"
        "<code>@MyChannel</code>",

        parse_mode="HTML"

    )


@router.message(
    F.text == "➕ Add Channel"
)
async def keyboard_add_channel(

    message: Message,

    state: FSMContext

):

    row = db.execute(

        """
        SELECT connected
        FROM users
        WHERE user_id=?
        """,

        (
            message.from_user.id,
        )

    ).fetchone()

    if not row or not row["connected"]:

        await message.answer(

            "❌ আগে 🔐 Connect Telegram করুন।"

        )

        return

    await state.set_state(

        ChannelState.waiting_channel

    )

    await message.answer(

        "➕ <b>Add Channel</b>\n\n"

        "Channel username পাঠান।\n\n"

        "উদাহরণ:\n"
        "<code>@MyChannel</code>",

        parse_mode="HTML"

    )


# =========================================================
# SAVE CHANNEL
# =========================================================

@router.message(
    ChannelState.waiting_channel
)
async def save_channel(

    message: Message,

    state: FSMContext

):

    if not message.text:

        await message.answer(
            "❌ Channel username পাঠান।"
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

        if not client.is_connected():

            await client.connect()

        if not await client.is_user_authorized():

            await message.answer(

                "❌ Telegram account connected নেই।"

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
                int(time.time())
            )

        )

        db.commit()

        await state.clear()

        await message.answer(

            f"✅ <b>Channel Added</b>\n\n"

            f"📺 {title}\n"
            f"🔗 {username}\n\n"

            "🎤 Auto Allow: <b>ON</b>",

            parse_mode="HTML",

            reply_markup=reply_keyboard()

        )

    except Exception as e:

        logger.exception(
            "Channel add error"
        )

        await message.answer(

            f"❌ Channel add করা যায়নি:\n\n{e}"

        )

        await state.clear()


# =========================================================
# MY CHANNELS
# =========================================================

async def show_my_channels(
    message: Message
):

    rows = db.execute(

        """
        SELECT id, title, auto_allow
        FROM channels
        WHERE owner_id=? AND active=1
        """,

        (
            message.from_user.id,
        )

    ).fetchall()

    buttons = []

    for row in rows:

        status = (
            "🟢"
            if row["auto_allow"]
            else
            "🔴"
        )

        buttons.append([

            InlineKeyboardButton(

                text=f"{status} {row['title']}",

                callback_data=
                f"channel:{row['id']}"

            )

        ])

    buttons.append([

        InlineKeyboardButton(

            text="➕ Add Channel",

            callback_data="add_channel"

        )

    ])

    if rows:

        text = (

            "📺 <b>My Channels</b>\n\n"

            "🟢 Auto Allow ON\n"
            "🔴 Auto Allow OFF"

        )

    else:

        text = (

            "📺 <b>My Channels</b>\n\n"

            "কোনো Channel নেই।"

        )

    await message.answer(

        text,

        parse_mode="HTML",

        reply_markup=InlineKeyboardMarkup(

            inline_keyboard=buttons

        )

    )


@router.callback_query(
    F.data == "my_channels"
)
async def my_channels(
    call: CallbackQuery
):

    await call.answer()

    await show_my_channels(
        call.message
    )


@router.message(
    F.text == "📺 My Channels"
)
async def keyboard_my_channels(
    message: Message
):

    await show_my_channels(
        message
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

    channel_id = int(

        call.data.split(":")[1]

    )

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
        return

    status = (
        "🟢 ON"
        if row["auto_allow"]
        else
        "🔴 OFF"
    )

    await call.message.edit_text(

        f"📺 <b>{row['title']}</b>\n\n"

        f"🎤 Auto Allow: <b>{status}</b>\n\n"

        "Auto Allow ON থাকলে bot Live participant "
        "member কিনা যাচাই করে speaking permission "
        "দেওয়ার চেষ্টা করবে।",

        parse_mode="HTML",

        reply_markup=InlineKeyboardMarkup(

            inline_keyboard=[

                [

                    InlineKeyboardButton(

                        text="🎤 Toggle Auto Allow",

                        callback_data=
                        f"toggle:{channel_id}"

                    )

                ],

                [

                    InlineKeyboardButton(

                        text="📊 Live Status",

                        callback_data=
                        f"statuslive:{channel_id}"

                    )

                ],

                [

                    InlineKeyboardButton(

                        text="❌ Remove Channel",

                        callback_data=
                        f"remove:{channel_id}"

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

        "✅ Channel removed.",

        reply_markup=main_menu()

    )


# =========================================================
# GET ACTIVE LIVE
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

        access_hash = getattr(

            user,

            "access_hash",

            None

        )

        if not access_hash:

            return False

        result = await client(

            functions.channels.GetParticipantRequest(

                channel=channel_entity,

                participant=types.InputPeerUser(

                    user_id=user.id,

                    access_hash=access_hash

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
# ALLOW SPEAK
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

    participant = await client.get_input_entity(
        user
    )

    await client(

        functions.phone.EditGroupCallParticipantRequest(

            call=input_call,

            participant=participant,

            muted=False

        )

    )


# =========================================================
# LIVE MONITOR
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

        call, channel_entity = await get_active_call(

            client,

            row["channel_username"]

        )

        if not call:

            return

        participants, users = await get_participants(

            client,

            call

        )

        for participant in participants:

            peer = participant.peer

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

            member = await is_channel_member(

                client,

                channel_entity,

                user

            )

            if not member:

                continue

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

                        "Allowed member %s in %s",

                        user.id,

                        row["channel_username"]

                    )

                except Exception as e:

                    logger.warning(

                        "Allow failed: %s",

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

                await monitor_channel(
                    row
                )

                await asyncio.sleep(
                    0.5
                )

        except Exception as e:

            logger.exception(e)

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

        active_call, channel_entity = await get_active_call(

            client,

            row["channel_username"]

        )

        if not active_call:

            await call.message.edit_text(

                "🔴 এখন কোনো Active Live নেই।",

                reply_markup=InlineKeyboardMarkup(

                    inline_keyboard=[

                        [

                            InlineKeyboardButton(

                                text="⬅️ Back",

                                callback_data=
                                f"channel:{channel_id}"

                            )

                        ]

                    ]

                )

            )

            return

        participants, users = await get_participants(

            client,

            active_call

        )

        members = 0

        allowed = 0

        muted = 0

        for participant in participants:

            peer = participant.peer

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

                            callback_data=
                            f"statuslive:{channel_id}"

                        )

                    ],

                    [

                        InlineKeyboardButton(

                            text="⬅️ Back",

                            callback_data=
                            f"channel:{channel_id}"

                        )

                    ]

                ]

            )

        )

    except Exception as e:

        await call.message.answer(

            f"❌ Status error:\n{e}"

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

    await call.message.edit_text(

        "🎙 <b>TELEGRAM AUTO SPEAKER</b>\n\n"
        "Main Menu:",

        parse_mode="HTML",

        reply_markup=main_menu()

    )


# =========================================================
# UPTIME ROBOT HEALTH
# =========================================================

async def health_handler(
    request
):

    return web.json_response({

        "status": "ok",

        "service": "telegram-auto-speaker",

        "timestamp": int(
            time.time()
        )

    })


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

        "Health server started on port %s",

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

    health_runner = await start_health_server()

    asyncio.create_task(
        auto_monitor()
    )

    try:

        await dp.start_polling(
            bot
        )

    finally:

        await health_runner.cleanup()

        await bot.session.close()

        db.close()


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    asyncio.run(
        main()
    )
