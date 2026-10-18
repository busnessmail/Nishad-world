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

# Logging Configuration
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
        raise SystemExit(
            f"Missing required setting {name}. Set it in the process environment "
            "or run from an interactive terminal."
        )

    while True:
        value = (getpass(label) if secret else input(label)).strip()
        if value:
            os.environ[name] = value
            return value
        if optional:
            return ""
        print(f"{name} is required.")

def _valid_telegram_bot_token(value: str) -> bool:
    return re.fullmatch(r"[0-9]{5,15}:[A-Za-z0-9_-]{20,}", value) is not None

def _prompt_telegram_bot_token():
    value = os.getenv("BOT_TOKEN", "").strip()
    while True:
        if _valid_telegram_bot_token(value):
            os.environ["BOT_TOKEN"] = value
            return value
        if value:
            print(
                "Telegram bot token has an invalid format or hidden character. "
                "Re-enter it without Ctrl+V; type it or use right-click paste."
            )
            os.environ.pop("BOT_TOKEN", None)
        if not sys.stdin.isatty():
            raise SystemExit(
                "Invalid BOT_TOKEN. Re-enter the token in an interactive terminal."
            )
        value = getpass("Telegram bot token (hidden): ").strip()

def _dpapi_transform(data: bytes, encrypt: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI is only available on Windows.")

    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    crypt32 = ctypes.WinDLL("crypt32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    input_buffer = ctypes.create_string_buffer(data)
    input_blob = DataBlob(
        len(data),
        ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    output_blob = DataBlob()

    if encrypt:
        operation = crypt32.CryptProtectData
        operation.argtypes = [
            ctypes.POINTER(DataBlob), wintypes.LPCWSTR, ctypes.POINTER(DataBlob),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(DataBlob),
        ]
        operation.restype = wintypes.BOOL
        succeeded = operation(
            ctypes.byref(input_blob), "Telegram bot local settings",
            None, None, None, 0, ctypes.byref(output_blob),
        )
    else:
        operation = crypt32.CryptUnprotectData
        operation.argtypes = [
            ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.POINTER(DataBlob),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(DataBlob),
        ]
        operation.restype = wintypes.BOOL
        succeeded = operation(
            ctypes.byref(input_blob), None, None, None, None, 0,
            ctypes.byref(output_blob),
        )

    if not succeeded:
        raise OSError("Windows could not protect or unprotect the local settings.")
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
    encrypted = _dpapi_transform(
        json.dumps(settings, separators=(",", ":")).encode("utf-8"),
        encrypt=True,
    )
    temporary_path = LOCAL_SETTINGS_PATH.with_suffix(".tmp")
    temporary_path.write_text(
        base64.b64encode(encrypted).decode("ascii"),
        encoding="ascii",
    )
    os.replace(temporary_path, LOCAL_SETTINGS_PATH)

def _load_local_settings() -> bool:
    if not LOCAL_SETTINGS_PATH.exists():
        return False
    try:
        encrypted = base64.b64decode(
            LOCAL_SETTINGS_PATH.read_text(encoding="ascii").strip(),
            validate=True,
        )
        settings = json.loads(_dpapi_transform(encrypted, encrypt=False).decode("utf-8"))
        version = settings.get("version")
        if version not in (1, 2):
            raise ValueError("Unsupported settings version.")
        vak_key = settings.get("vak_sms_api_key", "")
        if vak_key:
            os.environ["VAK_SMS_API_KEY"] = vak_key
        else:
            os.environ.pop("VAK_SMS_API_KEY", None)
        os.environ["ZINIPAY_SUBSCRIPTION_5_BDT"] = settings["subscription_5_bdt"]
        os.environ["ZINIPAY_SUBSCRIPTION_7_BDT"] = settings["subscription_7_bdt"]
        os.environ["PUBLIC_BASE_URL"] = settings["public_base_url"]
        if version == 1:
            _save_local_settings()
        return True
    except Exception:
        raise SystemExit(
            "Saved local settings could not be loaded or upgraded. Run START.bat with "
            "the same Windows account, or remove local_settings.dat to configure again."
        )

def _local_settings_are_available() -> bool:
    try:
        for key in ("ZINIPAY_SUBSCRIPTION_5_BDT", "ZINIPAY_SUBSCRIPTION_7_BDT"):
            amount = Decimal(os.getenv(key, "").strip())
            if not amount.is_finite() or amount <= 0:
                return False
    except (InvalidOperation, ValueError):
        return False
    parsed = urlparse(os.getenv("PUBLIC_BASE_URL", "").strip())
    return parsed.scheme == "https" and bool(parsed.netloc)

def _setup_local_settings():
    if not sys.stdin.isatty():
        raise SystemExit("Run START.bat interactively for the one-time local setup.")
    print(
        "\nOne-time setup: enter the remaining settings once. "
        "They will be encrypted for this Windows account and kept out of the ZIP."
    )
    _prompt_runtime_setting(
        "VAK_SMS_API_KEY",
        "VAK SMS API key (hidden; Enter skips number-buying features): ",
        secret=True,
        optional=True,
    )
    _prompt_positive_amount(
        "ZINIPAY_SUBSCRIPTION_5_BDT", "5-day subscription price in BDT: "
    )
    _prompt_positive_amount(
        "ZINIPAY_SUBSCRIPTION_7_BDT", "7-day subscription price in BDT: "
    )
    _prompt_public_https_url()
    try:
        _save_local_settings()
    except Exception:
        raise SystemExit(
            "Could not save encrypted local settings. Run START.bat on Windows "
            "from a writable folder."
        )
    print("One-time settings saved securely for this Windows account.")

def _prompt_positive_amount(name: str, label: str):
    value = os.getenv(name, "").strip()
    while True:
        if value:
            try:
                amount = Decimal(value)
                if amount.is_finite() and amount > 0:
                    os.environ[name] = str(amount)
                    return
            except InvalidOperation:
                pass
            print("Enter a valid amount greater than zero.")
            os.environ.pop(name, None)
        if not sys.stdin.isatty():
            return
        value = input(label).strip()

def _prompt_public_https_url():
    value = os.getenv("PUBLIC_BASE_URL", "").strip()
    while True:
        if value and urlparse(value).scheme == "https" and urlparse(value).netloc:
            os.environ["PUBLIC_BASE_URL"] = value.rstrip("/")
            return
        if value:
            print("Enter a public HTTPS URL, such as https://your-domain.example.")
            os.environ.pop("PUBLIC_BASE_URL", None)
        if not sys.stdin.isatty():
            return
        value = input("Public HTTPS URL (must match your ZiniPay Brand domain): ").strip()

def prompt_runtime_settings():
    saved_settings = _load_local_settings()
    if saved_settings and not _local_settings_are_available():
        raise SystemExit("Saved local settings are incomplete or invalid; remove local_settings.dat and set up again.")
    if not saved_settings and not _local_settings_are_available():
        _setup_local_settings()

    if not sys.stdin.isatty():
        _prompt_telegram_bot_token()
        _prompt_runtime_setting("ZINIPAY_API_KEY", "", secret=True)
        return

    print("\nEnter this run's two keys. Secret input stays hidden and is not saved.")
    _prompt_telegram_bot_token()
    _prompt_runtime_setting(
        "ZINIPAY_API_KEY",
        "ZiniPay Brand API key (hidden; press Enter to skip checkout): ",
        secret=True,
        optional=True,
    )

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

# Force Join Channel/Group Username or ID (এখানে আপনার চ্যানেল বা গ্রুপের ইউজারনেম দিন, যেমন "@your_channel")
FORCE_JOIN_CHANNEL = os.getenv("FORCE_JOIN_CHANNEL", "").strip()

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
        if isinstance(key, list):
            for field, order in reversed(key):
                self.documents.sort(
                    key=lambda item: (_get_nested(item, field) is None, _get_nested(item, field)),
                    reverse=order < 0,
                )
        else:
            self.documents.sort(
                key=lambda item: (_get_nested(item, key) is None, _get_nested(item, key)),
                reverse=direction < 0,
            )
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
        value = document.get(self.key_field)
        if value is None:
            raise ValueError(f"Local document is missing its {self.key_field} key.")
        return json.dumps(value, ensure_ascii=False, default=_json_default)

    def _matches(self, document, query):
        for field, expected in (query or {}).items():
            actual = _get_nested(document, field)
            if isinstance(expected, dict) and any(str(k).startswith("$") for k in expected):
                for operator, operand in expected.items():
                    if operator == "$ne":
                        if actual == operand or (isinstance(actual, list) and operand in actual):
                            return False
                    elif operator == "$eq":
                        if actual != operand:
                            return False
                    elif operator == "$exists":
                        if (actual is not None) != bool(operand):
                            return False
                    else:
                        raise NotImplementedError(f"Local query operator {operator} is not supported.")
            elif isinstance(actual, list):
                if expected not in actual:
                    return False
            elif actual != expected:
                return False
        return True

    def _project(self, document, projection):
        if not projection or not any(bool(value) for value in projection.values()):
            return document
        projected = {}
        for field, include in projection.items():
            if include:
                value = _get_nested(document, field)
                if value is not None:
                    _set_nested(projected, field, value)
        return projected

    def find_one(self, query=None, projection=None):
        with self.database.lock:
            rows = self.database.connection.execute(
                "SELECT payload FROM bot_documents WHERE collection = ?",
                (self.name,),
            ).fetchall()
        for (payload,) in rows:
            document = _decode_document(payload)
            if self._matches(document, query or {}):
                return self._project(document, projection)
        return None

    def find(self, query=None, projection=None):
        with self.database.lock:
            rows = self.database.connection.execute(
                "SELECT payload FROM bot_documents WHERE collection = ?",
                (self.name,),
            ).fetchall()
        documents = [
            self._project(document, projection)
            for (payload,) in rows
            if self._matches(document := _decode_document(payload), query or {})
        ]
        return LocalCursor(documents)

    def insert_one(self, document):
        new_document = dict(document)
        new_document.setdefault("_id", str(uuid4()))
        doc_key = self._document_key(new_document)
        with self.database.lock:
            self.database.connection.execute("BEGIN IMMEDIATE")
            try:
                self.database.connection.execute(
                    "INSERT INTO bot_documents(collection, doc_key, payload) VALUES (?, ?, ?)",
                    (self.name, doc_key, _encode_document(new_document)),
                )
                self.database.connection.commit()
            except Exception:
                self.database.connection.rollback()
                raise
        return type("InsertOneResult", (), {"inserted_id": new_document["_id"]})()

    def _apply_update(self, document, update):
        operators = any(str(key).startswith("$") for key in update)
        if not operators:
            replacement = dict(update)
            replacement.setdefault("_id", document.get("_id", str(uuid4())))
            return replacement
        for field, value in update.get("$set", {}).items():
            _set_nested(document, field, value)
        for field, value in update.get("$inc", {}).items():
            current = _get_nested(document, field, 0)
            _set_nested(document, field, (current or 0) + value)
        for field, value in update.get("$addToSet", {}).items():
            current = _get_nested(document, field)
            if current is None:
                current = []
                _set_nested(document, field, current)
            if value not in current:
                current.append(value)
        return document

    def _upsert_document(self, query, update):
        document = {
            key: value
            for key, value in (query or {}).items()
            if not (isinstance(value, dict) and any(str(k).startswith("$") for k in value))
        }
        document.setdefault("_id", str(uuid4()))
        self._apply_update(document, update)
        doc_key = self._document_key(document)
        self.database.connection.execute(
            "INSERT INTO bot_documents(collection, doc_key, payload) VALUES (?, ?, ?)",
            (self.name, doc_key, _encode_document(document)),
        )
        return doc_key

    def update_one(self, query, update, upsert=False):
        with self.database.lock:
            self.database.connection.execute("BEGIN IMMEDIATE")
            try:
                rows = self.database.connection.execute(
                    "SELECT doc_key, payload FROM bot_documents WHERE collection = ?",
                    (self.name,),
                ).fetchall()
                for doc_key, payload in rows:
                    document = _decode_document(payload)
                    if self._matches(document, query or {}):
                        before = _encode_document(document)
                        document = self._apply_update(document, update)
                        after = _encode_document(document)
                        if before != after:
                            self.database.connection.execute(
                                "UPDATE bot_documents SET payload = ? WHERE collection = ? AND doc_key = ?",
                                (after, self.name, doc_key),
                            )
                        self.database.connection.commit()
                        return LocalUpdateResult(1, int(before != after))
                if upsert:
                    upserted_id = self._upsert_document(query or {}, update)
                    self.database.connection.commit()
                    return LocalUpdateResult(0, 0, upserted_id)
                self.database.connection.commit()
                return LocalUpdateResult()
            except Exception:
                self.database.connection.rollback()
                raise

    def update_many(self, query, update):
        matched_count = 0
        modified_count = 0
        with self.database.lock:
            self.database.connection.execute("BEGIN IMMEDIATE")
            try:
                rows = self.database.connection.execute(
                    "SELECT doc_key, payload FROM bot_documents WHERE collection = ?",
                    (self.name,),
                ).fetchall()
                for doc_key, payload in rows:
                    document = _decode_document(payload)
                    if not self._matches(document, query or {}):
                        continue
                    matched_count += 1
                    before = _encode_document(document)
                    document = self._apply_update(document, update)
                    after = _encode_document(document)
                    if before != after:
                        self.database.connection.execute(
                            "UPDATE bot_documents SET payload = ? WHERE collection = ? AND doc_key = ?",
                            (after, self.name, doc_key),
                        )
                        modified_count += 1
                self.database.connection.commit()
            except Exception:
                self.database.connection.rollback()
                raise
        return LocalUpdateResult(matched_count, modified_count)

class LocalDatabase:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not DATABASE_PATH.exists() and DATABASE_BACKUP_PATH.exists():
            shutil.copy2(DATABASE_BACKUP_PATH, DATABASE_PATH)
            logging.info("Restored local bot database from the latest backup.")
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(
            str(DATABASE_PATH),
            timeout=30,
            check_same_thread=False,
            isolation_level=None,
        )
        with self.lock:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA busy_timeout=30000")
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS bot_documents ("
                "collection TEXT NOT NULL, doc_key TEXT NOT NULL, payload TEXT NOT NULL, "
                "PRIMARY KEY(collection, doc_key))"
            )
            check = self.connection.execute("PRAGMA quick_check").fetchone()[0]
            if check != "ok":
                self.connection.close()
                raise sqlite3.DatabaseError("Local bot database integrity check failed.")
        logging.info("Local user data database ready: data/bot_data.sqlite3")

    def __getitem__(self, name):
        return LocalCollection(self, name)

    def create_backup(self):
        temporary_path = DATA_DIR / "bot_data_backup.tmp"
        with self.lock:
            target = sqlite3.connect(str(temporary_path), timeout=30)
            try:
                self.connection.backup(target)
            finally:
                target.close()
        os.replace(temporary_path, DATABASE_BACKUP_PATH)

def start_local_backup_scheduler():
    def backup_loop():
        while True:
            try:
                db.create_backup()
            except Exception:
                logging.exception("Could not create the scheduled local database backup.")
            time.sleep(60)

    threading.Thread(
        target=backup_loop,
        name="local-database-backup",
        daemon=True,
    ).start()

db = LocalDatabase()
users_col = db["users"]
settings_col = db["settings"]
deposits_col = db["pending_deposits"]
subscription_payments_col = db["subscription_payments"]
daily_stats_col = db["daily_stats"]
subscription_activation_lock = threading.Lock()

def emoji(emoji_id: str, fallback: str = "✨") -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

E_CL = emoji("5222350726340032308", "🇨🇱")
E_HK = emoji("5292166459118606932", "🇭🇰")
E_WA = emoji("5334759662677957452", "💬")
E_SUB_VALID = emoji("5352638632278660622", "📅")
E_WELCOME = emoji("5352838545826420397", "👋")
E_PHONE = emoji("5269759232483303288", "📱")
E_ID = emoji("5841276284155467413", "🆔")
E_WAIT_OTP = emoji("5215522595922779944", "⏳")
E_PAY_SELECT = emoji("5256186332669035163", "💳")
E_BINANCE = emoji("5348212415077064131", "💛")
E_CANCEL = emoji("5240241223632954241", "🚫")
E_PROFILE_HEADER = emoji("5352861489541714456", "👤")
E_NAME = emoji("5397916757333654639", "📛")
E_BAL = emoji("5409048419211682843", "💵")
E_OTP_RCV = emoji("5253742260054409879", "📩")
E_SUB_EXP = emoji("5413879192267805083", "📅")
E_ADMIN_HEADER = emoji("5424818078833715060", "🛠")
E_BAN = emoji("5240241223632954241", "🚫")
E_UNBAN = emoji("5206607081334906820", "✅")
E_KEY_OTP = emoji("5282843764451195532", "🔑")

flask_app = Flask("")

@flask_app.route("/")
def home():
    return "HongKong & Chile WA Telegram Bot is Active!", 200

# Force Join Check Function
async def check_force_join(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not FORCE_JOIN_CHANNEL:
        return True
    try:
        member = await context.bot.get_chat_member(chat_id=FORCE_JOIN_CHANNEL, user_id=user_id)
        if member.status in ["member", "administrator", "creator"]:
            return True
    except Exception:
        pass
    return False

def get_zinipay_subscription_price(days: int):
    env_name = "ZINIPAY_SUBSCRIPTION_5_BDT" if days == 5 else "ZINIPAY_SUBSCRIPTION_7_BDT"
    raw_amount = os.getenv(env_name, "").strip()
    if not raw_amount:
        return None
    try:
        amount = Decimal(raw_amount)
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount <= 0:
        return None
    return amount

def _zinipay_headers():
    return {
        "Content-Type": "application/json",
        "zini-api-key": ZINIPAY_API_KEY or "",
    }

def create_zinipay_subscription_invoice(user_id: int, full_name: str, days: int):
    amount = get_zinipay_subscription_price(days)
    if not ZINIPAY_API_KEY or not amount:
        return None

    public_url = PUBLIC_BASE_URL
    redirect_url = ZINIPAY_REDIRECT_URL or public_url
    cancel_url = ZINIPAY_CANCEL_URL or redirect_url
    webhook_url = ZINIPAY_WEBHOOK_URL or (
        f"{public_url}/zinipay/subscription-webhook" if public_url else ""
    )
    if not all(
        url.startswith("https://")
        for url in (redirect_url, cancel_url, webhook_url)
    ):
        return None

    order_id = uuid4().hex
    payload = {
        "cus_name": full_name or str(user_id),
        "amount": float(amount),
        "metadata": {
            "order_id": order_id,
            "user_id": str(user_id),
            "subscription_days": days,
        },
        "redirect_url": redirect_url,
        "cancel_url": cancel_url,
        "webhook_url": webhook_url,
    }
    try:
        response = requests.post(
            f"{ZINIPAY_API_BASE}/v1/payment/create",
            headers=_zinipay_headers(),
            json=payload,
            timeout=20,
        )
        result = response.json()
    except (requests.RequestException, ValueError):
        return None

    status_ok = result.get("status") is True or str(result.get("status", "")).lower() in {
        "true", "success", "completed"
    }
    payment_url = result.get("payment_url")
    if not response.ok or not status_ok or not payment_url:
        return None

    invoice_id = result.get("invoice_id")
    if not invoice_id:
        path_parts = [part for part in urlparse(payment_url).path.split("/") if part]
        invoice_id = path_parts[-1] if path_parts else None
    if not invoice_id:
        return None

    subscription_payments_col.insert_one({
        "order_id": order_id,
        "invoice_id": str(invoice_id),
        "user_id": int(user_id),
        "days": int(days),
        "amount_bdt": str(amount),
        "status": "PENDING",
        "created_at": get_bd_time().replace(tzinfo=None),
    })
    return {"payment_url": payment_url, "amount": amount}

def verify_zinipay_invoice(invoice_id: str):
    if not ZINIPAY_API_KEY:
        return None
    try:
        response = requests.post(
            f"{ZINIPAY_API_BASE}/v1/payment/verify",
            headers=_zinipay_headers(),
            json={"invoice_id": invoice_id},
            timeout=20,
        )
        if not response.ok:
            return None
        return response.json()
    except (requests.RequestException, ValueError):
        return None

def notify_subscription_activated(user_id: int, days: int, expiry_date):
    if not BOT_TOKEN:
        return
    exp_str = expiry_date.strftime("%Y-%m-%d %H:%M")
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": user_id,
                "text": (
                    f"🎉 <b>ZiniPay payment successful!</b> "
                    f"{days} diner subscription active hoyeche.\n"
                    f"📅 Valid till: <code>{exp_str}</code> (BD Time)"
                ),
                "parse_mode": "HTML",
            },
            timeout=10,
        )
    except requests.RequestException:
        pass

def activate_zinipay_subscription(invoice_id: str, verification: dict):
    payment = subscription_payments_col.find_one({"invoice_id": invoice_id})
    if not payment:
        return False
    verified_invoice_id = verification.get("invoice_id")
    if verified_invoice_id and str(verified_invoice_id) != str(invoice_id):
        return False
    if str(verification.get("status", "")).upper() != "COMPLETED":
        return False
    try:
        if Decimal(str(verification.get("amount"))) != Decimal(payment["amount_bdt"]):
            return False
    except (InvalidOperation, TypeError, ValueError, KeyError):
        return False

    invoice_id = str(invoice_id)
    user_id = int(payment["user_id"])
    days = int(payment["days"])
    now = get_bd_time().replace(tzinfo=None)
    should_notify = False
    with subscription_activation_lock:
        user_doc = users_col.find_one({"user_id": user_id})
        if not user_doc:
            return False
        activated_ids = user_doc.get("activated_zinipay_invoice_ids", [])
        if invoice_id not in activated_ids:
            current_expiry = user_doc.get("subscription_expiry")
            start_date = max(now, current_expiry) if isinstance(current_expiry, datetime) else now
            expiry_date = start_date + timedelta(days=days)
            result = users_col.update_one(
                {
                    "user_id": user_id,
                    "activated_zinipay_invoice_ids": {"$ne": invoice_id},
                },
                {
                    "$set": {"subscription_expiry": expiry_date},
                    "$addToSet": {"activated_zinipay_invoice_ids": invoice_id},
                },
            )
            if result.modified_count:
                should_notify = True
            else:
                user_doc = users_col.find_one(
                    {"user_id": user_id},
                    {"activated_zinipay_invoice_ids": 1},
                )
                if not user_doc or invoice_id not in user_doc.get("activated_zinipay_invoice_ids", []):
                    return False
        else:
            expiry_date = user_doc.get("subscription_expiry")

    if should_notify:
        notify_subscription_activated(user_id, days, expiry_date)

    subscription_payments_col.update_one(
        {"invoice_id": invoice_id},
        {"$set": {
            "status": "COMPLETED",
            "transaction_id": verification.get("transaction_id"),
            "payment_method": verification.get("payment_method"),
            "verified_at": now,
        }},
    )
    return True

@flask_app.route("/zinipay/subscription-webhook", methods=["POST", "GET"])
def zinipay_subscription_webhook():
    payload = request.get_json(silent=True) or {}
    invoice_id = payload.get("invoice_id") or request.args.get("invoice_id")
    if not invoice_id:
        return {"ok": False, "error": "missing_invoice_id"}, 400

    invoice_id = str(invoice_id)
    payment = subscription_payments_col.find_one({"invoice_id": invoice_id})
    if not payment:
        return {"ok": False, "error": "unknown_invoice"}, 404
    if payment.get("status") == "COMPLETED":
        return {"ok": True}, 200

    verification = verify_zinipay_invoice(invoice_id)
    if not verification:
        return {"ok": False, "error": "verification_unavailable"}, 503
    if str(verification.get("status", "")).upper() != "COMPLETED":
        return {"ok": False, "status": "pending"}, 202
    if not activate_zinipay_subscription(invoice_id, verification):
        return {"ok": False, "error": "verification_mismatch"}, 400
    return {"ok": True}, 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

active_orders = {}

WAITING_AMOUNT, WAITING_TXID, WAITING_SCREENSHOT = range(3)
(
    ADMIN_BAN,
    ADMIN_UNBAN,
    ADMIN_ADD_BAL_USER,
    ADMIN_ADD_BAL_AMT,
    ADMIN_ZERO_BAL_USER,
    ADMIN_RATE_WA_CL_SET,
    ADMIN_RATE_WA_HK_SET,
    ADMIN_BROADCAST,
) = range(6, 14)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def mask_number(phone_str: str) -> str:
    clean_num = re.sub(r"[^\d+]", "", str(phone_str))
    if len(clean_num) <= 6:
        return clean_num
    prefix = clean_num[:4] if clean_num.startswith("+") else clean_num[:3]
    suffix = clean_num[-4:]
    masked_part = "*" * (len(clean_num) - len(suffix) - len(prefix))
    return f"{prefix}{masked_part}{suffix}"

def get_user(user_id: int):
    return users_col.find_one({"user_id": user_id})

def get_or_create_user(user_id: int, full_name: str = "User"):
    user = users_col.find_one({"user_id": user_id})
    if not user:
        user_data = {
            "user_id": user_id,
            "full_name": full_name,
            "balance": 0.0,
            "otp_count": 0,
            "selected_country": "cl",
            "selected_service": "wa",
            "is_banned": False,
            "subscription_expiry": None,
            "sub_days": 5
        }
        users_col.insert_one(user_data)
        return user_data
    else:
        users_col.update_one({"user_id": user_id}, {"$set": {"full_name": full_name}})
        return user

def is_subscribed(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    user = get_user(user_id)
    if user and user.get("subscription_expiry"):
        expiry = user["subscription_expiry"]
        if get_bd_time().replace(tzinfo=None) < expiry:
            return True
    return False

def get_rate(country: str = "cl"):
    doc = settings_col.find_one({"type": "rates"})
    if doc and "rates" in doc:
        if country == "hk" and "wa_hk" in doc["rates"]:
            return float(doc["rates"]["wa_hk"])
        elif country == "cl" and "wa_cl" in doc["rates"]:
            return float(doc["rates"]["wa_cl"])
    return 0.07 if country == "hk" else 0.10

def set_rate(country: str, rate: float):
    key = f"rates.wa_{country}"
    settings_col.update_one(
        {"type": "rates"},
        {"$set": {key: rate}},
        upsert=True
    )

def is_bot_active() -> bool:
    doc = settings_col.find_one({"type": "bot_status"})
    if doc:
        return doc.get("is_active", True)
    return True

def set_bot_active(status: bool):
    settings_col.update_one(
        {"type": "bot_status"},
        {"$set": {"is_active": status}},
        upsert=True
    )

def increment_daily_stat(country: str):
    today_str = get_bd_date_str()
    key = f"otp_counts.{country}"
    daily_stats_col.update_one(
        {"date": today_str},
        {"$inc": {key: 1}},
        upsert=True
    )

def get_main_keyboard(user_id):
    keyboard = [
        [KeyboardButton("💳 𝙰𝙲𝙲𝙾𝚄𝙽𝚃 𝙱𝙰𝙻𝙰𝙽𝙲𝙴"), KeyboardButton("🛒 𝙱𝚈 𝙽𝚄𝙼𝙱𝙴𝚁")],
        [KeyboardButton("🌐 𝚂𝙴𝙻𝙴𝙲𝚃 𝙲𝙾𝚄𝙽𝚃𝚁𝚈"), KeyboardButton("👤 𝙼𝚈 𝙿𝚁𝙾𝙵𝙸𝙻𝙴")],
        [KeyboardButton("💵 𝙳𝙸𝙿𝙾𝚂𝙸𝚃")]
    ]
    if is_admin(user_id):
        keyboard.append([KeyboardButton("⚙️ 𝙰𝙳𝙼𝙸𝙽 𝙿𝙰𝙽𝙴𝙻")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def set_number_status(id_num: str, status: str):
    url = f"https://vak-sms.com/api/setStatus/?apiKey={VAK_SMS_API_KEY}&idNum={id_num}&status={status}"
    try:
        return requests.get(url, timeout=5).json()
    except Exception:
        return {"error": "request_failed"}

async def set_number_status_safe(id_num: str, status: str, retries: int = 3):
    for attempt in range(retries):
        res = await asyncio.to_thread(set_number_status, id_num, status)
        if isinstance(res, dict) and "error" not in res:
            return res
        await asyncio.sleep(0.5)
    return {"error": "Failed after retries"}

def get_vak_balance():
    url = f"https://vak-sms.com/api/getBalance/?apiKey={VAK_SMS_API_KEY}"
    try:
        res = requests.get(url, timeout=5).json()
        return float(res.get("balance", 0.0))
    except Exception:
        return 0.0

def buy_vak_number(country: str = "cl", max_price: float = 0.10):
    url = f"https://vak-sms.com/api/getNumber/?apiKey={VAK_SMS_API_KEY}&service=wa&country={country}&maxPrice={max_price}"
    try:
        res = requests.get(url, timeout=5).json()
        if isinstance(res, dict) and res.get("error") in ["noNumber", "noBalance"]:
            return {"error": "Stock Out!"}
        if isinstance(res, dict) and "tel" in res and "idNum" in res:
            assigned_price = res.get("price")
            if assigned_price is not None:
                try:
                    price_val = float(assigned_price)
                    if price_val > max_price:
                        id_num = str(res["idNum"])
                        set_number_status(id_num, "bad")
                        return {"error": "Stock Out!"}
                except ValueError:
                    pass
        return res
    except Exception:
        return {"error": "Stock Out!"}

def fetch_otp_code(id_num: str):
    url = f"https://vak-sms.com/api/getSmsCode/?apiKey={VAK_SMS_API_KEY}&idNum={id_num}"
    try:
        return requests.get(url, timeout=5).json()
    except Exception as e:
        return {"error": str(e)}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    u_data = get_or_create_user(user_id, user.full_name)

    if u_data.get("is_banned", False):
        await update.message.reply_text(f"{E_BAN} 𝙱𝙰𝙽 𝙱𝚈 𝙰𝙳𝙼𝙸𝙽 𝙲𝙾𝙽𝚃𝙰𝙲𝚃 𝙰𝙳𝙼𝙸𝙽.", parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
        return

    # Force Join Check
    if FORCE_JOIN_CHANNEL and not await check_force_join(user_id, context):
        join_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{FORCE_JOIN_CHANNEL.replace('@', '')}")],
            [InlineKeyboardButton("🔄 Joined / Try Again", callback_data="check_join")]
        ])
        await update.message.reply_text(
            f"⚠️ <b>Bot bebohar korte hole obosshoi amader channel-এ join korte hobe!</b>\n\n"
            f"Nicher button-e click kore join kore asun, tarpor 'Joined / Try Again' button-e click korun.",
            parse_mode="HTML",
            reply_markup=join_kb
        )
        return

    if not is_bot_active() and not is_admin(user_id):
        await update.message.reply_text("🚧 <b>ʙᴏᴛ ᴜɴᴅᴇʀ ᴍᴀɪɴᴛᴀɪɴɪɴɢ ʙʏ ᴀᴅᴍɪɴ.</b> ᴘʟᴇᴀsᴇ ᴛʀʏ sᴏᴍᴇ ᴛɪᴍᴇ.", parse_mode="HTML")
        return

    if not is_subscribed(user_id):
        sub_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💛 5 Days", callback_data="buy_sub_5")],
            [InlineKeyboardButton("💛 7 Days", callback_data="buy_sub_7")]
        ])
        msg = (
            f"👋 Hello {user.full_name}!\n\n"
            f"{E_BAN} <b>YOU DO NOT HAVE AN ACTIVE SUBSCRIPTION!</b> {E_SUB_EXP}\n"
            f"Bot bebohar korte chaile subscription nite hobe.\n\n"
            f"📌 Choose a plan and pay with ZiniPay. Subscription activates automatically after payment verification.\n\n"
            f"Nicher button-e click kore subscription kinun:"
        )
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text("👇 <b>BUY SUBSCRIPTION:</b>", parse_mode="HTML", reply_markup=sub_kb)
        return

    welcome_msg = (
        f"👋 <b>𝚆𝙴𝙻𝙲𝙾𝙼𝙴 WHATSAPP 𝙱𝙾𝚃!</b> {E_WELCOME}\n\n"
        f"⚙️ <b>𝚃𝙶𝚂 𝚆𝙰 𝙽𝚄𝙼𝙱𝙴𝚁</b> {E_PHONE}"
    )
    await update.message.reply_text(welcome_msg, parse_mode="HTML", reply_markup=get_main_keyboard(user_id))

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    u_data = get_or_create_user(user_id, update.effective_user.full_name)

    if u_data.get("is_banned", False):
        await update.message.reply_text(f"{E_BAN} 𝙱𝙰𝙽 𝙱𝚈 𝙰𝙳𝙼𝙸𝙽 𝙲𝙾𝙽𝚃𝙰𝙲𝚃 𝙰𝙳𝙼𝙸𝙽.", parse_mode="HTML")
        return

    if FORCE_JOIN_CHANNEL and not await check_force_join(user_id, context):
        join_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{FORCE_JOIN_CHANNEL.replace('@', '')}")],
            [InlineKeyboardButton("🔄 Joined / Try Again", callback_data="check_join")]
        ])
        await update.message.reply_text(
            f"⚠️ <b>Bot bebohar korte hole obosshoi amader channel-এ join korte hobe!</b>",
            parse_mode="HTML",
            reply_markup=join_kb
        )
        return

    if not is_bot_active() and not is_admin(user_id):
        await update.message.reply_text("🚧 <b>ʙᴏᴛ ᴜɴᴅᴇʀ ᴍᴀɪɴᴛᴀɪɴɪɴɢ ʙʏ ᴀᴅᴍɪɴ.</b> ᴘʟᴇᴀsᴇ ᴛʀʏ sᴏᴍᴇ ᴛɪᴍᴇ.", parse_mode="HTML")
        return

    if not is_subscribed(user_id):
        sub_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💛 5 Days", callback_data="buy_sub_5")],
            [InlineKeyboardButton("💛 7 Days", callback_data="buy_sub_7")]
        ])
        await update.message.reply_text(f"{E_BAN} <b>SUBSCRIPTION EXPIRED! BUY NEW SUBSCRIPTION.</b>", parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text("👇 <b>BUY SUBSCRIPTION:</b>", parse_mode="HTML", reply_markup=sub_kb)
        return

    if text == "💳 𝙰𝙲𝙲𝙾𝚄𝙽𝚃 𝙱𝙰𝙻𝙰𝙽𝙲𝙴":
        bot_bal = u_data.get("balance", 0.0)
        msg = f"💰 <b>𝙼𝚈 𝙱𝙰𝙻𝙰𝙽𝙲𝙴:</b> <code>{bot_bal:.4f}</code> USDT {E_BAL}"
        if is_admin(user_id):
            site_bal = get_vak_balance()
            available_numbers = int(site_bal // 0.07) if site_bal > 0 else 0
            msg += f"\n📊 <b>𝙰𝚅𝙰𝙸𝙻𝙰𝙱𝙻𝙴 𝙽𝚄𝙼𝙱𝙴𝚁𝚂 :</b> <code>{available_numbers}</code> Pcs"
        await update.message.reply_text(msg, parse_mode="HTML")
        return

    elif text == "🌐 𝚂𝙴𝙻𝙴𝙲𝚃 𝙲𝙾𝚄𝙽𝚃𝚁𝚈":
        curr_country = u_data.get("selected_country", "cl")
        country_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"{'✅ ' if curr_country == 'cl' else ''}🇨🇱 Chile", callback_data="set_country_cl"),
                InlineKeyboardButton(f"{'✅ ' if curr_country == 'hk' else ''}🇭🇰 Hong Kong", callback_data="set_country_hk")
            ]
        ])
        await update.message.reply_text("🌐 <b>SELECT YOUR PREFERRED COUNTRY:</b>", parse_mode="HTML", reply_markup=country_kb)
        return

    elif text == "🛒 𝙱𝚈 𝙽𝚄𝙼𝙱𝙴𝚁":
        if user_id in active_orders:
            await update.message.reply_text(f"{E_CANCEL} 𝚈𝙾𝚄 𝙰𝙻𝚁𝙴𝙰𝙳𝚈 𝙷𝙰𝚅𝙴 𝙰𝙽 𝙰𝙲𝚃𝙸𝚅𝙴 𝙽𝚄𝙼𝙱𝙴𝚁. 𝙲𝙰𝙽𝙲𝙴𝙻 𝙾𝚁 𝙵𝙸𝙽𝙸𝚂𝙷 𝙸𝚃 𝙵𝙸𝚁𝚂𝚃.", parse_mode="HTML")
            return

        selected_country = u_data.get("selected_country", "cl")
        rate = get_rate(selected_country)
        user_bal = u_data.get("balance", 0.0)

        if user_bal < rate:
            await update.message.reply_text(f"{E_BAN} 𝙸𝙽𝚂𝚄𝙵𝙵𝙸𝙲𝙸𝙴𝙽𝚃 𝙱𝙰𝙻𝙰𝙽𝙲𝙴!\n⚡ 𝙽𝙴𝙴𝙳: <code>{rate:.2f}</code> USDT\n💰 𝚈𝙾𝚄𝚁 𝙱𝙰𝙻𝙰𝙽𝙲𝙴: <code>{user_bal:.4f}</code> USDT {E_BAL}", parse_mode="HTML")
            return

        c_name = "Chile 🇨🇱" if selected_country == "cl" else "Hong Kong 🇭🇰"
        await update.message.reply_text(f"⏳ <b>𝚆𝙰𝙸𝚃 𝙵𝙾𝚁 {c_name} 𝙽𝚄𝙼𝙱𝙴𝚁...</b> {E_WAIT_OTP}", parse_mode="HTML")

        max_price = 0.07 if selected_country == "hk" else rate
        res = await asyncio.to_thread(buy_vak_number, selected_country, max_price)
        if "error" in res or "tel" not in res or "idNum" not in res:
            await update.message.reply_text(f"{E_CANCEL} <b>Stock Out!</b>", parse_mode="HTML")
            return

        phone_num = str(res["tel"])
        id_num = str(res["idNum"])

        inline_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚫 𝙲𝙰𝙽𝙲𝙴𝙻 𝙽𝚄𝙼𝙱𝙴𝚁", callback_data="cancel_number")]
        ])

        buying_msg = (
            f"{E_UNBAN} <b>𝙽𝚄𝙼𝙱𝙴𝚁 𝙿𝚄𝚁𝙲𝙷𝙰𝚂𝙴𝙳! ({c_name})</b>\n\n"
            f"📱 <b>𝙽𝚄𝙼𝙱𝙴𝚁:</b> <code>{phone_num}</code> {E_PHONE}\n"
            f"💰 <b>𝙿𝚁𝙸𝙲𝙴:</b> <code>{rate:.2f}</code> USDT {E_BAL}\n\n"
            f"⏳ <b>𝚆𝙰𝙸𝚃𝙸𝙽𝙶 𝙵𝙾𝚁 𝙾𝚃𝙿 (𝙰𝚄𝚃𝙾 𝙿𝙾𝙻𝙻𝙸𝙽𝙶)...</b> {E_WAIT_OTP}"
        )
        sent_msg = await update.message.reply_text(buying_msg, parse_mode="HTML", reply_markup=inline_kb)

        active_orders[user_id] = {
            "idNum": id_num,
            "phone": phone_num,
            "cost": rate,
            "country": selected_country,
            "status": "WAITING_OTP",
            "message_id": sent_msg.message_id,
            "cancel_task": None,
            "poll_task": None
        }

        async def auto_cancel():
            await asyncio.sleep(180)
            if user_id in active_orders and active_orders[user_id]["status"] == "WAITING_OTP":
                order_info = active_orders[user_id]
                msg_id = order_info["message_id"]
                await set_number_status_safe(id_num, "bad")
                if user_id in active_orders:
                    del active_orders[user_id]
                try:
                    await context.bot.edit_message_text(
                        chat_id=user_id,
                        message_id=msg_id,
                        text=f"{E_CANCEL} <b>𝙽𝚄𝙼𝙱𝙴𝚁 𝙲𝙰𝙽𝙲𝙴𝙻𝙻𝙴𝙳 𝙳𝚄𝙴 𝚃𝙾 𝚃𝙸𝙼𝙴𝙾𝚄𝚃 (𝟹 𝙼𝙸𝙽):</b> <code>{phone_num}</code>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

        async def poll_otp():
            while user_id in active_orders and active_orders[user_id]["status"] == "WAITING_OTP":
                await asyncio.sleep(1.5)
                sms_res = await asyncio.to_thread(fetch_otp_code, id_num)
                
                if isinstance(sms_res, dict) and sms_res.get("smsCode"):
                    otp_code = str(sms_res["smsCode"])
                    order_info = active_orders[user_id]
                    order_info["status"] = "COMPLETED"
                    order_msg_id = order_info["message_id"]
                    ord_country = order_info.get("country", "cl")

                    if order_info["cancel_task"]:
                        order_info["cancel_task"].cancel()

                    users_col.update_one(
                        {"user_id": user_id},
                        {
                            "$inc": {
                                "balance": -order_info["cost"],
                                "otp_count": 1
                            }
                        }
                    )

                    increment_daily_stat(ord_country)
                    await set_number_status_safe(id_num, "end")
                    del active_orders[user_id]

                    updated_user_doc = get_user(user_id)
                    rem_bal = updated_user_doc.get("balance", 0.0) if updated_user_doc else 0.0

                    otp_msg = (
                        f"{E_UNBAN} <b>𝙾𝚃𝙿 𝚁𝙴𝙲𝙴𝙸𝚅𝙴𝙳 𝚂𝚄𝙲𝙲𝙴𝚂𝚂𝙵𝚄𝙻𝙻𝚈!</b>\n\n"
                        f"📱 <b>𝙽𝚄𝙼𝙱𝙴𝚁:</b> <code>{phone_num}</code> {E_PHONE}\n"
                        f"💬 <b>𝙾𝚃𝙿 𝙲𝙾𝙳𝙴:</b> <code>{otp_code}</code> {E_KEY_OTP}\n\n"
                        f"💰 <b>𝙳𝙴𝙳𝚄𝙲𝚃𝙴𝙳:</b> <code>{rate:.2f}</code> USDT {E_BAL}\n"
                        f"💵 <b>𝚁𝙴𝙼𝙰𝙸𝙽𝙸𝙽𝙶 𝙱𝙰𝙻𝙰𝙽𝙲𝙴:</b> <code>{rem_bal:.4f}</code> USDT"
                    )
                    
                    full_sms = sms_res.get("sms", "")
                    inline_kb_copy = None
                    if full_sms:
                        inline_kb_copy = InlineKeyboardMarkup([
                            [InlineKeyboardButton("📋 𝙲𝙾𝙿𝚈 𝚂𝙼𝚂", callback_data=f"copy_sms:{user_id}")]
                        ])
                        context.user_data[f"full_sms_{user_id}"] = full_sms

                    try:
                        await context.bot.edit_message_text(
                            chat_id=user_id,
                            message_id=order_msg_id,
                            text=otp_msg,
                            parse_mode="HTML",
                            reply_markup=inline_kb_copy
                        )
                    except Exception as e:
                        logging.error(f"Failed to edit message: {e}")

                    # OTP Group Forwarding Logic
                    if OTP_GROUP_ID:
                        try:
                            m_num = mask_number(phone_num)
                            group_text = (
                                f"🎉 <b>𝙽𝙴𝚆 𝚂𝚄𝙲𝙲𝙴𝚂𝚂𝙵𝚄𝙻 𝙾𝚃𝙿!</b> {E_WA}\n\n"
                                f"👤 <b>𝚄𝚂𝙴𝚁 ID:</b> <code>{user_id}</code> {E_ID}\n"
                                f"📱 <b>𝙽𝚄𝙼𝙱𝙴𝚁:</b> <code>{m_num}</code> {E_PHONE}\n"
                                f"💬 <b>𝙾𝚃𝙿 𝙲𝙾𝙳𝙴:</b> <code>{otp_code}</code> {E_KEY_OTP}"
                            )
                            await context.bot.send_message(chat_id=OTP_GROUP_ID, text=group_text, parse_mode="HTML")
                        except Exception as e:
                            logging.error(f"Failed to send to group: {e}")
                    break

        cancel_task = asyncio.create_task(auto_cancel())
        poll_task = asyncio.create_task(poll_otp())
        
        active_orders[user_id]["cancel_task"] = cancel_task
        active_orders[user_id]["poll_task"] = poll_task

    elif text == "👤 𝙼𝚈 𝙿𝚁𝙾𝙵𝙸𝙻𝙴":
        exp_time = u_data.get("subscription_expiry")
        exp_str = exp_time.strftime("%Y-%m-%d %H:%M") if (exp_time and not is_admin(user_id)) else "Unlimited (Admin)"
        selected_c = u_data.get('selected_country', 'cl').upper()
        p_msg = (
            f"👤 <b>𝚄𝚂𝙴𝚁 𝙿𝚁𝙾𝙵𝙸𝙻𝙴</b> {E_PROFILE_HEADER}\n\n"
            f"🆔 <b>𝚄𝚂𝙴𝚁 ID:</b> <code>{user_id}</code> {E_ID}\n"
            f"📛 <b>𝙽𝙰𝙼𝙴:</b> {u_data.get('full_name', 'User')} {E_NAME}\n"
            f"🌐 <b>SELECTED COUNTRY:</b> <code>{selected_c}</code>\n"
            f"💵 <b>𝙱𝙰𝙻𝙰𝙽𝙲𝙴:</b> <code>{u_data.get('balance', 0.0):.4f}</code> USDT {E_BAL}\n"
            f"📊 <b>𝚃𝙾𝚃𝙰𝙻 𝙾𝚃𝙿 𝙱𝙾𝚄𝙶𝙷𝚃:</b> <code>{u_data.get('otp_count', 0)}</code> {E_OTP_RCV}\n"
            f"📅 <b>𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝚅𝙰𝙻𝙸𝙳:</b> {exp_str} {E_SUB_VALID}"
        )
        await update.message.reply_text(p_msg, parse_mode="HTML")

    elif text == "💵 𝙳𝙸𝙿𝙾𝚂𝙸𝚃":
        dep_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💛 BINANCE PAY", callback_data="pay_binance")]
        ])
        await update.message.reply_text(f"💳 <b>SELECT DEPOSIT METHOD:</b> {E_PAY_SELECT}", parse_mode="HTML", reply_markup=dep_kb)

    elif text == "⚙️ 𝙰𝙳𝙼𝙸𝙽 𝙿𝙰𝙽𝙴𝙻" and is_admin(user_id):
        curr_status = is_bot_active()
        status_text = "🟢 ACTIVE" if curr_status else "🔴 OFF (MAINTENANCE)"

        admin_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚫 BAN USER", callback_data="admin_ban"), InlineKeyboardButton("✅ UNBAN USER", callback_data="admin_unban")],
            [InlineKeyboardButton("➕ ADD BALANCE", callback_data="admin_add_bal"), InlineKeyboardButton("🧹 ZERO BALANCE", callback_data="admin_zero_bal")],
            [InlineKeyboardButton("🧹 ZERO ALL BALANCES", callback_data="admin_zero_all_bal")],
            [InlineKeyboardButton("📊 VIEW USERS", callback_data="admin_view_users"), InlineKeyboardButton("📈 DAILY STATS", callback_data="admin_daily_stats")],
            [InlineKeyboardButton("⚙️ SET CHILE RATE", callback_data="admin_set_rate_cl"), InlineKeyboardButton("⚙️ SET HK RATE", callback_data="admin_set_rate_hk")],
            [InlineKeyboardButton("💥 RESET ALL SUBSCRIPTIONS", callback_data="admin_reset_all_subs")],
            [InlineKeyboardButton(f"🤖 BOT STATUS: {status_text}", callback_data="admin_toggle_bot")],
            [InlineKeyboardButton("📢 BROADCAST", callback_data="admin_broadcast")]
        ])
        await update.message.reply_text(f"{E_ADMIN_HEADER} <b>𝙰𝙳𝙼𝙸𝙽 𝙲𝙾𝙽𝚃𝚁𝙾𝙻 𝙿𝙰𝙽𝙴𝙻</b>", parse_mode="HTML", reply_markup=admin_kb)

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "check_join":
        if FORCE_JOIN_CHANNEL and await check_force_join(user_id, context):
            await query.message.edit_text("✅ Dhonnobad channel-এ join korar jonno! Ekhon apnake bot-er main menu dewa holo.", parse_mode="HTML")
            await context.bot.send_message(chat_id=user_id, text="👇 <b>MAIN MENU:</b>", parse_mode="HTML", reply_markup=get_main_keyboard(user_id))
        else:
            await query.answer("❌ Apni abow o channel-এ join koren nai! Age join করুন.", show_alert=True)
        return

    if data == "set_country_cl":
        users_col.update_one({"user_id": user_id}, {"$set": {"selected_country": "cl"}})
        await query.message.edit_text("🇨🇱 <b>Chile Selected Successfully!</b> Now you can buy Chile WhatsApp numbers.", parse_mode="HTML")
        return
    elif data == "set_country_hk":
        users_col.update_one({"user_id": user_id}, {"$set": {"selected_country": "hk"}})
        await query.message.edit_text("🇭🇰 <b>Hong Kong Selected Successfully!</b> Now you can buy Hong Kong WhatsApp numbers.", parse_mode="HTML")
        return

    if data == "cancel_number":
        if user_id in active_orders:
            order = active_orders[user_id]
            id_num = order["idNum"]
            phone_num = order["phone"]

            if order["cancel_task"]:
                order["cancel_task"].cancel()
            if order["poll_task"]:
                order["poll_task"].cancel()

            asyncio.create_task(set_number_status_safe(id_num, "bad"))
            del active_orders[user_id]

            await query.edit_message_text(f"{E_CANCEL} <b>𝙽𝚄𝙼𝙱𝙴𝚁 𝙲𝙰𝙽𝙲𝙴𝙻𝙻𝙴𝙳:</b> <code>{phone_num}</code>", parse_mode="HTML")
        else:
            await query.edit_message_text(f"{E_CANCEL} <b>𝙽𝙾 𝙰𝙲𝚃𝙸𝚅𝙴 𝙽𝚄𝙼𝙱𝙴𝚁 𝚃𝙾 𝙲𝙰𝙽𝙲𝙴𝙻.</b>", parse_mode="HTML")

    elif data.startswith("copy_sms:"):
        target_uid = int(data.split(":")[1])
        sms_text = context.user_data.get(f"full_sms_{target_uid}", "No SMS Text Available")
        await query.message.reply_text(f"📋 <b>𝙵𝚄𝙻𝙻 𝚂𝙼𝚂:</b>\n<code>{sms_text}</code>", parse_mode="HTML")

    elif is_admin(user_id):
        if data == "admin_toggle_bot":
            curr = is_bot_active()
            new_status = not curr
            set_bot_active(new_status)
            status_text = "🟢 ACTIVE" if new_status else "🔴 OFF (MAINTENANCE)"
            admin_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🚫 BAN USER", callback_data="admin_ban"), InlineKeyboardButton("✅ UNBAN USER", callback_data="admin_unban")],
                [InlineKeyboardButton("➕ ADD BALANCE", callback_data="admin_add_bal"), InlineKeyboardButton("🧹 ZERO BALANCE", callback_data="admin_zero_bal")],
                [InlineKeyboardButton("🧹 ZERO ALL BALANCES", callback_data="admin_zero_all_bal")],
                [InlineKeyboardButton("📊 VIEW USERS", callback_data="admin_view_users"), InlineKeyboardButton("📈 DAILY STATS", callback_data="admin_daily_stats")],
                [InlineKeyboardButton("⚙️ SET CHILE RATE", callback_data="admin_set_rate_cl"), InlineKeyboardButton("⚙️ SET HK RATE", callback_data="admin_set_rate_hk")],
                [InlineKeyboardButton("💥 RESET ALL SUBSCRIPTIONS", callback_data="admin_reset_all_subs")],
                [InlineKeyboardButton(f"🤖 BOT STATUS: {status_text}", callback_data="admin_toggle_bot")],
                [InlineKeyboardButton("📢 BROADCAST", callback_data="admin_broadcast")]
            ])
            await query.edit_message_reply_markup(reply_markup=admin_kb)

        elif data == "admin_daily_stats":
            stats_cursor = daily_stats_col.find({}).sort("date", -1).limit(10)
            stats_list = list(stats_cursor)
            if not stats_list:
                await query.message.reply_text("📈 <b>NO DAILY STATS FOUND YET.</b>", parse_mode="HTML")
                return
            msg = "📈 <b>DAILY OTP STATISTICS (BD TIME):</b>\n\n"
            for st in stats_list:
                d_date = st.get("date", "N/A")
                counts = st.get("otp_counts", {})
                cl_count = counts.get("cl", 0)
                hk_count = counts.get("hk", 0)
                total_day = cl_count + hk_count
                msg += (
                    f"📅 <b>Date:</b> <code>{d_date}</code>\n"
                    f"🇨🇱 <b>Chile:</b> <code>{cl_count}</code> Pcs\n"
                    f"🇭🇰 <b>Hong Kong:</b> <code>{hk_count}</code> Pcs\n"
                    f"📊 <b>Total:</b> <code>{total_day}</code> Pcs\n"
                    f"----------------------------\n"
                )
            await query.message.reply_text(msg, parse_mode="HTML")

        elif data == "admin_zero_all_bal":
            users_col.update_many({}, {"$set": {"balance": 0.0}})
            await query.message.reply_text("🧹 <b>SUCCESS! ALL USERS' BALANCES HAVE BEEN RESET TO 0.00 USDT.</b>", parse_mode="HTML")

        elif data == "admin_reset_all_subs":
            users_col.update_many({}, {"$set": {"subscription_expiry": None}})
            await query.message.reply_text("💥 <b>SUCCESS! ALL USERS' SUBSCRIPTIONS HAVE BEEN RESET/CANCELLED.</b>", parse_mode="HTML")

        elif data == "admin_view_users":
            all_users = list(users_col.find({}))
            total_users = len(all_users)
            if total_users == 0:
                await query.message.reply_text("📋 <b>NO USERS FOUND.</b>", parse_mode="HTML")
                return
            text_msg = f"📊 <b>TOTAL BOT USERS:</b> <code>{total_users}</code>\n\n"
            for u in all_users:
                text_msg += (
                    f"👤 <b>Name:</b> {u.get('full_name', 'N/A')}\n"
                    f"🆔 <b>ID:</b> <code>{u.get('user_id')}</code>\n"
                    f"💰 <b>Balance:</b> <code>{u.get('balance', 0.0):.4f}</code> USDT\n"
                    f"📥 <b>OTP Bought:</b> <code>{u.get('otp_count', 0)}</code>\n"
                    f"🚫 <b>Status:</b> {'Banned' if u.get('is_banned') else 'Active'}\n"
                    f"----------------------------\n"
                )
            if len(text_msg) > 4000:
                for i in range(0, len(text_msg), 4000):
                    await query.message.reply_text(text_msg[i:i+4000], parse_mode="HTML")
            else:
                await query.message.reply_text(text_msg, parse_mode="HTML")

        elif data == "admin_ban":
            await query.message.reply_text("SEND USER ID TO BAN:")
            return ADMIN_BAN
        elif data == "admin_unban":
            await query.message.reply_text("SEND USER ID TO UNBAN:")
            return ADMIN_UNBAN
        elif data == "admin_add_bal":
            await query.message.reply_text("SEND USER ID TO ADD BALANCE:")
            return ADMIN_ADD_BAL_USER
        elif data == "admin_zero_bal":
            await query.message.reply_text("SEND USER ID TO ZERO BALANCE:")
            return ADMIN_ZERO_BAL_USER
        elif data == "admin_set_rate_cl":
            curr_rate = get_rate("cl")
            await query.message.reply_text(f"CURRENT CHILE WA RATE IS: <code>{curr_rate}</code> USDT\nSEND NEW RATE FOR CHILE WA:", parse_mode="HTML")
            return ADMIN_RATE_WA_CL_SET
        elif data == "admin_set_rate_hk":
            curr_rate = get_rate("hk")
            await query.message.reply_text(f"CURRENT HONG KONG WA RATE IS: <code>{curr_rate}</code> USDT\nSEND NEW RATE FOR HONG KONG WA:", parse_mode="HTML")
            return ADMIN_RATE_WA_HK_SET
        elif data == "admin_broadcast":
            await query.message.reply_text("SEND BROADCAST MESSAGE TO ALL USERS:")
            return ADMIN_BROADCAST

