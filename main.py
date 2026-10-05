import os
import sys
import asyncio
import logging
import sqlite3
from datetime import datetime

from dotenv import load_dotenv
from aiohttp import web

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from telethon import TelegramClient, functions, types
from telethon.errors import RPCError


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not API_ID:
    raise RuntimeError("API_ID is missing")

if not API_HASH:
    raise RuntimeError("API_HASH is missing")

API_ID = int(API_ID)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_FILE = os.path.join(BASE_DIR, "speaker_manager.db")
SESSION_DIR = os.path.join(BASE_DIR, "sessions")

os.makedirs(SESSION_DIR, exist_ok=True)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("speaker-manager")


# ============================================================
# DATABASE
# ============================================================

db = sqlite3.connect(DB_FILE, check_same_thread=False)

db.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    session_name TEXT NOT NULL,
    connected INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    channel_type TEXT NOT NULL,
    channel_username TEXT,
    channel_id INTEGER,
    title TEXT,
    active INTEGER DEFAULT 1,
    auto_allow INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(owner_id, channel_type)
)
""")

db.commit()


# ============================================================
# TELETHON CLIENTS
# ============================================================

clients = {}

monitor_task = None


def session_path(user_id: int) -> str:
    return os.path.join(
        SESSION_DIR,
        f"user_{user_id}"
    )


def get_or_create_client(user_id: int):
    if user_id not in clients:
        clients[user_id] = TelegramClient(
            session_path(user_id),
            API_ID,
            API_HASH,
        )

    return clients[user_id]


# ============================================================
# DATABASE HELPERS
# ============================================================

def create_user_if_needed(user_id: int):

    row = db.execute(
        "SELECT user_id FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()

    if not row:

        db.execute(
            """
            INSERT INTO users
            (user_id, session_name, connected, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                user_id,
                f"user_{user_id}",
                0,
                datetime.utcnow().isoformat(),
            ),
        )

        db.commit()


def set_connected(user_id: int, connected: bool):

    create_user_if_needed(user_id)

    db.execute(
        """
        UPDATE users
        SET connected=?
        WHERE user_id=?
        """,
        (
            1 if connected else 0,
            user_id,
        ),
    )

    db.commit()


def get_connected(user_id: int):

    row = db.execute(
        """
        SELECT connected
        FROM users
        WHERE user_id=?
        """,
        (user_id,),
    ).fetchone()

    return bool(row and row[0])


