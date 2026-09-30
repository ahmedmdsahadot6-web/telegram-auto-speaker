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
    BufferedInputFile
)
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from telethon import TelegramClient, functions, types
from telethon.errors import RPCError, SessionPasswordNeededError

import qrcode


# =========================================================
# ENVIRONMENT
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()


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

os.makedirs("sessions", exist_ok=True)


# =========================================================
# TELEGRAM BOT
# =========================================================

bot = Bot(BOT_TOKEN)

dp = Dispatcher()

router = Router()

dp.include_router(router)


# =========================================================
# CLIENT STORAGE
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

class ChannelState(StatesGroup):

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
# START
# =========================================================

@router.message(CommandStart())
async def start_handler(message: Message):

    await message.answer(
        "🎙 <b>TELEGRAM AUTO SPEAKER</b>\n\n"
        "Channel member Live-এ join করলে "
        "automatically speaking permission দেওয়ার "
        "জন্য এই bot ব্যবহার করতে পারবেন।\n\n"
        "প্রথমে Telegram account connect করুন।",
        parse_mode="HTML",
        reply_markup=main_menu()
    )


# =========================================================
# HELP
# =========================================================

@router.callback_query(F.data == "help")
async def help_handler(call: CallbackQuery):

    await call.answer()

    await call.message.edit_text(

        "ℹ️ <b>কিভাবে কাজ করবে</b>\n\n"

        "1️⃣ Telegram account connect করুন\n"
        "2️⃣ নিজের Channel add করুন\n"
        "3️⃣ Live শুরু করুন\n"
        "4️⃣ Channel member Live-এ join করবে\n"
        "5️⃣ Bot member যাচাই করবে\n"
        "6️⃣ Member হলে Auto Allow করার চেষ্টা করবে\n"
        "7️⃣ Member নিজের Mic ON করতে পারবে\n\n"

        "⚠️ এটি Telegram Live / Group Call-এর "
        "participant permission ব্যবস্থার উপর নির্ভর করে।",

        parse_mode="HTML",
        reply_markup=main_menu()
    )


# =========================================================
# CONNECT TELEGRAM
# =========================================================