async def sub_start_5(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await sub_zinipay_checkout(update, context, 5)

async def sub_start_7(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await sub_zinipay_checkout(update, context, 7)

async def sub_zinipay_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE, days: int):
    query = update.callback_query
    await query.answer()
    users_col.update_one(
        {"user_id": query.from_user.id},
        {"$set": {"sub_days": days}},
    )
    amount = get_zinipay_subscription_price(days)
    if not amount:
        await query.message.reply_text("⚠️ ZiniPay checkout এখনো সেট করা হয়নি।")
        return ConversationHandler.END

    invoice = await asyncio.to_thread(
        create_zinipay_subscription_invoice,
        query.from_user.id,
        query.from_user.full_name,
        days,
    )
    if not invoice:
        await query.message.reply_text("❌ ZiniPay invoice তৈরি করা যায়নি।")
        return ConversationHandler.END

    payment_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💳 Pay with ZiniPay", url=invoice["payment_url"])
    ]])
    await query.message.reply_text(
        f"🧾 <b>ZiniPay invoice তৈরি হয়েছে</b>\n"
        f"📅 Plan: {days} দিন\n"
        f"💰 Amount: <b>৳{invoice['amount']:.2f} BDT</b>",
        parse_mode="HTML",
        reply_markup=payment_kb,
    )
    return ConversationHandler.END

