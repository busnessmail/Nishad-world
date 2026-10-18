import base64
import ctypes
import json
import logging
import os
import shutil
import sqlite3
import threading
import asyncio
import re
import sys
import time
from getpass import getpass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4
from flask import Flask, request
from telegram import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Update,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    filters,
)
import requests

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

LOCAL_SETTINGS_PATH = Path(__file__).resolve().with_name("local_settings.dat")
DATA_DIR = Path(__file__).resolve().parent / "data"
DATABASE_PATH = DATA_DIR / "bot_data.sqlite3"
DATABASE_BACKUP_PATH = DATA_DIR / "bot_data_backup.sqlite3"

def _prompt_runtime_setting(name: str, label: str, secret: bool = False, optional: bool = False):
    value = os.getenv(name, "").strip()
    if value:
        return value
    if not sys.stdin.isatty():
        if optional:
            return ""
        raise SystemExit(f"Missing required setting {name}.")
    while True:
        value = (getpass(label) if secret else input(label)).strip()
        if value:
            os.environ[name] = value
            return value
        if optional:
            return ""

def _valid_telegram_bot_token(value: str) -> bool:
    return re.fullmatch(r"[0-9]{5,15}:[A-Za-z0-9_-]{20,}", value) is not None

def _prompt_telegram_bot_token():
    value = os.getenv("BOT_TOKEN", "").strip()
    while True:
        if _valid_telegram_bot_token(value):
            os.environ["BOT_TOKEN"] = value
            return value
        if value:
            os.environ.pop("BOT_TOKEN", None)
        if not sys.stdin.isatty():
            raise SystemExit("Invalid BOT_TOKEN.")
        value = getpass("Telegram bot token (hidden): ").strip()

def _dpapi_transform(data: bytes, encrypt: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI only.")
    from ctypes import wintypes
    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]
    crypt32 = ctypes.WinDLL("crypt32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    input_buffer = ctypes.create_string_buffer(data)
    input_blob = DataBlob(len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output_blob = DataBlob()
    if encrypt:
        succeeded = crypt32.CryptProtectData(ctypes.byref(input_blob), "Bot", None, None, None, 0, ctypes.byref(output_blob))
    else:
        succeeded = crypt32.CryptUnprotectData(ctypes.byref(input_blob), None, None, None, None, 0, ctypes.byref(output_blob))
    if not succeeded:
        raise OSError("DPAPI failed.")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))

def _save_local_settings():
    settings = {
        "version": 2,
        "vak_sms_api_key": os.getenv("VAK_SMS_API_KEY", "").strip(),
        "subscription_5_bdt": os.getenv("ZINIPAY_SUBSCRIPTION_5_BDT", "").strip(),
        "subscription_7_bdt": os.getenv("ZINIPAY_SUBSCRIPTION_7_BDT", "").strip(),
        "public_base_url": os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/"),
    }
    encrypted = _dpapi_transform(json.dumps(settings).encode("utf-8"), encrypt=True)
    LOCAL_SETTINGS_PATH.write_text(base64.b64encode(encrypted).decode("ascii"), encoding="ascii")

def _load_local_settings() -> bool:
    if not LOCAL_SETTINGS_PATH.exists():
        return False
    try:
        encrypted = base64.b64decode(LOCAL_SETTINGS_PATH.read_text(encoding="ascii").strip(), validate=True)
        settings = json.loads(_dpapi_transform(encrypted, encrypt=False).decode("utf-8"))
        os.environ["VAK_SMS_API_KEY"] = settings.get("vak_sms_api_key", "")
        os.environ["ZINIPAY_SUBSCRIPTION_5_BDT"] = settings["subscription_5_bdt"]
        os.environ["ZINIPAY_SUBSCRIPTION_7_BDT"] = settings["subscription_7_bdt"]
        os.environ["PUBLIC_BASE_URL"] = settings["public_base_url"]
        return True
    except Exception:
        return False

def prompt_runtime_settings():
    if not sys.stdin.isatty():
        _prompt_telegram_bot_token()
        return
    if not _load_local_settings():
        try:
            _prompt_runtime_setting("VAK_SMS_API_KEY", "VAK SMS API key: ", secret=True, optional=True)
            _save_local_settings()
        except Exception:
            pass
    _prompt_telegram_bot_token()

BD_TIMEZONE = timezone(timedelta(hours=6))

def get_bd_time():
    return datetime.now(BD_TIMEZONE)

def get_bd_date_str():
    return get_bd_time().strftime("%Y-%m-%d")

prompt_runtime_settings()
BOT_TOKEN = os.getenv("BOT_TOKEN")
VAK_SMS_API_KEY = os.getenv("VAK_SMS_API_KEY")

MAIN_ADMIN_ID = 7259690721
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = list(dict.fromkeys(
    [MAIN_ADMIN_ID] +
    [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()]
))

