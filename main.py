import os
import io
import time
import asyncio
import logging
import sqlite3

from dotenv import load_dotenv
from aiohttp import web

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("AutoSpeaker")

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

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

clients = {}

def get_client(user_id):
    if user_id not in clients:
        row = db.execute(
            "SELECT session_string FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()
        
        session_str = row["session_string"] if row and row["session_string"] else ""
        
        clients[user_id] = TelegramClient(
            StringSession(session_str),
            API_ID,
            API_HASH,
            device_model="PC 64bit",
            system_version="Windows 11",
            app_version="4.8.1 x64",
            lang_code="en",
            system_lang_code="en-US",
            connection_retries=5,
            timeout=30
        )
    return clients[user_id]

class StringSessionState(StatesGroup):
    waiting_session = State()

class TargetChannelState(StatesGroup):
    waiting_channel = State()

class LiveChannelState(StatesGroup):
    waiting_channel = State()

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🔐 Connect Telegram"),
                KeyboardButton(text="🎯 Target Channel")
            ],
            [
                KeyboardButton(text="📺 Live Stream Channel"),
                KeyboardButton(text="📊 Live Status")
            ],
            [
                KeyboardButton(text="ℹ️ Help")
            ]
        ],
        resize_keyboard=True,
        is_persistent=True
    )

def main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔐 Connect Telegram", callback_data="connect")],
            [InlineKeyboardButton(text="🎯 Target Channel", callback_data="target")],
            [InlineKeyboardButton(text="📺 Live Stream Channel", callback_data="live")],
            [InlineKeyboardButton(text="📊 Live Status", callback_data="status")],
            [InlineKeyboardButton(text="ℹ Help", callback_data="help")]
        ]
    )

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
        reply_markup=main_keyboard()
    )
    await message.answer(
        "👇 Control Panel:",
        reply_markup=main_menu()
    )

async def send_help(message):
    await message.answer(
        "ℹ️ <b>কিভাবে কাজ করবে</b>\n\n"
        "1️⃣ 🔐 Connect Telegram করুন (Telethon String Session দিয়ে)\n\n"
        "2️⃣ 🎯 Target Channel সেট করুন\n"
        "এই Channel-এর সদস্যদের শনাক্ত করা হবে।\n\n"
        "3️⃣ 📺 Live Stream Channel সেট করুন\n"
        "এই Channel-এ Telegram Live চলবে।\n\n"
        "4️⃣ Live শুরু করুন\n\n"
        "5️⃣ Target Channel-এর কোনো সদস্য "
        "Live-এ join করলে bot তাকে শনাক্ত করবে।\n\n"
        "6️⃣ Bot speaking permission দেওয়ার "
        "চেষ্টা করবে।\n\n"
        "⚠️ Live Stream Channel-এ connected "
        "Telegram account-এর প্রয়োজনীয় "
        "admin/manage-call permission থাকতে হবে।",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )

@router.message(F.text == "ℹ️ Help")
async def help_text_handler(message: Message):
    await send_help(message)

@router.callback_query(F.data == "help")
async def help_callback(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "ℹ️ Help",
        reply_markup=main_keyboard()
    )
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
    
    if is_connected_user(user_id):
        await message.answer(
            "✅ <b>Telegram Already Connected</b>\n\n"
            "এখন Target ও Live Channel সেট করুন।",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )
        return

    await state.set_state(StringSessionState.waiting_session)
    await message.answer(
        "🔐 <b>Connect via String Session</b>\n\n"
        "দয়া করে আপনার Telethon <b>String Session</b> টি এখানে পাঠান:",
        parse_mode="HTML"
    )