async def deposit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        user_id = query.from_user.id
        u_data = get_or_create_user(user_id, query.from_user.full_name)
    else:
        user_id = update.effective_user.id
        u_data = get_or_create_user(user_id, update.effective_user.full_name)

    if u_data.get("is_banned", False):
        return ConversationHandler.END

    cancel_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ CANCEL DEPOSIT", callback_data="cancel_deposit")]
    ])
    dep_msg = (
        f"💵 <b>𝙳𝙴𝙿𝙾𝚂𝙸𝚃 𝚅𝙸𝙰 𝙱𝙸𝙽𝙰𝙽𝙲𝙴 𝙿𝙰𝚈</b> {E_BINANCE}\n\n"
        f"🆔 <b>𝙱𝙸𝙽𝙰𝙽𝙲𝙴 𝙿𝙰𝚈 ID:</b> <code>{BINANCE_ID}</code> {E_ID}\n"
        f"⚠️ <b>𝙼𝙸𝙽𝙸𝙼𝚄𝙼 𝙳𝙴𝙿𝙾𝚂𝙸𝚃:</b> <code>$0.11</code> USDT\n\n"
        f"✍️ <b>𝚂𝙴𝙽𝙳 𝚃𝙷𝙴 𝙰𝙼𝙾𝚄𝙽𝚃 (𝚄𝚂𝙳𝚃) 𝚈𝙾𝚄 𝙷𝙰𝚅𝙴 𝚂𝙴𝙽𝚃:</b>"
    )
    if query:
        await query.message.reply_text(dep_msg, parse_mode="HTML", reply_markup=cancel_kb)
    else:
        await update.message.reply_text(dep_msg, parse_mode="HTML", reply_markup=cancel_kb)
    return WAITING_AMOUNT