def save_channel(
    owner_id: int,
    channel_type: str,
    username: str,
    channel_id: int,
    title: str,
):

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
            active=1
        """,
        (
            owner_id,
            channel_type,
            username,
            channel_id,
            title,
            datetime.utcnow().isoformat(),
        ),
    )

    db.commit()


def get_channel(owner_id: int, channel_type: str):

    return db.execute(
        """
        SELECT
            channel_username,
            channel_id,
            title,
            active,
            auto_allow
        FROM channels
        WHERE owner_id=?
        AND channel_type=?
        """,
        (
            owner_id,
            channel_type,
        ),
    ).fetchone()


def get_all_users():

    return db.execute(
        """
        SELECT user_id
        FROM users
        WHERE connected=1
        """
    ).fetchall()


# ============================================================
# AIROGRAM
# ============================================================

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
router = Router()

dp.include_router(router)


# ============================================================
# STATES
# ============================================================

class TargetChannelState(StatesGroup):
    waiting_channel = State()


class LiveChannelState(StatesGroup):
    waiting_channel = State()


# ============================================================
# KEYBOARDS
# ============================================================

main_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🔐 Connect Telegram",
                callback_data="connect"
            )
        ],
        [
            InlineKeyboardButton(
                text="🎯 Target Channel",
                callback_data="target"
            )
        ],
        [
            InlineKeyboardButton(
                text="📺 Live Stream Channel",
                callback_data="live"
            )
        ],
        [
            InlineKeyboardButton(
                text="📊 Live Status",
                callback_data="status"
            )
        ],
        [
            InlineKeyboardButton(
                text="ℹ️ Help",
                callback_data="help"
            )
        ],
    ]
)


# ============================================================
# START
# ============================================================

@router.message(F.text == "/start")
async def start_handler(message: Message):

    create_user_if_needed(message.from_user.id)

    text = (
        "👋 <b>Auto Speaker Manager</b>\n\n"

        "এই বট Target Channel-এর সদস্যদের "
        "Live Stream-এর participant হিসেবে monitor করার জন্য তৈরি।\n\n"

        "⚙️ Workflow:\n"
        "1️⃣ Telegram User Account connect করুন\n"
        "2️⃣ Target Channel সেট করুন\n"
        "3️⃣ Live Stream Channel সেট করুন\n"
        "4️⃣ Live চালু হলে participant monitor হবে\n"
        "5️⃣ Target member হলে speaking permission দেওয়ার চেষ্টা করবে\n\n"

        "👇 নিচের Control Panel ব্যবহার করুন।"
    )

    await message.answer(
        text,
        reply_markup=main_keyboard,
        parse_mode="HTML",
    )


# ============================================================
# CONNECT
# ============================================================

@router.callback_query(F.data == "connect")
async def connect_handler(callback: CallbackQuery):

    user_id = callback.from_user.id

    create_user_if_needed(user_id)

    client = get_or_create_client(user_id)

    try:

        await client.connect()

        authorized = await client.is_user_authorized()

        if authorized:

            me = await client.get_me()

            set_connected(user_id, True)

            name = (
                getattr(me, "first_name", None)
                or getattr(me, "username", None)
                or "Telegram User"
            )

            await callback.message.answer(
                "✅ <b>Telegram already connected</b>\n\n"
                f"Account: <b>{name}</b>\n\n"
                "এখন Target Channel এবং Live Stream Channel সেট করুন।",
                parse_mode="HTML",
            )

        else:

            await callback.message.answer(
                "🔐 <b>Telegram account এখনো connected নয়।</b>\n\n"

                "এই Bot-এর ভিতরে OTP বা 2FA password নেওয়া হবে না।\n\n"

                "প্রথমে trusted computer/Termux/terminal-এ চালান:\n\n"

                "<code>python main.py --login "
                f"{user_id}</code>\n\n"

                "Terminal-এ Telegram phone number, login code এবং "
                "প্রয়োজনে 2FA password দিতে হবে।\n\n"

                "Login শেষ হলে এই Bot-এ আবার "
                "🔐 Connect Telegram চাপুন।",
                parse_mode="HTML",
            )

    except Exception as e:

        logger.exception("Connect error")

        await callback.message.answer(
            "❌ Telegram connection check failed.\n\n"
            f"<code>{str(e)[:500]}</code>",
            parse_mode="HTML",
        )

    await callback.answer()


# ============================================================
# TARGET CHANNEL
# ============================================================

@router.callback_query(F.data == "target")
async def target_handler(
    callback: CallbackQuery,
    state: FSMContext,
):

    user_id = callback.from_user.id

    if not get_connected(user_id):

        await callback.message.answer(
            "⚠️ আগে 🔐 Connect Telegram করুন।"
        )

        await callback.answer()
        return

    await state.set_state(
        TargetChannelState.waiting_channel
    )

    await callback.message.answer(
        "🎯 <b>Target Channel</b>\n\n"
        "Channel-এর @username অথবা public link পাঠান।\n\n"
        "উদাহরণ:\n"
        "<code>@examplechannel</code>",
        parse_mode="HTML",
    )

    await callback.answer()


@router.message(TargetChannelState.waiting_channel)
async def target_channel_received(
    message: Message,
    state: FSMContext,
):

    user_id = message.from_user.id

    username = message.text.strip()

    if username.startswith("https://t.me/"):
        username = username.replace(
            "https://t.me/",
            ""
        )

    username = username.rstrip("/")

    if not username.startswith("@"):
        username = "@" + username

    client = get_or_create_client(user_id)

    try:

        await client.connect()

        if not await client.is_user_authorized():

            await message.answer(
                "❌ Telegram account connected নেই।"
            )

            await state.clear()
            return

        entity = await client.get_entity(username)

        title = getattr(
            entity,
            "title",
            username,
        )

        channel_id = entity.id

        save_channel(
            owner_id=user_id,
            channel_type="target",
            username=username,
            channel_id=channel_id,
            title=title,
        )

        await message.answer(
            "✅ <b>Target Channel saved</b>\n\n"
            f"📌 {title}\n"
            f"🔗 {username}",
            parse_mode="HTML",
        )

    except Exception as e:

        logger.exception("Target channel error")

        await message.answer(
            "❌ Target Channel সেট করা যায়নি।\n\n"
            "Username/link ঠিক আছে কিনা এবং connected "
            "Telegram account channel-টি দেখতে পারে কিনা যাচাই করুন।\n\n"
            f"<code>{str(e)[:500]}</code>",
            parse_mode="HTML",
        )

    await state.clear()


# ============================================================
# LIVE CHANNEL
# ============================================================

@router.callback_query(F.data == "live")
async def live_handler(
    callback: CallbackQuery,
    state: FSMContext,
):

    user_id = callback.from_user.id

    if not get_connected(user_id):

        await callback.message.answer(
            "⚠️ আগে 🔐 Connect Telegram করুন।"
        )

        await callback.answer()
        return

    await state.set_state(
        LiveChannelState.waiting_channel
    )

    await callback.message.answer(
        "📺 <b>Live Stream Channel</b>\n\n"
        "যে Channel-এ Live চালাবে তার @username অথবা "
        "public link পাঠান।",
        parse_mode="HTML",
    )

    await callback.answer()


@router.message(LiveChannelState.waiting_channel)
async def live_channel_received(
    message: Message,
    state: FSMContext,
):

    user_id = message.from_user.id

    username = message.text.strip()

    if username.startswith("https://t.me/"):
        username = username.replace(
            "https://t.me/",
            ""
        )

    username = username.rstrip("/")

    if not username.startswith("@"):
        username = "@" + username

    client = get_or_create_client(user_id)

    try:

        await client.connect()

        if not await client.is_user_authorized():

            await message.answer(
                "❌ Telegram account connected নেই।"
            )

            await state.clear()
            return

        entity = await client.get_entity(username)

        title = getattr(
            entity,
            "title",
            username,
        )

        channel_id = entity.id

        save_channel(
            owner_id=user_id,
            channel_type="live",
            username=username,
            channel_id=channel_id,
            title=title,
        )

        await message.answer(
            "✅ <b>Live Stream Channel saved</b>\n\n"
            f"📺 {title}\n"
            f"🔗 {username}\n\n"

            "⚠️ Live পরিচালনার জন্য connected Telegram "
            "account-কে Channel-এর প্রয়োজনীয় admin "
            "permission দিতে হবে।",
            parse_mode="HTML",
        )

    except Exception as e:

        logger.exception("Live channel error")

        await message.answer(
            "❌ Live Channel সেট করা যায়নি।\n\n"
            f"<code>{str(e)[:500]}</code>",
            parse_mode="HTML",
        )

    await state.clear()


# ============================================================
# GET ACTIVE CALL
# ============================================================

async def get_active_call(client, entity):

    try:

        full = await client(
            functions.channels.GetFullChannelRequest(
                channel=entity
            )
        )

        return getattr(
            full.full_chat,
            "call",
            None,
        )

    except Exception:

        logger.exception(
            "Unable to get active call"
        )

        return None


# ============================================================
# GET PARTICIPANTS
# ============================================================

async def get_participants(
    client,
    call,
):

    try:

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

        return result.participants or []

    except Exception:

        logger.exception(
            "Unable to get participants"
        )

        return []


# ============================================================
# TARGET MEMBERSHIP
# ============================================================

async def is_target_member(
    client,
    target_entity,
    user_id,
):

    try:

        result = await client(
            functions.channels.GetParticipantRequest(
                channel=target_entity,
                participant=user_id,
            )
        )

        participant = result.participant

        if isinstance(
            participant,
            (
                types.ChannelParticipantLeft,
                types.ChannelParticipantBanned,
            )
        ):
            return False

        return True

    except RPCError:

        return False

    except Exception:

        return False


# ============================================================
# ALLOW / UNMUTE ATTEMPT
# ============================================================

async def allow_participant(
    client,
    call,
    user_id,
):

    try:

        input_call = types.InputGroupCall(
            id=call.id,
            access_hash=call.access_hash,
        )

        participant = types.InputPeerUser(
            user_id=user_id,
            access_hash=0,
        )

        await client(
            functions.phone.EditGroupCallParticipantRequest(
                call=input_call,
                participant=participant,
                muted=False,
            )
        )

        return True, "permission request sent"

    except RPCError as e:

        return False, str(e)

    except Exception as e:

        return False, str(e)


# ============================================================
# MONITOR ONE USER
# ============================================================

async def monitor_user(user_id):

    target = get_channel(
        user_id,
        "target",
    )

    live = get_channel(
        user_id,
        "live",
    )

    if not target or not live:
        return

    client = get_or_create_client(user_id)

    try:

        if not client.is_connected():

            await client.connect()

        if not await client.is_user_authorized():

            set_connected(
                user_id,
                False,
            )

            return

        target_username = target[0]
        live_username = live[0]

        target_entity = await client.get_entity(
            target_username
        )

        live_entity = await client.get_entity(
            live_username
        )

        call = await get_active_call(
            client,
            live_entity,
        )

        if not call:
            return

        participants = await get_participants(
            client,
            call,
        )

        for participant in participants:

            peer = getattr(
                participant,
                "peer",
                None,
            )

            if not isinstance(
                peer,
                types.PeerUser,
            ):
                continue

            user_id_in_call = peer.user_id

            is_member = await is_target_member(
                client,
                target_entity,
                user_id_in_call,
            )

            if not is_member:
                continue

            muted = getattr(
                participant,
                "muted",
                False,
            )

            if muted:

                success, reason = await allow_participant(
                    client,
                    call,
                    user_id_in_call,
                )

                if success:

                    logger.info(
                        "Speaking permission attempted: "
                        "owner=%s participant=%s",
                        user_id,
                        user_id_in_call,
                    )

                else:

                    logger.warning(
                        "Could not allow participant: "
                        "owner=%s participant=%s reason=%s",
                        user_id,
                        user_id_in_call,
                        reason,
                    )

    except Exception:

        logger.exception(
            "Monitor error for user %s",
            user_id,
        )


# ============================================================
# AUTO MONITOR
# ============================================================

async def auto_monitor():

    logger.info(
        "Auto monitor started"
    )

    while True:

        try:

            users = get_all_users()

            for row in users:

                user_id = row[0]

                try:

                    await monitor_user(
                        user_id
                    )

                except Exception:

                    logger.exception(
                        "User monitor failed: %s",
                        user_id,
                    )

        except Exception:

            logger.exception(
                "Monitor loop error"
            )

        await asyncio.sleep(5)


# ============================================================
# LIVE STATUS
# ============================================================

@router.callback_query(F.data == "status")
async def status_handler(
    callback: CallbackQuery,
):

    user_id = callback.from_user.id

    target = get_channel(
        user_id,
        "target",
    )

    live = get_channel(
        user_id,
        "live",
    )

    if not target or not live:

        await callback.message.answer(
            "⚠️ আগে Target Channel এবং Live Stream Channel সেট করুন।"
        )

        await callback.answer()
        return

    client = get_or_create_client(user_id)

    try:

        await client.connect()

        if not await client.is_user_authorized():

            await callback.message.answer(
                "❌ Telegram account connected নেই।"
            )

            await callback.answer()
            return

        target_entity = await client.get_entity(
            target[0]
        )

        live_entity = await client.get_entity(
            live[0]
        )

        call = await get_active_call(
            client,
            live_entity,
        )

        if not call:

            await callback.message.answer(
                "📴 বর্তমানে কোনো active Live পাওয়া যায়নি।"
            )

            await callback.answer()
            return

        participants = await get_participants(
            client,
            call,
        )

        total_target = 0
        muted_count = 0
        allowed_count = 0

        for participant in participants:

            peer = getattr(
                participant,
                "peer",
                None,
            )

            if not isinstance(
                peer,
                types.PeerUser,
            ):
                continue

            member = await is_target_member(
                client,
                target_entity,
                peer.user_id,
            )

            if not member:
                continue

            total_target += 1

            muted = getattr(
                participant,
                "muted",
                False,
            )

            if muted:
                muted_count += 1
            else:
                allowed_count += 1

        text = (
            "📊 <b>Live Status</b>\n\n"
            f"👥 Target members in Live: "
            f"<b>{total_target}</b>\n\n"
            f"🔇 Muted: <b>{muted_count}</b>\n"
            f"🎙 Allowed: <b>{allowed_count}</b>\n"
        )

        await callback.message.answer(
            text,
            parse_mode="HTML",
        )

    except Exception as e:

        logger.exception(
            "Status error"
        )

        await callback.message.answer(
            "❌ Status পাওয়া যায়নি।\n\n"
            f"<code>{str(e)[:500]}</code>",
            parse_mode="HTML",
        )

    await callback.answer()


# ============================================================
# HELP
# ============================================================

@router.callback_query(F.data == "help")
async def help_handler(
    callback: CallbackQuery,
):

    text = (
        "ℹ️ <b>How to use</b>\n\n"

        "1️⃣ প্রথমে trusted terminal-এ:\n"
        "<code>python main.py --login YOUR_BOT_USER_ID</code>\n\n"

        "2️⃣ Telegram account login শেষ করুন।\n\n"

        "3️⃣ Bot-এ 🔐 Connect Telegram চাপুন।\n\n"

        "4️⃣ 🎯 Target Channel সেট করুন।\n\n"

        "5️⃣ 📺 Live Stream Channel সেট করুন।\n\n"

        "6️⃣ Live চালু হলে monitor কাজ করবে।\n\n"

        "⚠️ Bot নিজে OTP বা 2FA password নেয় না।\n"
        "⚠️ Session file কারও সাথে share করবেন না।"
    )

    await callback.message.answer(
        text,
        parse_mode="HTML",
    )

    await callback.answer()


# ============================================================
# HEALTH SERVER
# ============================================================

async def health(request):

    return web.Response(
        text="Auto Speaker Manager is running."
    )


async def health_server():

    app = web.Application()

    app.router.add_get(
        "/",
        health,
    )

    app.router.add_get(
        "/health",
        health,
    )

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT,
    )

    await site.start()

    logger.info(
        "Health server running on port %s",
        PORT,
    )

    return runner


# ============================================================
# TELEGRAM SESSION LOGIN
# ============================================================

async def login_account(user_id: int):

    client = TelegramClient(
        session_path(user_id),
        API_ID,
        API_HASH,
    )

    print()
    print("=" * 60)
    print(" TELEGRAM USER ACCOUNT LOGIN")
    print("=" * 60)
    print()
    print(f"Bot user ID: {user_id}")
    print()
    print(
        "Your phone number, Telegram login code and "
        "2FA password (if enabled) are entered ONLY "
        "in this trusted terminal."
    )
    print()
    print("They are NOT sent to the Telegram bot.")
    print()

    try:

        await client.start()

        me = await client.get_me()

        print()
        print("✅ LOGIN SUCCESSFUL")
        print()

        print(
            "Account:",
            getattr(me, "first_name", "") or "",
            getattr(me, "last_name", "") or "",
        )

        print(
            "Username:",
            f"@{me.username}" if me.username else "none",
        )

        print()
        print(
            "Session created at:"
        )

        print(
            session_path(user_id) + ".session"
        )

        create_user_if_needed(user_id)

        set_connected(
            user_id,
            True,
        )

        print()
        print(
            "Now start the bot normally with:"
        )

        print()
        print(
            "python main.py"
        )

        print()

    except Exception as e:

        print()
        print("❌ LOGIN FAILED")
        print(str(e))
        print()

    finally:

        await client.disconnect()


# ============================================================
# MAIN
# ============================================================

async def main():

    global monitor_task

    logger.info(
        "Starting Auto Speaker Manager..."
    )

    await health_server()

    monitor_task = asyncio.create_task(
        auto_monitor()
    )

    try:

        await dp.start_polling(
            bot
        )

    finally:

        if monitor_task:

            monitor_task.cancel()

            try:
                await monitor_task
            except asyncio.CancelledError:
                pass

        for client in clients.values():

            try:

                if client.is_connected():
                    await client.disconnect()

            except Exception:
                pass

        await bot.session.close()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    if len(sys.argv) >= 2:

        command = sys.argv[1]

        if command == "--login":

            if len(sys.argv) < 3:

                print(
                    "Usage: python main.py --login YOUR_BOT_USER_ID"
                )

                sys.exit(1)

            try:

                login_user_id = int(
                    sys.argv[2]
                )

            except ValueError:

                print(
                    "User ID must be a number."
                )

                sys.exit(1)

            asyncio.run(
                login_account(
                    login_user_id
                )
            )

        else:

            print(
                "Unknown command."
            )

            print(
                "Use:"
            )

            print(
                "python main.py"
            )

            print(
                "python main.py --login YOUR_BOT_USER_ID"
            )

    else:

        asyncio.run(
            main()
        )
