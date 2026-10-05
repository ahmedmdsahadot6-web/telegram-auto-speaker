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
    BufferedInputFile,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from telethon import TelegramClient, functions, types
from telethon.errors import (
    RPCError,
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    PasswordHashInvalidError
)

import qrcode

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
            session_name TEXT,  
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

os.makedirs(
    "sessions",
    exist_ok=True
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

clients = {}
login_tasks = {}
phone_login_data = {}

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

class TargetChannelState(StatesGroup):
    waiting_channel = State()

class LiveChannelState(StatesGroup):
    waiting_channel = State()

class PhoneLoginState(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()

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
            [InlineKeyboardButton(text="ℹ️ Help", callback_data="help")]
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
        "1️⃣ 🔐 Connect Telegram করুন (QR Code অথবা Phone Number দিয়ে)\n\n"  
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
        "admin/manage-call permission থাকতে হবে।\n\n"  
        "⚠️ RTMP livestream হলে Telegram API "  
        "দিয়ে participant unmute করা যায় না।",  
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
    client = get_client(user_id)  

    try:  
        await client.connect()  
        if await client.is_user_authorized():  
            db.execute(  
                """  
                INSERT OR REPLACE INTO users  
                (user_id, session_name, connected, created_at)  
                VALUES (?, ?, 1, ?)  
                """,  
                (user_id, session_path(user_id), int(time.time()))  
            )  
            db.commit()  
            await message.answer(  
                "✅ <b>Telegram Already Connected</b>\n\n"  
                "এখন Target ও Live Channel সেট করুন।",  
                parse_mode="HTML",  
                reply_markup=main_keyboard()  
            )  
            return  

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📷 Connect via QR Code", callback_data="connect_qr")],
                [InlineKeyboardButton(text="📱 Connect via Phone Number (OTP)", callback_data="connect_phone")]
            ]
        )

        await message.answer(
            "🔐 <b>Choose Login Method</b>\n\n"
            "আপনি কোন মাধ্যমে আপনার Telegram অ্যাকাউন্ট কানেক্ট করতে চান তা নিচে থেকে সিলেক্ট করুন:",
            parse_mode="HTML",
            reply_markup=keyboard
        )

    except Exception as e:  
        logger.exception(e)  
        await message.answer(f"❌ Telegram connect করা যায়নি:\n\n{e}")