async def deposit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    cancel_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ CANCEL DEPOSIT", callback_data="cancel_deposit")]
    ])
    try:
        amount = float(text)
        if amount < 0.11:
            await update.message.reply_text("❌ MINIMUM DEPOSIT IS $0.11 USDT!", parse_mode="HTML", reply_markup=cancel_kb)
            return WAITING_AMOUNT
        context.user_data["dep_amount"] = amount
        await update.message.reply_text("📥 <b>𝚂𝙴𝙽𝙳 𝚈𝙾𝚄𝚁 𝙱𝙸𝙽𝙰𝙽𝙲𝙴 𝙿𝙰𝚈 𝚃𝚇𝙸𝙳 / 𝙾𝚁𝙳𝙴𝚁 ID:</b>", parse_mode="HTML", reply_markup=cancel_kb)
        return WAITING_TXID
    except ValueError:
        await update.message.reply_text("❌ INVALID AMOUNT.", parse_mode="HTML", reply_markup=cancel_kb)
        return WAITING_AMOUNT

async def deposit_txid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["dep_txid"] = update.message.text
    cancel_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ CANCEL DEPOSIT", callback_data="cancel_deposit")]
    ])
    await update.message.reply_text("📸 <b>𝚂𝙴𝙽𝙳 𝙰 𝚂𝙲𝚁𝙴𝙴𝙽𝚂𝙷𝙾𝚃 𝙾𝙵 𝚃𝙷𝙴 𝙿𝙰𝚈𝙼𝙴𝙽𝚃:</b>", parse_mode="HTML", reply_markup=cancel_kb)
    return WAITING_SCREENSHOT