OTP_GROUP_ID = os.getenv("OTP_GROUP_ID")
BINANCE_ID = os.getenv("BINANCE_ID", "1102671249")
ZINIPAY_API_KEY = os.getenv("ZINIPAY_API_KEY")
ZINIPAY_API_BASE = os.getenv("ZINIPAY_API_BASE", "https://api.zinipay.com").rstrip("/")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
ZINIPAY_REDIRECT_URL = os.getenv("ZINIPAY_REDIRECT_URL", "").strip()
ZINIPAY_CANCEL_URL = os.getenv("ZINIPAY_CANCEL_URL", "").strip()
ZINIPAY_WEBHOOK_URL = os.getenv("ZINIPAY_WEBHOOK_URL", "").strip()

def _json_default(value):
    if isinstance(value, datetime):
        return {"__local_bot_datetime__": value.isoformat()}
    raise TypeError(f"Unsupported local database value: {type(value).__name__}")

def _json_object_hook(value):
    if set(value) == {"__local_bot_datetime__"}:
        return datetime.fromisoformat(value["__local_bot_datetime__"])
    return value

def _encode_document(document) -> str:
    return json.dumps(document, ensure_ascii=False, default=_json_default, separators=(",", ":"))

def _decode_document(payload: str):
    return json.loads(payload, object_hook=_json_object_hook)

def _get_nested(document, path, default=None):
    value = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value

def _set_nested(document, path, value):
    parts = path.split(".")
    target = document
    for part in parts[:-1]:
        nested = target.get(part)
        if not isinstance(nested, dict):
            nested = {}
            target[part] = nested
        target = nested
    target[parts[-1]] = value

class LocalCursor:
    def __init__(self, documents):
        self.documents = documents
    def sort(self, key, direction=1):
        self.documents.sort(key=lambda item: (_get_nested(item, key) is None, _get_nested(item, key)), reverse=direction < 0)
        return self
    def limit(self, count):
        self.documents = self.documents[:count]
        return self
    def __iter__(self):
        return iter(self.documents)

class LocalUpdateResult:
    def __init__(self, matched_count=0, modified_count=0, upserted_id=None):
        self.matched_count = matched_count
        self.modified_count = modified_count
        self.upserted_id = upserted_id

class LocalCollection:
    def __init__(self, database, name):
        self.database = database
        self.name = name
        self.key_field = {
            "users": "user_id",
            "settings": "type",
            "pending_deposits": "dep_id",
            "subscription_payments": "invoice_id",
            "daily_stats": "date",
        }[name]

    def _document_key(self, document):
        return json.dumps(document.get(self.key_field), ensure_ascii=False, default=_json_default)

    def _matches(self, document, query):
        for field, expected in (query or {}).items():
            if _get_nested(document, field) != expected:
                return False
        return True

    def _project(self, document, projection):
        return document

    def find_one(self, query=None, projection=None):
        with self.database.lock:
            rows = self.database.connection.execute("SELECT payload FROM bot_documents WHERE collection = ?", (self.name,)).fetchall()
        for (payload,) in rows:
            document = _decode_document(payload)
            if self._matches(document, query or {}):
                return document
        return None

    def find(self, query=None, projection=None):
        with self.database.lock:
            rows = self.database.connection.execute("SELECT payload FROM bot_documents WHERE collection = ?", (self.name,)).fetchall()
        documents = [_decode_document(payload) for (payload,) in rows if self._matches(_decode_document(payload), query or {})]
        return LocalCursor(documents)

    def insert_one(self, document):
        new_document = dict(document)
        new_document.setdefault("_id", str(uuid4()))
        doc_key = self._document_key(new_document)
        with self.database.lock:
            self.database.connection.execute("INSERT OR REPLACE INTO bot_documents(collection, doc_key, payload) VALUES (?, ?, ?)", (self.name, doc_key, _encode_document(new_document)))
        return type("InsertOneResult", (), {"inserted_id": new_document["_id"]})()

    def update_one(self, query, update, upsert=False):
        with self.database.lock:
            rows = self.database.connection.execute("SELECT doc_key, payload FROM bot_documents WHERE collection = ?", (self.name,)).fetchall()
            for doc_key, payload in rows:
                document = _decode_document(payload)
                if self._matches(document, query or {}):
                    for field, val in update.get("$set", {}).items():
                        _set_nested(document, field, val)
                    self.database.connection.execute("UPDATE bot_documents SET payload = ? WHERE collection = ? AND doc_key = ?", (_encode_document(document), self.name, doc_key))
                    return LocalUpdateResult(1, 1)
            if upsert:
                document = {k: v for k, v in query.items()}
                for field, val in update.get("$set", {}).items():
                    _set_nested(document, field, val)
                doc_key = self._document_key(document)
                self.database.connection.execute("INSERT INTO bot_documents(collection, doc_key, payload) VALUES (?, ?, ?)", (self.name, doc_key, _encode_document(document)))
                return LocalUpdateResult(0, 0, "1")
        return LocalUpdateResult()

    def update_many(self, query, update):
        return LocalUpdateResult(1, 1)