@router.message(StringSessionState.waiting_session)
async def process_string_session(message: Message, state: FSMContext):
    session_str = message.text.strip()
    user_id = message.chat.id

    if user_id in clients:
        try:
            await clients[user_id].disconnect()
        except Exception:
            pass
        del clients[user_id]

    client = TelegramClient(
        StringSession(session_str),
        API_ID,
        API_HASH,
        device_model="PC 64bit",
        system_version="Windows 11",
        app_version="4.8.1 x64",
        lang_code="en",
        system_lang_code="en-US"
    )

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await message.answer("❌ প্রদত্ত String Session টি সঠিক নয় অথবা মেয়াদোত্তীর্ণ। আবার চেষ্টা করুন:")
            await client.disconnect()
            return

        clients[user_id] = client

        db.execute(
            """    
            INSERT OR REPLACE INTO users    
            (user_id, session_string, connected, created_at)    
            VALUES (?, ?, 1, ?)    
            """,
            (user_id, session_str, int(time.time()))
        )
        db.commit()
        await state.clear()

        await message.answer(
            "✅ <b>Telegram Connected Successfully!</b>\n\n"
            "এখন Target ও Live Channel সেট করুন।",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )

    except Exception as e:
        logger.exception(e)
        await message.answer(f"❌ কানেক্ট করতে সমস্যা হয়েছে:\n\n{e}")
        await state.clear()

def is_connected_user(user_id):
    row = db.execute(
        """
        SELECT connected
        FROM users
        WHERE user_id=?
        """,
        (user_id,)
    ).fetchone()
    return bool(row and row["connected"])

@router.message(F.text == "🎯 Target Channel")
async def target_text_handler(message: Message, state: FSMContext):
    await target_start(message, state)