async def deposit_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    photo = update.message.photo[-1]
    photo_file_id = photo.file_id

    amount = context.user_data.get("dep_amount", 0.0)
    txid = context.user_data.get("dep_txid", "N/A")

    await update.message.reply_text(f"{E_UNBAN} <b>𝙳𝙴𝙿𝙾𝚂𝙸𝚃 𝚁𝙴𝚀𝚄𝙴𝚂𝚃 𝚂𝚄𝙱𝙼𝙸𝚃𝚃𝙴𝙳!</b>", parse_mode="HTML")

    dep_id = f"{user_id}_{int(get_bd_time().timestamp())}"
    admin_messages = []

    for admin_id in ADMIN_IDS:
        try:
            admin_msg = (
                f"📥 <b>𝙽𝙴𝚆 𝙳𝙴𝙿𝙾𝚂𝙸𝚃 𝚁𝙴𝚀𝚄𝙴𝚂𝚃!</b>\n\n"
                f"👤 <b>𝚄𝚂𝙴𝚁:</b> {user.full_name} (<code>{user_id}</code>) {E_ID}\n"
                f"💰 <b>𝙰𝙼𝙾𝚄𝙽𝚃:</b> <code>${amount:.2f}</code> USDT {E_BAL}\n"
                f"🆔 <b>𝚃𝚇𝙸𝙳:</b> <code>{txid}</code>"
            )
            approve_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ APPROVE", callback_data=f"app_dep:{dep_id}:{user_id}:{amount}"),
                    InlineKeyboardButton("❌ REJECT", callback_data=f"rej_dep:{dep_id}:{user_id}")
                ]
            ])
            sent_m = await context.bot.send_photo(
                chat_id=admin_id,
                photo=photo_file_id,
                caption=admin_msg,
                parse_mode="HTML",
                reply_markup=approve_kb
            )
            admin_messages.append({"chat_id": admin_id, "message_id": sent_m.message_id})
        except Exception:
            pass

    deposits_col.insert_one({
        "dep_id": dep_id,
        "user_id": user_id,
        "amount": amount,
        "status": "PENDING",
        "admin_messages": admin_messages
    })
    return ConversationHandler.END