class LocalDatabase:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(str(DATABASE_PATH), timeout=30, check_same_thread=False, isolation_level=None)
        with self.lock:
            self.connection.execute("CREATE TABLE IF NOT EXISTS bot_documents (collection TEXT NOT NULL, doc_key TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(collection, doc_key))")

    def __getitem__(self, name):
        return LocalCollection(self, name)

    def create_backup(self):
        pass

db = LocalDatabase()
users_col = db["users"]
settings_col = db["settings"]
deposits_col = db["pending_deposits"]
subscription_payments_col = db["subscription_payments"]
daily_stats_col = db["daily_stats"]
subscription_activation_lock = threading.Lock()

def emoji(emoji_id: str, fallback: str = "✨") -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

E_WELCOME = emoji("5352838545826420397", "👋")
E_PHONE = emoji("5269759232483303288", "📱")
E_ID = emoji("5841276284155467413", "🆔")
E_WAIT_OTP = emoji("5215522595922779944", "⏳")
E_PAY_SELECT = emoji("5256186332669035163", "💳")
E_BINANCE = emoji("5348212415077064131", "💛")
E_CANCEL = emoji("5240241223632954241", "🚫")
E_BAN = emoji("5240241223632954241", "🚫")
E_UNBAN = emoji("5206607081334906820", "✅")
E_ADMIN_HEADER = emoji("5424818078833715060", "🛠")

flask_app = Flask("")
@flask_app.route("/")
def home():
    return "Bot is Active!", 200

# Force Join Database Management
def get_force_join_channel():
    doc = settings_col.find_one({"type": "force_join"})
    return doc.get("channel", "") if doc else ""

def set_force_join_channel(channel: str):
    settings_col.update_one({"type": "force_join"}, {"$set": {"channel": channel}}, upsert=True)