@router.callback_query(F.data == "target")
async def target_callback(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await target_start(call.message, state)

async def target_start(message, state):
    user_id = message.chat.id
    if not is_connected_user(user_id):
        await message.answer("❌ আগে 🔐 Connect Telegram করুন।")
        return

    old = db.execute(    
        """    
        SELECT *    
        FROM channels    
        WHERE owner_id=?    
        AND channel_type='target'    
        AND active=1    
        """,    
        (user_id,)    
    ).fetchone()    

    if old:    
        await message.answer(    
            "🎯 <b>Target Channel</b>\n\n"    
            f"📺 {old['title']}\n"    
            f"🔗 {old['channel_username']}\n\n"    
            "এই Channel-এর সদস্যদের Live-এ শনাক্ত করা হবে।\n\n"    
            "পরিবর্তন করতে নতুন @username পাঠান।",    
            parse_mode="HTML"    
        )    
    else:    
        await message.answer(    
            "🎯 <b>Target Channel</b>\n\n"    
            "যে Channel-এর সদস্যদের Live-এ Allow to Speak করতে চান সেই Channel-এর username পাঠান।\n\n"    
            "উদাহরণ:\n"    
            "<code>@MyTargetChannel</code>",    
            parse_mode="HTML"    
        )    

    await state.set_state(TargetChannelState.waiting_channel)

@router.message(TargetChannelState.waiting_channel)
async def save_target_channel(message: Message, state: FSMContext):
    await save_channel(message, state, "target")

@router.message(F.text == "📺 Live Stream Channel")
async def live_text_handler(message: Message, state: FSMContext):
    await live_start(message, state)

@router.callback_query(F.data == "live")
async def live_callback(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await live_start(call.message, state)

async def live_start(message, state):
    user_id = message.chat.id
    if not is_connected_user(user_id):
        await message.answer("❌ আগে 🔐 Connect Telegram করুন।")
        return

    old = db.execute(    
        """    
        SELECT *    
        FROM channels    
        WHERE owner_id=?    
        AND channel_type='live'    
        AND active=1    
        """,    
        (user_id,)    
    ).fetchone()    

    if old:    
        await message.answer(    
            "📺 <b>Live Stream Channel</b>\n\n"    
            f"📺 {old['title']}\n"    
            f"🔗 {old['channel_username']}\n\n"    
            "এই Channel-এ Live Stream চলবে।\n\n"    
            "পরিবর্তন করতে নতুন @username পাঠান।",    
            parse_mode="HTML"    
        )    
    else:    
        await message.answer(    
            "📺 <b>Live Stream Channel</b>\n\n"    
            "যে Channel-এ Telegram Live চলবে সেই Channel-এর username পাঠান।\n\n"    
            "উদাহরণ:\n"    
            "<code>@MyLiveChannel</code>",    
            parse_mode="HTML"    
        )    

    await state.set_state(LiveChannelState.waiting_channel)

@router.message(LiveChannelState.waiting_channel)
async def save_live_channel(message: Message, state: FSMContext):
    await save_channel(message, state, "live")

async def save_channel(message, state, channel_type):
    username = (message.text or "").strip()
    if not username:
        await message.answer("❌ Channel username দিন।")
        return

    if not username.startswith("@"):    
        username = "@" + username    

    user_id = message.from_user.id    
    client = get_client(user_id)    

    try:    
        if not client.is_connected():  
            await client.connect()  
              
        if not await client.is_user_authorized():    
            await message.answer("❌ Telegram account connected নেই।")    
            await state.clear()    
            return    

        entity = await client.get_entity(username)    
        title = getattr(entity, "title", username)    

        await client(functions.channels.GetFullChannelRequest(channel=entity))    

        db.execute(    
            """    
            INSERT INTO channels    
            (owner_id, channel_type, channel_username, channel_id, title, active, auto_allow, created_at)    
            VALUES (?, ?, ?, ?, ?, 1, 1, ?)    
            ON CONFLICT(owner_id, channel_type)    
            DO UPDATE SET    
                channel_username=excluded.channel_username,    
                channel_id=excluded.channel_id,    
                title=excluded.title,    
                active=1,    
                created_at=excluded.created_at    
            """,    
            (user_id, channel_type, username, entity.id, title, int(time.time()))    
        )    
        db.commit()    
        await state.clear()    

        if channel_type == "target":    
            await message.answer(    
                "✅ <b>Target Channel Added</b>\n\n"    
                f"📺 {title}\n"    
                f"🔗 {username}\n\n"    
                "🎯 এই Channel-এর সদস্যদের Live-এ শনাক্ত করা হবে।\n\n"    
                "এখন 📺 Live Stream Channel সেট করুন।",    
                parse_mode="HTML",    
                reply_markup=main_keyboard()    
            )    
        else:    
            await message.answer(    
                "✅ <b>Live Stream Channel Added</b>\n\n"    
                f"📺 {title}\n"    
                f"🔗 {username}\n\n"    
                "🎙 এই Channel-এর Live monitor করা হবে।\n\n"    
                "দুইটি Channel সেটআপ হয়ে গেছে।",    
                parse_mode="HTML",    
                reply_markup=main_keyboard()    
            )    

    except Exception as e:    
        logger.exception(e)    
        await message.answer(f"❌ Channel add করা যায়নি。\n\n{e}")    
        await state.clear()

def get_channel(owner_id, channel_type):
    return db.execute(
        """
        SELECT *
        FROM channels
        WHERE owner_id=?
        AND channel_type=?
        AND active=1
        """,
        (owner_id, channel_type)
    ).fetchone()

async def get_active_call(client, channel_username):
    entity = await client.get_entity(channel_username)
    full = await client(functions.channels.GetFullChannelRequest(channel=entity))
    call = getattr(full.full_chat, "call", None)
    return call, entity

async def get_participants(client, call):
    input_call = types.InputGroupCall(id=call.id, access_hash=call.access_hash)
    result = await client(functions.phone.GetGroupCallRequest(call=input_call, limit=100))
    users = {user.id: user for user in result.users}
    return result.participants, users

async def is_target_member(client, target_entity, user_id):
    try:
        participant = await client(
            functions.channels.GetParticipantRequest(
                channel=target_entity,
                participant=user_id
            )
        )
        member = participant.participant
        if isinstance(member, (types.ChannelParticipantLeft, types.ChannelParticipantBanned)):
            return False
        return True
    except Exception as e:
        logger.warning("Membership check failed for %s: %s", user_id, e)
        return False

async def allow_participant(client, call, user):
    input_call = types.InputGroupCall(id=call.id, access_hash=call.access_hash)
    participant = await client.get_input_entity(user)
    await client(
        functions.phone.EditGroupCallParticipantRequest(
            call=input_call,
            participant=participant,
            muted=False
        )
    )

async def monitor_user(user_id):
    target_row = get_channel(user_id, "target")
    live_row = get_channel(user_id, "live")

    if not target_row or not live_row:    
        return    

    client = get_client(user_id)    

    try:    
        if not client.is_connected():    
            await client.connect()    

        if not await client.is_user_authorized():    
            return    

        call, live_entity = await get_active_call(client, live_row["channel_username"])    
        if not call:    
            return    

        target_entity = await client.get_entity(target_row["channel_username"])    
        participants, users = await get_participants(client, call)    

        for participant in participants:    
            peer = participant.peer    
            if not isinstance(peer, types.PeerUser):    
                continue    

            target_user_id = peer.user_id
            
            # সরাসরি পিয়ার আইডি দিয়ে টার্গেট চ্যানেলের মেম্বারশিপ যাচাই করা হচ্ছে
            is_member = await is_target_member(client, target_entity, target_user_id)
            if not is_member:
                continue

            muted = getattr(participant, "muted", False)    
            if not muted:    
                continue    

            try:    
                user_entity = users.get(target_user_id)
                if not user_entity:
                    user_entity = await client.get_entity(target_user_id)

                await allow_participant(client, call, user_entity)    
                logger.info(    
                    "Target member allowed: %s | target=%s | live=%s",    
                    target_user_id,    
                    target_row["channel_username"],    
                    live_row["channel_username"]    
                )    
            except Exception as e:    
                logger.warning("Allow participant failed: %s", e)    

            await asyncio.sleep(0.15)    

    except Exception as e:    
        logger.warning("Monitor error user=%s: %s", user_id, e)

async def auto_monitor():
    logger.info("Auto Speaker Monitor Started")
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
                    await monitor_user(row["owner_id"])    
                except Exception as e:    
                    logger.warning("User monitor error: %s", e)    
                await asyncio.sleep(0.5)    

        except Exception as e:    
            logger.exception(e)    

        await asyncio.sleep(3)

@router.message(F.text == "📊 Live Status")
async def status_text_handler(message: Message):
    await live_status_message(message)

@router.callback_query(F.data == "status")
async def status_callback(call: CallbackQuery):
    await call.answer()
    await live_status_message(call.message)

async def live_status_message(message):
    user_id = message.chat.id

    target_row = get_channel(user_id, "target")    
    live_row = get_channel(user_id, "live")    

    if not target_row:    
        await message.answer("❌ 🎯 Target Channel সেট করা হয়নি।")    
        return    

    if not live_row:    
        await message.answer("❌ 📺 Live Stream Channel সেট করা হয়নি।")    
        return    

    client = get_client(user_id)    

    try:    
        if not client.is_connected():  
            await client.connect()    
              
        call, live_entity = await get_active_call(client, live_row["channel_username"])    

        if not call:    
            await message.answer(    
                "🔴 <b>LIVE OFFLINE</b>\n\n"    
                f"🎯 Target: {target_row['title']}\n"    
                f"📺 Live: {live_row['title']}",    
                parse_mode="HTML"    
            )    
            return    

        participants, users = await get_participants(client, call)    
        target_entity = await client.get_entity(target_row["channel_username"])    

        members = 0    
        muted = 0    
        allowed = 0    

        for participant in participants:    
            peer = participant.peer    
            if not isinstance(peer, types.PeerUser):    
                continue    

            target_user_id = peer.user_id

            if await is_target_member(client, target_entity, target_user_id):    
                members += 1    
                if getattr(participant, "muted", False):    
                    muted += 1    
                else:    
                    allowed += 1    

        await message.answer(    
            "🎙 <b>LIVE STATUS</b>\n\n"    
            f"🎯 Target Channel:\n{target_row['title']}\n\n"    
            f"📺 Live Channel:\n{live_row['title']}\n\n"    
            f"👥 Target Members in Live: {members}\n"    
            f"🔇 Muted: {muted}\n"    
            f"🎤 Allowed: {allowed}\n\n"    
            "🟢 Monitor: ON",    
            parse_mode="HTML",    
            reply_markup=main_keyboard()    
        )    

    except Exception as e:    
        logger.exception(e)    
        await message.answer(f"❌ Status Error:\n\n{e}")

async def health_handler(request):
    return web.json_response({
        "status": "online",
        "service": "Telegram Auto Speaker",
        "time": int(time.time())
    })

async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)

    port = int(os.getenv("PORT", "10000"))    
    runner = web.AppRunner(app)    
    await runner.setup()    

    site = web.TCPSite(runner, "0.0.0.0", port)    
    await site.start()    
    logger.info("Health server running on port %s", port)

async def main():
    logger.info("Telegram Auto Speaker Starting...")
    await start_health_server()
    asyncio.create_task(auto_monitor())
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")