async def deposit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text(f"{E_CANCEL} <b>𝙳𝙴𝙿𝙾𝚂𝙸𝚃 𝙿𝚁𝙾𝙲𝙴𝚂𝚂 𝙲𝙰𝙽𝙲𝙴𝙻𝙻𝙴𝙳!</b>", parse_mode="HTML")
    return ConversationHandler.END

async def admin_ban_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target_id = int(update.message.text)
        users_col.update_one({"user_id": target_id}, {"$set": {"is_banned": True}})
        await update.message.reply_text(f"USER <code>{target_id}</code> BANNED.", parse_mode="HTML")
    except ValueError:
        pass
    return ConversationHandler.END

async def admin_unban_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target_id = int(update.message.text)
        users_col.update_one({"user_id": target_id}, {"$set": {"is_banned": False}})
        await update.message.reply_text(f"USER <code>{target_id}</code> UNBANNED.", parse_mode="HTML")
    except ValueError:
        pass
    return ConversationHandler.END

async def admin_add_bal_user_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target_id = int(update.message.text)
        context.user_data["target_user_id"] = target_id
        await update.message.reply_text("SEND AMOUNT TO ADD:")
        return ADMIN_ADD_BAL_AMT
    except ValueError:
        return ConversationHandler.END

async def admin_add_bal_amt_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(update.message.text)
        target_id = context.user_data.get("target_user_id")
        users_col.update_one({"user_id": target_id}, {"$inc": {"balance": amt}})
        await update.message.reply_text(f"ADDED <code>${amt:.2f}</code> USDT.", parse_mode="HTML")
    except ValueError:
        pass
    return ConversationHandler.END