@router.callback_query(F.data == "connect")
async def connect_telegram(call: CallbackQuery):

    await call.answer()

    user_id = call.from_user.id

    client = get_client(user_id)

    try:

        await client.connect()

        # ---------------------------------------------
        # Already connected
        # ---------------------------------------------

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

            await call.message.answer(
                "✅ <b>Telegram Already Connected</b>\n\n"
                "আপনার Telegram account আগে থেকেই connected আছে।",
                parse_mode="HTML",
                reply_markup=main_menu()
            )

            return


        # ---------------------------------------------
        # Create QR Login
        # ---------------------------------------------

        login = await client.qr_login()

        login_tasks[user_id] = login

        # ---------------------------------------------
        # Generate QR
        # ---------------------------------------------

        qr_image = qrcode.make(login.url)

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


        # ---------------------------------------------
        # IMPORTANT:
        # Make tg://login link a clickable button
        # ---------------------------------------------

        login_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[

                [
                    InlineKeyboardButton(
                        text="📱 Open Telegram & Connect",
                        url=login.url
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


        # ---------------------------------------------
        # Send QR
        # ---------------------------------------------

        await call.message.answer_photo(

            photo=photo,

            caption=(
                "🔐 <b>Connect Telegram</b>\n\n"

                "নিচের QR Code Telegram App দিয়ে "
                "Scan করতে পারেন।\n\n"

                "অথবা নিচের "
                "<b>📱 Open Telegram & Connect</b> "
                "বাটনে চাপুন।\n\n"

                "📱 <b>QR পদ্ধতি:</b>\n"
                "Telegram → Settings → Devices → "
                "Link Desktop Device → QR Scan\n\n"

                "⚠️ OTP বা 2FA password এই bot-এ "
                "পাঠাবেন না।"
            ),

            parse_mode="HTML",

            reply_markup=login_keyboard
        )


        # ---------------------------------------------
        # Send clickable link separately as backup
        # ---------------------------------------------

        await call.message.answer(

            "🔗 <b>Login Link</b>\n\n"
            "উপরের বাটন কাজ না করলে নিচের বাটনে চাপুন।",

            parse_mode="HTML",

            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[

                    [
                        InlineKeyboardButton(
                            text="🔗 Open Telegram Login",
                            url=login.url
                        )
                    ]

                ]
            )
        )


        # ---------------------------------------------
        # Start waiting
        # ---------------------------------------------

        asyncio.create_task(
            wait_for_qr_login(user_id)
        )


    except Exception as e:

        logger.exception(e)

        await call.message.answer(
            "❌ <b>Telegram connect করা যায়নি</b>\n\n"
            f"<code>{str(e)}</code>",
            parse_mode="HTML"
        )


# =========================================================
# CANCEL LOGIN
# =========================================================

@router.callback_query(F.data == "cancel_login")
async def cancel_login(call: CallbackQuery):

    await call.answer()

    user_id = call.from_user.id

    login_tasks.pop(
        user_id,
        None
    )

    await call.message.answer(
        "❌ Login process cancelled.",
        reply_markup=main_menu()
    )


# =========================================================
# WAIT FOR QR LOGIN
# =========================================================

async def wait_for_qr_login(user_id):

    try:

        login = login_tasks.get(user_id)

        if not login:
            return

        client = get_client(user_id)


        # ---------------------------------------------
        # Wait for QR / login
        # ---------------------------------------------

        try:

            await login.wait(
                timeout=180
            )

        except SessionPasswordNeededError:

            await bot.send_message(
                user_id,

                "⚠️ আপনার Telegram account-এ "
                "2-Step Verification চালু আছে।\n\n"

                "এই bot আপনার 2FA password "
                "সংগ্রহ করবে না।"
            )

            return


        # ---------------------------------------------
        # Save connection
        # ---------------------------------------------

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


        # ---------------------------------------------
        # Success
        # ---------------------------------------------

        await bot.send_message(

            user_id,

            "✅ <b>Telegram Connected Successfully!</b>\n\n"
            "এখন আপনি আপনার Channel Add করতে পারবেন।",

            parse_mode="HTML",

            reply_markup=main_menu()
        )


    except asyncio.TimeoutError:

        await bot.send_message(
            user_id,
            "⏳ QR Login-এর সময় শেষ হয়ে গেছে।\n\n"
            "আবার 🔐 Connect Telegram চাপুন।",
            reply_markup=main_menu()
        )


    except Exception as e:

        logger.exception(e)

        try:

            await bot.send_message(
                user_id,

                "❌ <b>Login failed</b>\n\n"
                f"<code>{str(e)}</code>",

                parse_mode="HTML",

                reply_markup=main_menu()
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

@router.callback_query(F.data == "add_channel")
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
        (call.from_user.id,)
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

        "আপনার Channel username পাঠান।\n\n"

        "উদাহরণ:\n"
        "<code>@MyChannel</code>",

        parse_mode="HTML"
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

    client = get_client(user_id)


    try:

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

            reply_markup=main_menu()
        )


    except Exception as e:

        logger.exception(e)

        await message.answer(
            "❌ <b>Channel add করা যায়নি</b>\n\n"
            f"<code>{str(e)}</code>",
            parse_mode="HTML"
        )

        await state.clear()


# =========================================================
# MY CHANNELS
# =========================================================

@router.callback_query(F.data == "my_channels")
async def my_channels(call: CallbackQuery):

    await call.answer()


    rows = db.execute(
        """
        SELECT id, title, auto_allow
        FROM channels
        WHERE owner_id=? AND active=1
        """,
        (call.from_user.id,)
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
                callback_data=f"channel:{row['id']}"
            )

        ])


    buttons.append([

        InlineKeyboardButton(
            text="➕ Add Channel",
            callback_data="add_channel"
        )

    ])


    buttons.append([

        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data="back"
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


    await call.message.edit_text(

        text,

        parse_mode="HTML",

        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        )
    )


# =========================================================
# CHANNEL MENU
# =========================================================

@router.callback_query(F.data.startswith("channel:"))
async def channel_menu(call: CallbackQuery):

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

        "Auto Allow ON থাকলে bot "
        "Channel member-দের Live participant "
        "হিসেবে পেলে speaking permission দেওয়ার "
        "চেষ্টা করবে।",

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
# TOGGLE AUTO ALLOW
# =========================================================

@router.callback_query(F.data.startswith("toggle:"))
async def toggle_auto_allow(call: CallbackQuery):

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


    await channel_menu(call)


# =========================================================
# REMOVE CHANNEL
# =========================================================

@router.callback_query(F.data.startswith("remove:"))
async def remove_channel(call: CallbackQuery):

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
# GET ACTIVE CALL
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
# GET PARTICIPANTS
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
# ALLOW PARTICIPANT
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
# MONITOR CHANNEL
# =========================================================

async def monitor_channel(row):

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


        if not call:

            return


        participants, users = \
            await get_participants(

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


            is_member = await is_channel_member(

                client,

                channel_entity,

                user

            )


            if not is_member:

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

                        "Auto allowed user %s in %s",

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

@router.callback_query(F.data.startswith("statuslive:"))
async def live_status(call: CallbackQuery):

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

                "🔴 এখন কোনো Active Live নেই.",

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


        participants, users = \
            await get_participants(

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

            "❌ Status error:\n"
            f"{e}"

        )


# =========================================================
# BACK
# =========================================================

@router.callback_query(F.data == "back")
async def back_handler(call: CallbackQuery):

    await call.answer()


    await call.message.edit_text(

        "🎙 <b>TELEGRAM AUTO SPEAKER</b>\n\n"
        "Main Menu:",

        parse_mode="HTML",

        reply_markup=main_menu()
    )


# =========================================================
# MAIN
# =========================================================

async def main():

    logger.info(
        "Telegram Auto Speaker started"
    )


    asyncio.create_task(
        auto_monitor()
    )


    await dp.start_polling(
        bot
    )


if __name__ == "__main__":

    asyncio.run(main())