@router.callback_query(F.data == "connect_qr")
async def connect_qr_callback(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    message = call.message
    user_id = message.chat.id
    client = get_client(user_id)

    try:
        if not client.is_connected():
            await client.connect()

        login = await client.qr_login()  
        login_tasks[user_id] = login  

        qr_image = qrcode.make(login.url)  
        buffer = io.BytesIO()  
        qr_image.save(buffer, format="PNG")  
        buffer.seek(0)  

        photo = BufferedInputFile(  
            buffer.read(),  
            filename="telegram_login_qr.png"  
        )  

        keyboard = InlineKeyboardMarkup(  
            inline_keyboard=[  
                [InlineKeyboardButton(text="📱 Connect via Telegram", url=login.url)],  
                [InlineKeyboardButton(text="🔄 Check Connection", callback_data="check_connection")]  
            ]  
        )  

        await message.answer_photo(  
            photo=photo,  
            caption=(  
                "🔐 <b>Connect via QR Code</b>\n\n"  
                "পদ্ধতি ১:\n"  
                "নিচের <b>Connect via Telegram</b> button চাপুন。\n\n"  
                "অথবা\n\n"  
                "পদ্ধতি ২:\n"  
                "Telegram → Settings → Devices → Link Desktop Device → QR Scan করুন।"  
            ),  
            parse_mode="HTML",  
            reply_markup=keyboard  
        )  

        await message.answer(  
            "⏳ <b>Connection অপেক্ষা করছে...</b>\n\n"  
            "Telegram login সম্পন্ন করলে automatically connected হবে।",  
            parse_mode="HTML"  
        )  

        asyncio.create_task(wait_for_qr_login(user_id))

    except Exception as e:
        logger.exception(e)
        await message.answer(f"❌ QR Login শুরু করা যায়নি:\n\n{e}")

async def wait_for_qr_login(user_id):
    try:  
        login = login_tasks.get(user_id)  
        if not login:  
            return  

        client = get_client(user_id)  

        try:  
            await login.wait(timeout=180)  
        except SessionPasswordNeededError:  
            await bot.send_message(  
                user_id,  
                "⚠️ আপনার Telegram account-এ 2-Step Verification চালু আছে。\n\n"  
                "দয়া করে Phone Number Login পদ্ধতি ব্যবহার করুন অথবা 2FA password দিন।"  
            )  
            return  

        db.execute(  
            """  
            INSERT OR REPLACE INTO users  
            (user_id, session_name, connected, created_at)  
            VALUES (?, ?, 1, ?)  
            """,  
            (user_id, session_path(user_id), int(time.time()))  
        )  
        db.commit()  

        await bot.send_message(  
            user_id,  
            "✅ <b>Telegram Connected!</b>\n\n"  
            "এখন প্রথমে 🎯 Target Channel সেট করুন।",  
            parse_mode="HTML",  
            reply_markup=main_keyboard()  
        )  

    except Exception as e:  
        logger.exception(e)  
        try:  
            await bot.send_message(user_id, f"❌ Login failed:\n\n{e}")  
        except:  
            pass  
    finally:  
        login_tasks.pop(user_id, None)

@router.callback_query(F.data == "connect_phone")
async def connect_phone_callback(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(PhoneLoginState.waiting_phone)
    await call.message.answer(
        "📱 <b>Phone Number দিয়ে লগইন</b>\n\n"
        "আপনার Telegram অ্যাকাউন্টের ফোন নাম্বারটি কান্ট্রি কোডসহ পাঠান (যেমন: <code>+8801712345678</code>)",
        parse_mode="HTML"
    )

@router.message(PhoneLoginState.waiting_phone)
async def process_phone_number(message: Message, state: FSMContext):
    phone = message.text.strip()
    user_id = message.chat.id
    client = get_client(user_id)

    try:
        if not client.is_connected():
            await client.connect()

        sent_code = await client.send_code_request(phone)
        
        phone_login_data[user_id] = {
            "phone": phone,
            "phone_code_hash": sent_code.phone_code_hash
        }

        await state.set_state(PhoneLoginState.waiting_code)
        await message.answer(
            "📩 আপনার Telegram অ্যাপে একটি কোড (OTP) পাঠানো হয়েছে।\n\n"
            "কোডটি এখানে দিন (উদাহরণস্বরূপ: <code>12345</code>):",
            parse_mode="HTML"
        )

    except PhoneNumberInvalidError:
        await message.answer("❌ ফোন নাম্বারটি সঠিক নয়। দয়া করে সঠিক ফরম্যাটে আবার পাঠান (যেমন: <code>+8801712345678</code>):", parse_mode="HTML")
    except Exception as e:
        logger.exception(e)
        await message.answer(f"❌ কোড পাঠানো যায়নি:\n\n{e}")
        await state.clear()

@router.message(PhoneLoginState.waiting_code)
async def process_phone_code(message: Message, state: FSMContext):
    code = message.text.strip().replace(" ", "")
    user_id = message.chat.id
    client = get_client(user_id)
    data = phone_login_data.get(user_id)

    if not data:
        await message.answer("❌ সেশন মেয়াদোত্তীর্ণ হয়ে গেছে। আবার চেষ্টা করুন।", reply_markup=main_keyboard())
        await state.clear()
        return

    try:
        await client.sign_in(
            phone=data["phone"],
            code=code,
            phone_code_hash=data["phone_code_hash"]
        )

        db.execute(
            """  
            INSERT OR REPLACE INTO users  
            (user_id, session_name, connected, created_at)  
            VALUES (?, ?, 1, ?)  
            """,
            (user_id, session_path(user_id), int(time.time()))
        )
        db.commit()
        await state.clear()
        phone_login_data.pop(user_id, None)

        await message.answer(
            "✅ <b>Telegram Connected Successfully!</b>\n\n"
            "এখন Target ও Live Channel সেট করুন。",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )

    except SessionPasswordNeededError:
        await state.set_state(PhoneLoginState.waiting_password)
        await message.answer(
            "🔐 আপনার অ্যাকাউন্টে <b>2-Step Verification (2FA)</b> চালু আছে。\n\n"
            "দয়া করে আপনার ক্লাউড পাসওয়ার্ডটি (2FA Password) এখানে পাঠান:",
            parse_mode="HTML"
        )
    except PhoneCodeInvalidError:
        await message.answer("❌ কোডটি ভুল হয়েছে। সঠিক কোডটি আবার দিন:")
    except Exception as e:
        logger.exception(e)
        await message.answer(f"❌ লগইন ব্যর্থ হয়েছে:\n\n{e}")
        await state.clear()
        phone_login_data.pop(user_id, None)

@router.message(PhoneLoginState.waiting_password)
async def process_2fa_password(message: Message, state: FSMContext):
    password = message.text.strip()
    user_id = message.chat.id
    client = get_client(user_id)

    try:
        await client.sign_in(password=password)

        db.execute(
            """  
            INSERT OR REPLACE INTO users  
            (user_id, session_name, connected, created_at)  
            VALUES (?, ?, 1, ?)  
            """,
            (user_id, session_path(user_id), int(time.time()))
        )
        db.commit()
        await state.clear()
        phone_login_data.pop(user_id, None)

        await message.answer(
            "✅ <b>2FA Password দিয়ে Telegram Connected!</b>\n\n"
            "এখন Target ও Live Channel সেট করুন।",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )

    except PasswordHashInvalidError:
        await message.answer("❌ 2FA পাসওয়ার্ড ভুল হয়েছে। সঠিক পাসওয়ার্ডটি আবার দিন:")
    except Exception as e:
        logger.exception(e)
        await message.answer(f"❌ লগইন সম্পন্ন করা যায়নি:\n\n{e}")
        await state.clear()
        phone_login_data.pop(user_id, None)

@router.callback_query(F.data == "check_connection")
async def check_connection(call: CallbackQuery):
    await call.answer()  
    user_id = call.from_user.id  
    client = get_client(user_id)  

    try:  
        await client.connect()  
        if await client.is_user_authorized():  
            db.execute(  
                """  
                INSERT OR REPLACE INTO users  
                (user_id, session_name, connected, created_at)  
                VALUES (?, ?, 1, ?)  
                """,  
                (user_id, session_path(user_id), int(time.time()))  
            )  
            db.commit()  
            await call.message.answer("✅ Telegram Connected!", reply_markup=main_keyboard())  
        else:  
            await call.message.answer("⏳ এখনও Connected হয়নি。\n\nআগে QR Scan বা Connect button ব্যবহার করুন।")  
    except Exception as e:  
        await call.message.answer(f"❌ Connection check failed:\n{e}")

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
            "এই Channel-এর সদস্যদের Live-এ শনাক্ত করা হবে。\n\n"  
            "পরিবর্তন করতে নতুন @username পাঠান।",  
            parse_mode="HTML"  
        )  
    else:  
        await message.answer(  
            "🎯 <b>Target Channel</b>\n\n"  
            "যে Channel-এর সদস্যদের Live-এ Allow to Speak করতে চান সেই Channel-এর username পাঠান。\n\n"  
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
            "যে Channel-এ Telegram Live চলবে সেই Channel-এর username পাঠান。\n\n"  
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
    result = await client(functions.phone.GetGroupCallRequest(call=input_call, limit=100, offset=0))  
    users = {user.id: user for user in result.users}  
    return result.participants, users

async def is_target_member(client, target_entity, user):
    try:  
        participant = await client(  
            functions.channels.GetParticipantRequest(  
                channel=target_entity,  
                participant=types.InputPeerUser(user_id=user.id, access_hash=user.access_hash)  
            )  
        )  
        member = participant.participant  
        if isinstance(member, (types.ChannelParticipantLeft, types.ChannelParticipantBanned)):  
            return False  
        return True  
    except Exception as e:  
        logger.warning("Membership check failed for %s: %s", user.id, e)  
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

    if not client.is_connected():  
        await client.connect()  

    if not await client.is_user_authorized():  
        return  

    try:  
        call, live_entity = await get_active_call(client, live_row["channel_username"])  
        if not call:  
            return  

        target_entity = await client.get_entity(target_row["channel_username"])  
        participants, users = await get_participants(client, call)  

        for participant in participants:  
            peer = participant.peer  
            if not isinstance(peer, types.PeerUser):  
                continue  

            user = users.get(peer.user_id)  
            if not user:  
                continue  

            member = await is_target_member(client, target_entity, user)  
            if not member:  
                continue  

            muted = getattr(participant, "muted", False)  
            if not muted:  
                continue  

            try:  
                await allow_participant(client, call, user)  
                logger.info(  
                    "Target member allowed: %s | target=%s | live=%s",  
                    user.id,  
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

            user = users.get(peer.user_id)  
            if not user:  
                continue  

            if await is_target_member(client, target_entity, user):  
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

@router.callback_query(F.data == "back")
async def back_handler(call: CallbackQuery):
    await call.answer()  
    await call.message.answer(  
        "🎙 <b>TELEGRAM AUTO SPEAKER</b>\n\nControl Panel:",  
        parse_mode="HTML",  
        reply_markup=main_menu()  
    )

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