async def admin_zero_bal_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target_id = int(update.message.text)
        users_col.update_one({"user_id": target_id}, {"$set": {"balance": 0.0}})
        await update.message.reply_text(f"BALANCE ZEROED FOR <code>{target_id}</code>.", parse_mode="HTML")
    except ValueError:
        pass
    return ConversationHandler.END

async def admin_set_rate_cl_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_rate = float(update.message.text)
        set_rate("cl", new_rate)
        await update.message.reply_text(f"CHILE WA RATE UPDATED TO: {new_rate}", parse_mode="HTML")
    except ValueError:
        pass
    return ConversationHandler.END

async def admin_set_rate_hk_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_rate = float(update.message.text)
        set_rate("hk", new_rate)
        await update.message.reply_text(f"HONG KONG WA RATE UPDATED TO: {new_rate}", parse_mode="HTML")
    except ValueError:
        pass
    return ConversationHandler.END

async def admin_broadcast_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_text = update.message.text
    users = users_col.find({})
    count = 0
    for u in users:
        try:
            await context.bot.send_message(u["user_id"], msg_text)
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await update.message.reply_text(f"BROADCAST SENT TO {count} USERS.")
    return ConversationHandler.END

async def admin_deposit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    action, dep_id, target_uid = parts[0], parts[1], int(parts[2])

    dep_doc = deposits_col.find_one({"dep_id": dep_id})
    if not dep_doc or dep_doc.get("status") != "PENDING":
        return

    if action == "app_dep":
        amt = float(parts[3])
        users_col.update_one({"user_id": target_uid}, {"$inc": {"balance": amt}})
        deposits_col.update_one({"dep_id": dep_id}, {"$set": {"status": "APPROVED"}})
        try:
            await context.bot.send_message(target_uid, f"🎉 <b>Deposit Approved! ${amt:.2f} USDT added.</b>", parse_mode="HTML")
        except Exception:
            pass
    elif action == "rej_dep":
        deposits_col.update_one({"dep_id": dep_id}, {"$set": {"status": "REJECTED"}})
        try:
            await context.bot.send_message(target_uid, "❌ <b>Deposit Request Rejected.</b>", parse_mode="HTML")
        except Exception:
            pass