async def check_force_join(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    channel = get_force_join_channel()
    if not channel:
        return True
    try:
        member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
        if member.status in ["member", "administrator", "creator"]:
            return True
    except Exception:
        pass
    return False

def get_zinipay_subscription_price(days: int):
    raw_amount = os.getenv("ZINIPAY_SUBSCRIPTION_5_BDT" if days == 5 else "ZINIPAY_SUBSCRIPTION_7_BDT", "").strip()
    try:
        return Decimal(raw_amount) if raw_amount else None
    except:
        return None

def create_zinipay_subscription_invoice(user_id: int, full_name: str, days: int):
    amount = get_zinipay_subscription_price(days)
    if not ZINIPAY_API_KEY or not amount:
        return None
    order_id = uuid4().hex
    payload = {
        "cus_name": full_name or str(user_id),
        "amount": float(amount),
        "metadata": {"order_id": order_id, "user_id": str(user_id), "subscription_days": days},
        "redirect_url": PUBLIC_BASE_URL,
        "cancel_url": PUBLIC_BASE_URL,
        "webhook_url": f"{PUBLIC_BASE_URL}/zinipay/subscription-webhook",
    }
    try:
        response = requests.post(f"{ZINIPAY_API_BASE}/v1/payment/create", headers={"zini-api-key": ZINIPAY_API_KEY}, json=payload, timeout=20)
        result = response.json()
        if response.ok and result.get("payment_url"):
            return {"payment_url": result["payment_url"], "amount": amount}
    except:
        pass
    return None

def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

active_orders = {}
WAITING_AMOUNT, WAITING_TXID, WAITING_SCREENSHOT = range(3)
ADMIN_BAN, ADMIN_UNBAN, ADMIN_ADD_BAL_USER, ADMIN_ADD_BAL_AMT, ADMIN_SET_FORCE_CHANNEL = range(6, 11)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def get_user(user_id: int):
    return users_col.find_one({"user_id": user_id})

def get_or_create_user(user_id: int, full_name: str = "User"):
    user = users_col.find_one({"user_id": user_id})
    if not user:
        user_data = {"user_id": user_id, "full_name": full_name, "balance": 0.0, "otp_count": 0, "selected_country": "cl", "is_banned": False, "subscription_expiry": None}
        users_col.insert_one(user_data)
        return user_data
    return user

def is_subscribed(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    user = get_user(user_id)
    if user and user.get("subscription_expiry"):
        if get_bd_time().replace(tzinfo=None) < user["subscription_expiry"]:
            return True
    return False

def get_rate(country: str = "cl"):
    return 0.07 if country == "hk" else 0.10

def get_main_keyboard(user_id):
    keyboard = [
        [KeyboardButton("💳 𝙰𝙲𝙲𝙾𝚄𝙽𝚃 𝙱𝙰𝙻𝙰𝙽𝙲𝙴"), KeyboardButton("🛒 𝙱𝚈 𝙽𝚄𝙼𝙱𝙴𝚁")],
        [KeyboardButton("🌐 𝚂𝙴𝙻𝙴𝙲𝚃 𝙲𝙾𝚄𝙽𝚃𝚁𝚈"), KeyboardButton("👤 𝙼𝚈 𝙿𝚁𝙾𝙵𝙸𝙻𝙴")],
        [KeyboardButton("💵 𝙳𝙸𝙿𝙾𝚂𝙸𝚃")]
    ]
    if is_admin(user_id):
        keyboard.append([KeyboardButton("⚙️ 𝙰𝙳𝙼𝙸𝙽 𝙿𝙰𝙽𝙴𝙻")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u_data = get_or_create_user(user.id, user.full_name)

    if u_data.get("is_banned", False):
        return

    channel = get_force_join_channel()
    if channel and not await check_force_join(user.id, context):
        join_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{channel.replace('@', '')}")],
            [InlineKeyboardButton("🔄 Joined / Try Again", callback_data="check_join")]
        ])
        await update.message.reply_text(f"⚠️ <b>Bot bebohar korte hole obosshoi amader channel-এ join korte hobe!</b>", parse_mode="HTML", reply_markup=join_kb)
        return

    if not is_subscribed(user.id):
        sub_kb = InlineKeyboardMarkup([[InlineKeyboardButton("💛 5 Days", callback_data="buy_sub_5")], [InlineKeyboardButton("💛 7 Days", callback_data="buy_sub_7")]])
        await update.message.reply_text(f"👋 Hello {user.full_name}!\n\n{E_BAN} <b>SUBSCRIPTION REQUIRED!</b>", parse_mode="HTML", reply_markup=sub_kb)
        return

    await update.message.reply_text(f"👋 <b>WELCOME WHATSAPP BOT!</b> {E_WELCOME}", parse_mode="HTML", reply_markup=get_main_keyboard(user.id))

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    u_data = get_or_create_user(user_id, update.effective_user.full_name)

    if u_data.get("is_banned", False) or not is_subscribed(user_id):
        return

    channel = get_force_join_channel()
    if channel and not await check_force_join(user_id, context):
        join_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{channel.replace('@', '')}")],
            [InlineKeyboardButton("🔄 Joined / Try Again", callback_data="check_join")]
        ])
        await update.message.reply_text("⚠️ <b>Channel join kora obosshoi dorkar!</b>", parse_mode="HTML", reply_markup=join_kb)
        return

    if text == "⚙️ 𝙰𝙳𝙼𝙸𝙽 𝙿𝙰𝙽𝙴𝙻" and is_admin(user_id):
        admin_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 SET FORCE JOIN CHANNEL", callback_data="admin_set_channel")],
            [InlineKeyboardButton("🚫 BAN USER", callback_data="admin_ban"), InlineKeyboardButton("✅ UNBAN USER", callback_data="admin_unban")]
        ])
        await update.message.reply_text(f"{E_ADMIN_HEADER} <b>𝙰𝙳𝙼𝙸𝙽 𝙲𝙾𝙽𝚃𝚁𝙾𝙻 𝙿𝙰𝙽𝙴𝙻</b>", parse_mode="HTML", reply_markup=admin_kb)

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "check_join":
        if await check_force_join(user_id, context):
            await query.message.edit_text("✅ Dhonnobad channel-এ join korar jonno!", parse_mode="HTML")
            await context.bot.send_message(chat_id=user_id, text="👇 <b>MAIN MENU:</b>", parse_mode="HTML", reply_markup=get_main_keyboard(user_id))
        else:
            await query.answer("❌ Apni abow o channel-এ join koren nai!", show_alert=True)
        return

    if is_admin(user_id) and data == "admin_set_channel":
        curr_ch = get_force_join_channel() or "None"
        await query.message.reply_text(f"📢 Current Force Join Channel: <code>{curr_ch}</code>\n\nNotun channel username (e.g. @mychannel) ba group ID pathan:", parse_mode="HTML")
        return ADMIN_SET_FORCE_CHANNEL

async def admin_set_channel_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ch_text = update.message.text.strip()
    set_force_join_channel(ch_text)
    await update.message.reply_text(f"✅ Force Join Channel successfully set to: <code>{ch_text}</code>", parse_mode="HTML")
    return ConversationHandler.END

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()

    admin_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_callback_query, pattern="^admin_set_channel$")],
        states={ADMIN_SET_FORCE_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_channel_process)]},
        fallbacks=[]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(admin_handler)
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.Command, handle_messages))

    logging.info("Starting Telegram Bot...")
    app.run_polling()

if __name__ == "__main__":
    main()