def main():
    start_local_backup_scheduler()
    threading.Thread(target=run_flask, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    deposit_handler = ConversationHandler(
        entry_points=[
            CommandHandler("deposit", deposit_start),
            CallbackQueryHandler(deposit_start, pattern="^pay_binance$")
        ],
        states={
            WAITING_AMOUNT: [
                CallbackQueryHandler(deposit_cancel, pattern="^cancel_deposit$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_amount)
            ],
            WAITING_TXID: [
                CallbackQueryHandler(deposit_cancel, pattern="^cancel_deposit$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_txid)
            ],
            WAITING_SCREENSHOT: [
                CallbackQueryHandler(deposit_cancel, pattern="^cancel_deposit$"),
                MessageHandler(filters.PHOTO, deposit_screenshot)
            ],
        },
        fallbacks=[CallbackQueryHandler(deposit_cancel, pattern="^cancel_deposit$")]
    )

    admin_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_callback_query, pattern="^admin_")],
        states={
            ADMIN_BAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ban_process)],
            ADMIN_UNBAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_unban_process)],
            ADMIN_ADD_BAL_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_bal_user_process)],
            ADMIN_ADD_BAL_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_bal_user_process)],
            ADMIN_ZERO_BAL_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_zero_bal_process)],
            ADMIN_RATE_WA_CL_SET: [MessageHandler(filters.TEXT & ~filters.Command, admin_set_rate_cl_process)],
            ADMIN_RATE_WA_HK_SET: [MessageHandler(filters.TEXT & ~filters.Command, admin_set_rate_hk_process)],
            ADMIN_BROADCAST: [MessageHandler(filters.TEXT & ~filters.Command, admin_broadcast_process)],
        },
        fallbacks=[]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(sub_start_5, pattern="^buy_sub_5$"))
    app.add_handler(CallbackQueryHandler(sub_start_7, pattern="^buy_sub_7$"))
    app.add_handler(deposit_handler)
    app.add_handler(admin_handler)
    app.add_handler(CallbackQueryHandler(admin_deposit_callback, pattern="^(app_dep|rej_dep):"))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.Command, handle_messages))

    logging.info("Starting Telegram Bot...")
    app.run_polling()

if __name__ == "__main__":
    main()
