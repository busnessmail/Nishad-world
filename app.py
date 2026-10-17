import logging
import os
import threading
import asyncio
import re
from datetime import datetime, timedelta, timezone
from flask import Flask
from pymongo import MongoClient
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

# Timezone Definition for Bangladesh (UTC+6)
BD_TIMEZONE = timezone(timedelta(hours=6))

def get_bd_time():
    return datetime.now(BD_TIMEZONE)

def get_bd_date_str():
    return get_bd_time().strftime("%Y-%m-%d")

# Environment Variables & Config
BOT_TOKEN = os.getenv("BOT_TOKEN")
VAK_SMS_API_KEY = os.getenv("VAK_SMS_API_KEY", "91b1a1918e4c47c2a9accf9b19c9226d")

# Support multiple admins (comma-separated string parsed into a list of ints)
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "123456789")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()]

OTP_GROUP_ID = os.getenv("OTP_GROUP_ID")
BINANCE_ID = os.getenv("BINANCE_ID", "1102671200")
# পেমেন্টের জন্য বিকাশ ও নগদ নম্বর
PAYMENT_NUMBER = os.getenv("PAYMENT_NUMBER", "01XXXXXXXXX") 
MONGODB_URI = os.getenv("MONGODB_URI")

# MongoDB Setup
if not MONGODB_URI:
    logging.error("❌ MONGODB_URI Environment Variable missing!")
client = MongoClient(MONGODB_URI)
db = client["hongkong_cl_wa_bot_db"]

users_col = db["users"]
settings_col = db["settings"]
deposits_col = db["pending_deposits"]
daily_stats_col = db["daily_stats"]  
daily_sub_stats_col = db["daily_sub_stats"]  # নতুন কালেকশন ডেইলি সাবস্ক্রিপশন স্ট্যাটস এর জন্য

# Helper Emoji Generator Function (Telegram Custom Emojis)
def emoji(emoji_id: str, fallback: str = "✨") -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

# Premium Emoji Mapping
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

# Flask Web Server
flask_app = Flask("")

@flask_app.route("/")
def home():
    return "HongKong & Chile WA Telegram Bot is Active!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

# In-Memory Active Orders
active_orders = {}

# Conversation States
WAITING_AMOUNT, WAITING_TXID, WAITING_SCREENSHOT = range(3)
SUB_DAYS, SUB_TXID, SUB_SCREENSHOT = range(3, 6)
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

# Helper Functions
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
            "sub_days": 3
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

def increment_daily_sub_stat(days: int, amount: float):
    today_str = get_bd_date_str()
    day_key = f"sub_{days}_count"
    daily_sub_stats_col.update_one(
        {"date": today_str},
        {
            "$inc": {
                day_key: 1,
                "total_count": 1,
                "total_amount": amount
            }
        },
        upsert=True
    )

# Keyboards
def get_main_keyboard(user_id):
    keyboard = [
        [KeyboardButton("💳 𝙰𝙲𝙲𝙾𝚄𝙽𝚃 𝙱𝙰𝙻𝙰𝙽𝙲𝙴"), KeyboardButton("🛒 𝙱𝚈 𝙽𝚄𝙼𝙱𝙴𝚁")],
        [KeyboardButton("🌐 𝚂𝙴𝙻𝙴𝙲𝚃 𝙲𝙾𝚄𝙽𝚃𝚁𝚈"), KeyboardButton("👤 𝙼𝚈 𝙿𝚁𝙾𝙵𝙸𝙻𝙴")],
        [KeyboardButton("💵 𝙳𝙸𝙿𝙾𝚂𝙸𝚃")]
    ]
    if is_admin(user_id):
        keyboard.append([KeyboardButton("⚙️ 𝙰𝙳𝙼𝙸𝙽 𝙿𝙰𝙽𝙴𝙻")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# VAK-SMS API Functions
def set_number_status(id_num: str, status: str):
    url = f"https://vak-sms.com/api/setStatus/?apiKey={VAK_SMS_API_KEY}&idNum={id_num}&status={status}"
    try:
        return requests.get(url, timeout=5).json()
    except Exception as e:
        logging.error(f"Error setting status for {id_num}: {e}")
        return {"error": str(e)}

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

# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    u_data = get_or_create_user(user_id, user.full_name)

    if u_data.get("is_banned", False):
        await update.message.reply_text(f"{E_BAN} 𝙱𝙰𝙽 𝙱𝚈 𝙰𝙳𝙼𝙸𝙽 𝙲𝙾𝙽𝚃𝙰𝙲𝚃 𝙰𝙳𝙼𝙸𝙽.", parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
        return

    if not is_bot_active() and not is_admin(user_id):
        await update.message.reply_text("🚧 <b>ʙᴏᴛ ᴜɴᴅᴇʀ ᴍᴀɪɴᴛᴀɪɴɪɴɢ ʙʏ ᴀᴅᴍɪɴ.</b> ᴘʟᴇᴀsᴇ ᴛʀʏ sᴏᴍᴇ ᴛɪᴍᴇ.", parse_mode="HTML")
        return

    if not is_subscribed(user_id):
        sub_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔥 ৩ দিন = ২০ টাকা", callback_data="buy_sub_3")],
            [InlineKeyboardButton("🔥 ১৫ দিন = ১০০ টাকা", callback_data="buy_sub_15")],
            [InlineKeyboardButton("🔥 ৩০ দিন = ১৭০ টাকা", callback_data="buy_sub_30")]
        ])
        msg = (
            f"👋 Hello {user.full_name}!\n\n"
            f"{E_BAN} <b>YOU DO NOT HAVE AN ACTIVE SUBSCRIPTION!</b> {E_SUB_EXP}\n"
            f"Bot bebohar korte chaile subscription nite hobe.\n\n"
            f"📌 SUBSCRIPTION PLANS (Bkash / Nagad):\n"
            f"• ৩ দিন = ২০ টাকা\n"
            f"• ১৫ দিন = ১০০ টাকা\n"
            f"• ৩০ দিন = ১৭০ টাকা\n\n"
            f"Nicher button-e click kore subscription kinun:"
        )
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text("👇 <b>BUY SUBSCRIPTION:</b>", parse_mode="HTML", reply_markup=sub_kb)
        return

    welcome_msg = (
        f"👋 <b>𝚆𝙴𝙻𝙲𝙾𝙼𝙴 WHATSAPP 𝙱𝙾𝚃!</b> {E_WELCOME}\n\n"
        f"⚙️ <b>HUNTER ZONE NUMBER</b> {E_PHONE}"
    )
    await update.message.reply_text(welcome_msg, parse_mode="HTML", reply_markup=get_main_keyboard(user_id))

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    u_data = get_or_create_user(user_id, update.effective_user.full_name)

    if u_data.get("is_banned", False):
        await update.message.reply_text(f"{E_BAN} 𝙱𝙰𝙽 𝙱𝚈 𝙰𝙳𝙼𝙸𝙽 𝙲𝙾𝙽𝚃𝙰𝙲𝚃 𝙰𝙳𝙼𝙸𝙽.", parse_mode="HTML")
        return

    if not is_bot_active() and not is_admin(user_id):
        await update.message.reply_text("🚧 <b>ʙᴏᴛ ᴜɴᴅᴇʀ ᴍᴀɪɴᴛᴀɪɴɪɴɢ ʙʏ ᴀᴅᴍɪɴ.</b> ᴘʟᴇᴀsᴇ ᴛʀʏ sᴏᴍᴇ ᴛɪᴍᴇ.", parse_mode="HTML")
        return

    if not is_subscribed(user_id):
        sub_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔥 ৩ দিন = ২০ টাকা", callback_data="buy_sub_3")],
            [InlineKeyboardButton("🔥 ১৫ দিন = ১০০ টাকা", callback_data="buy_sub_15")],
            [InlineKeyboardButton("🔥 ৩০ দিন = ১৭০ টাকা", callback_data="buy_sub_30")]
        ])
        await update.message.reply_text(f"{E_BAN} <b>SUBSCRIPTION EXPIRED! BUY NEW SUBSCRIPTION.</b>", parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text("👇 <b>BUY SUBSCRIPTION:</b>", parse_mode="HTML", reply_markup=sub_kb)
        return

    # User Buttons
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
            await asyncio.sleep(180)  # ৩ মিনিট
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

                    # ডেইলি স্ট্যাটস আপডেট (সফল OTP রিসিভ হলে)
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
            [InlineKeyboardButton("💰 SUBSCRIPTION STATS", callback_data="admin_sub_stats")],
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
                [InlineKeyboardButton("💰 SUBSCRIPTION STATS", callback_data="admin_sub_stats")],
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

        elif data == "admin_sub_stats":
            stats_cursor = daily_sub_stats_col.find({}).sort("date", -1).limit(10)
            stats_list = list(stats_cursor)

            if not stats_list:
                await query.message.reply_text("💰 <b>NO SUBSCRIPTION STATS FOUND YET.</b>", parse_mode="HTML")
                return

            msg = "💰 <b>DAILY SUBSCRIPTION STATISTICS (BD TIME):</b>\n\n"
            for st in stats_list:
                d_date = st.get("date", "N/A")
                c_3 = st.get("sub_3_count", 0)
                c_15 = st.get("sub_15_count", 0)
                c_30 = st.get("sub_30_count", 0)
                tot_count = st.get("total_count", 0)
                tot_amt = st.get("total_amount", 0.0)

                msg += (
                    f"📅 <b>Date:</b> <code>{d_date}</code>\n"
                    f"🔥 <b>৩ দিন (২০৳):</b> <code>{c_3}</code> Pcs\n"
                    f"🔥 <b>১৫ দিন (১০০৳):</b> <code>{c_15}</code> Pcs\n"
                    f"🔥 <b>৩০ দিন (১৭০৳):</b> <code>{c_30}</code> Pcs\n"
                    f"📊 <b>Total Sold:</b> <code>{tot_count}</code> Pcs\n"
                    f"💵 <b>Total Amount:</b> <code>{tot_amt:.2f}</code> BDT\n"
                    f"----------------------------\n"
                )

            await query.message.reply_text(msg, parse_mode="HTML")

        elif data == "admin_zero_all_bal":
            users_col.update_many({}, {"$set": {"balance": 0.0}})
            await query.message.reply_text("🧹 <b>SUCCESS! ALL USERS' BALANCES HAVE BEEN RESET TO 0.00 USDT.</b>", parse_mode="HTML")

        elif data == "admin_reset_all_subs":
            users_col.update_many({}, {"$set": {"subscription_expiry": None}})
            await query.message.reply_text("💥 <b>SUCCESS! ALL USERS' SUBSCRIPTIONS HAVE BEEN RESET/CANCELLED.</b>\nNow all regular users will be asked to buy a subscription when they start or use the bot.", parse_mode="HTML")

        elif data == "admin_view_users":
            all_users = list(users_col.find({}))
            total_users = len(all_users)
            
            if total_users == 0:
                await query.message.reply_text("📋 <b>NO USERS FOUND.</b>", parse_mode="HTML")
                return

            sub_3_users = []
            sub_15_users = []
            sub_30_users = []
            other_users = []

            for u in all_users:
                days = u.get("sub_days")
                if is_subscribed(u.get("user_id")):
                    if days == 3:
                        sub_3_users.append(u)
                    elif days == 15:
                        sub_15_users.append(u)
                    elif days == 30:
                        sub_30_users.append(u)
                    else:
                        other_users.append(u)
                else:
                    other_users.append(u)

            text_msg = f"📊 <b>TOTAL BOT USERS:</b> <code>{total_users}</code>\n\n"

            def format_user_list(title, u_list):
                if not u_list:
                    return ""
                res = f"<b>{title} ({len(u_list)} Users):</b>\n"
                for u in u_list:
                    res += (
                        f"• {u.get('full_name', 'N/A')} (ID: <code>{u.get('user_id')}</code>)\n"
                        f"  Bal: <code>{u.get('balance', 0.0):.4f}</code> USDT | OTP: <code>{u.get('otp_count', 0)}</code>\n"
                    )
                return res + "\n"

            text_msg += format_user_list("🔥 ৩ দিনের সাবস্ক্রিপশন ব্যবহারকারী", sub_3_users)
            text_msg += format_user_list("🔥 ১৫ দিনের সাবস্ক্রিপশন ব্যবহারকারী", sub_15_users)
            text_msg += format_user_list("🔥 ৩০ দিনের সাবস্ক্রিপশন ব্যবহারকারী", sub_30_users)
            text_msg += format_user_list("📌 অন্যান্য / মেয়াদোত্তীর্ণ ব্যবহারকারী", other_users)

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

# Subscription Purchase Flow Handlers (Bkash / Nagad Based)
async def sub_start_3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    users_col.update_one({"user_id": user_id}, {"$set": {"sub_days": 3}})
    
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_sub_flow")]])
    msg = (
        f"🔥 <b>SUBSCRIPTION PLAN:</b> ৩ দিন\n"
        f"💰 <b>AMOUNT:</b> ২০ টাকা\n\n"
        f"👇 <b>SEND PAYMENT VIA BKASH / NAGAD (Send Money):</b>\n"
        f"📱 <b>NUMBER:</b> <code>{PAYMENT_NUMBER}</code> {E_ID}\n\n"
        f"Payment kora sese apnar TrxID-ti likhe pathan:"
    )
    await query.message.reply_text(msg, parse_mode="HTML", reply_markup=cancel_kb)
    return SUB_TXID

async def sub_start_15(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    users_col.update_one({"user_id": user_id}, {"$set": {"sub_days": 15}})
    
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_sub_flow")]])
    msg = (
        f"🔥 <b>SUBSCRIPTION PLAN:</b> ১৫ দিন\n"
        f"💰 <b>AMOUNT:</b> ১০০ টাকা\n\n"
        f"👇 <b>SEND PAYMENT VIA BKASH / NAGAD (Send Money):</b>\n"
        f"📱 <b>NUMBER:</b> <code>{PAYMENT_NUMBER}</code> {E_ID}\n\n"
        f"Payment kora sese apnar TrxID-ti likhe pathan:"
    )
    await query.message.reply_text(msg, parse_mode="HTML", reply_markup=cancel_kb)
    return SUB_TXID

async def sub_start_30(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    users_col.update_one({"user_id": user_id}, {"$set": {"sub_days": 30}})
    
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_sub_flow")]])
    msg = (
        f"🔥 <b>SUBSCRIPTION PLAN:</b> ৩০ দিন\n"
        f"💰 <b>AMOUNT:</b> ১৭০ টাকা\n\n"
        f"👇 <b>SEND PAYMENT VIA BKASH / NAGAD (Send Money):</b>\n"
        f"📱 <b>NUMBER:</b> <code>{PAYMENT_NUMBER}</code> {E_ID}\n\n"
        f"Payment kora sese apnar TrxID-ti likhe pathan:"
    )
    await query.message.reply_text(msg, parse_mode="HTML", reply_markup=cancel_kb)
    return SUB_TXID

async def sub_txid_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txid = update.message.text.strip()
    context.user_data["sub_txid"] = txid
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_sub_flow")]])
    await update.message.reply_text("📸 <b>Ekhon apnar payment-er screenshot (Photo) Pathan:</b>", parse_mode="HTML", reply_markup=cancel_kb)
    return SUB_SCREENSHOT

async def sub_screenshot_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    photo = update.message.photo[-1]
    txid = context.user_data.get("sub_txid")
    user_doc = users_col.find_one({"user_id": user.id})
    days = user_doc.get("sub_days", 3) if user_doc else 3
    
    if days == 3:
        price_str = "২০ টাকা"
    elif days == 15:
        price_str = "১০০ টাকা"
    else:
        price_str = "১৭০ টাকা"

    admin_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ APPROVED", callback_data=f"approve_sub_{user.id}"),
            InlineKeyboardButton("❌ REJECTED", callback_data=f"reject_sub_{user.id}")
        ]
    ])

    caption = (
        f"🔔 <b>NEW SUBSCRIPTION REQUEST (BKASH/NAGAD)!</b>\n\n"
        f"👤 <b>USER:</b> {user.full_name} (<code>{user.id}</code>) {E_ID}\n"
        f"💳 <b>METHOD:</b> BKASH / NAGAD\n"
        f"📅 <b>PLAN:</b> {days} Days {E_SUB_VALID}\n"
        f"💰 <b>AMOUNT:</b> {price_str}\n"
        f"🧾 <b>TrxID:</b> <code>{txid}</code>"
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(chat_id=admin_id, photo=photo.file_id, caption=caption, parse_mode="HTML", reply_markup=admin_kb)
        except Exception:
            pass

    await update.message.reply_text("✅ Apnar subscription request admin-er kache pathano hoyeche! Admin approve korlei bot active hoye jaabe.")
    return ConversationHandler.END

async def cancel_sub_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass
    return ConversationHandler.END

# Deposit Conversation Handlers
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
        if query:
            await query.message.reply_text(f"{E_BAN} 𝙱𝙰𝙽 𝙱𝚈 𝙰𝙳𝙼𝙸𝙽 𝙲𝙾𝙽𝚃𝙰𝙲𝚃 𝙰𝙳𝙼𝙸𝙽.", parse_mode="HTML")
        else:
            await update.message.reply_text(f"{E_BAN} 𝙱𝙰𝙽 𝙱𝚈 𝙰𝙳𝙼𝙸𝙽 𝙲𝙾𝙽𝚃𝙰𝙲𝚃 𝙰𝙳𝙼𝙸𝙽.", parse_mode="HTML")
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
            await update.message.reply_text("❌ <b>MINIMUM DEPOSIT IS $0.11 USDT!</b>\nPlease enter an amount equal to or greater than $0.11:", parse_mode="HTML", reply_markup=cancel_kb)
            return WAITING_AMOUNT

        context.user_data["dep_amount"] = amount
        await update.message.reply_text("📥 <b>𝚂𝙴𝙽𝙳 𝚈𝙾𝚄𝚁 𝙱𝙸𝙽𝙰𝙽𝙲𝙴 𝙿𝙰𝚈 𝚃𝚇𝙸𝙳 / 𝙾𝚁𝙳𝙴𝚁 ID:</b>", parse_mode="HTML", reply_markup=cancel_kb)
        return WAITING_TXID
    except ValueError:
        await update.message.reply_text("❌ INVALID AMOUNT. PLEASE ENTER A NUMBER (E.G. 0.50):", parse_mode="HTML", reply_markup=cancel_kb)
        return WAITING_AMOUNT

async def deposit_txid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txid = update.message.text
    context.user_data["dep_txid"] = txid
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

    await update.message.reply_text(f"{E_UNBAN} <b>𝙳𝙴𝙿𝙾𝚂𝙸𝚃 𝚁𝙴𝚀𝚄𝙴𝚂𝚃 𝚂𝚄𝙱𝙼𝙸𝚃𝚃𝙴𝙳!</b>\n𝙰𝚍𝚖𝚒𝚗 𝚠𝚒𝚕𝚕 𝚟𝚎𝚛𝚒𝚏𝚢 𝚊𝚗𝚍 𝚊𝚍𝚍 𝚢𝚘𝚞𝚛 𝚋𝚊𝚕𝚊𝚗𝚌𝚎 𝚜𝚘𝚘𝚗.", parse_mode="HTML")

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
        except Exception as e:
            logging.error(f"Failed to send deposit to admin {admin_id}: {e}")

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

# Admin Conversation Handlers
async def admin_ban_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target_id = int(update.message.text)
        users_col.update_one({"user_id": target_id}, {"$set": {"is_banned": True}})
        await update.message.reply_text(f"{E_UNBAN} USER <code>{target_id}</code> HAS BEEN BANNED.", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ INVALID USER ID.")
    return ConversationHandler.END

async def admin_unban_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target_id = int(update.message.text)
        users_col.update_one({"user_id": target_id}, {"$set": {"is_banned": False}})
        await update.message.reply_text(f"{E_UNBAN} USER <code>{target_id}</code> HAS BEEN UNBANNED.", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ INVALID USER ID.")
    return ConversationHandler.END

async def admin_add_bal_user_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target_id = int(update.message.text)
        context.user_data["target_user_id"] = target_id
        await update.message.reply_text("SEND AMOUNT TO ADD:")
        return ADMIN_ADD_BAL_AMT
    except ValueError:
        await update.message.reply_text("❌ INVALID USER ID.")
        return ConversationHandler.END

async def admin_add_bal_amt_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(update.message.text)
        target_id = context.user_data.get("target_user_id")
        users_col.update_one({"user_id": target_id}, {"$inc": {"balance": amt}})
        await update.message.reply_text(f"{E_UNBAN} ADDED <code>${amt:.2f}</code> USDT TO USER <code>{target_id}</code>.", parse_mode="HTML")
        try:
            await context.bot.send_message(target_id, f"🎉 <b>𝙰𝚙𝚗𝚊𝚛 <code>${amt:.2f}</code> USDT 𝚍𝚎𝚙𝚘𝚜𝚒𝚝 𝚜𝚑𝚘𝚏𝚘𝚕𝚋𝚑𝚊𝚋𝚎 𝚓𝚞𝚔𝚝𝚘 𝚔𝚘𝚛𝚊 𝚑𝚘𝚢𝚎𝚌𝚑𝚎!</b>", parse_mode="HTML")
        except Exception:
            pass
    except ValueError:
        await update.message.reply_text("❌ INVALID AMOUNT.")
    return ConversationHandler.END

async def admin_zero_bal_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target_id = int(update.message.text)
        users_col.update_one({"user_id": target_id}, {"$set": {"balance": 0.0}})
        await update.message.reply_text(f"{E_UNBAN} BALANCE ZEROED FOR USER <code>{target_id}</code>.", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ INVALID USER ID.")
    return ConversationHandler.END

async def admin_set_rate_cl_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_rate = float(update.message.text)
        set_rate("cl", new_rate)
        await update.message.reply_text(f"{E_UNBAN} CHILE WA RATE UPDATED TO: <code>{new_rate}</code> USDT", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ INVALID RATE AMOUNT.")
    return ConversationHandler.END

async def admin_set_rate_hk_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_rate = float(update.message.text)
        set_rate("hk", new_rate)
        await update.message.reply_text(f"{E_UNBAN} HONG KONG WA RATE UPDATED TO: <code>{new_rate}</code> USDT", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ INVALID RATE AMOUNT.")
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
    await update.message.reply_text(f"📢 BROADCAST SENT TO {count} USERS.")
    return ConversationHandler.END

async def admin_deposit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    admin_id = query.from_user.id

    if not is_admin(admin_id):
        return

    parts = data.split(":")
    action = parts[0]
    dep_id = parts[1]
    target_uid = int(parts[2])

    dep_doc = deposits_col.find_one({"dep_id": dep_id})
    if not dep_doc or dep_doc.get("status") != "PENDING":
        await query.message.reply_text("⚠️ <b>THIS DEPOSIT REQUEST WAS ALREADY PROCESSED.</b>", parse_mode="HTML")
        return

    admin_messages = dep_doc.get("admin_messages", [])

    if action == "app_dep":
        amt = float(parts[3])
        users_col.update_one({"user_id": target_uid}, {"$inc": {"balance": amt}})
        deposits_col.update_one({"dep_id": dep_id}, {"$set": {"status": "APPROVED"}})

        for amsg in admin_messages:
            try:
                await context.bot.edit_message_caption(
                    chat_id=amsg["chat_id"],
                    message_id=amsg["message_id"],
                    caption=f"{query.message.caption}\n\n✅ <b>APPROVED BY ADMIN</b>",
                    parse_mode="HTML"
                )
            except Exception as e:
                logging.error(f"Failed to sync approve status to admin {amsg['chat_id']}: {e}")

        try:
            await context.bot.send_message(target_uid, f"🎉 <b>𝙰𝚙𝚗𝚊𝚛 <code>${amt:.2f}</code> USDT 𝚍𝚎𝚙𝚘𝚜𝚒𝚝 𝚜𝚑𝚘𝚏𝚘𝚕𝚋𝚑𝚊𝚋𝚎 𝚓𝚞𝚔𝚝𝚘 𝚔𝚘𝚛𝚊 𝚑𝚘𝚢𝚎𝚌𝚑𝚎!</b>", parse_mode="HTML")
        except Exception:
            pass

    elif action == "rej_dep":
        deposits_col.update_one({"dep_id": dep_id}, {"$set": {"status": "REJECTED"}})

        for amsg in admin_messages:
            try:
                await context.bot.edit_message_caption(
                    chat_id=amsg["chat_id"],
                    message_id=amsg["message_id"],
                    caption=f"{query.message.caption}\n\n❌ <b>REJECTED BY ADMIN</b>",
                    parse_mode="HTML"
                )
            except Exception as e:
                logging.error(f"Failed to sync reject status to admin {amsg['chat_id']}: {e}")

        try:
            await context.bot.send_message(target_uid, "❌ <b>𝙰𝚙𝚗𝚊𝚛 𝚍𝚎𝚙𝚘𝚜𝚒𝚝 𝚛𝚎𝚀𝚞𝚎𝚜𝚝 𝚝𝚒 𝚛𝚎𝚓𝚎𝚌𝚝 𝚔𝚘𝚛𝚊 𝚑𝚘𝚢𝚎𝚌𝚑𝚎!</b>", parse_mode="HTML")
        except Exception:
            pass

# Subscription Admin Approval Callbacks
async def handle_sub_approval_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    admin_id = query.from_user.id

    if not is_admin(admin_id):
        return

    if data.startswith("approve_sub_"):
        target_id = int(data.split("_")[2])
        user_doc = users_col.find_one({"user_id": target_id})
        days = user_doc.get("sub_days", 3) if user_doc else 3
        
        expiry_date = get_bd_time().replace(tzinfo=None) + timedelta(days=days)
        users_col.update_one({"user_id": target_id}, {"$set": {"subscription_expiry": expiry_date}})
        
        # সাবস্ক্রিপশন স্ট্যাটস এবং এমাউন্ট আপডেট (৩ দিন = ২০৳, ১৫ দিন = ১০০৳, ৩০ দিন = ১৭০৳)
        price_val = 20.0 if days == 3 else (100.0 if days == 15 else 170.0)
        increment_daily_sub_stat(days, price_val)

        exp_str = expiry_date.strftime("%Y-%m-%d %H:%M")

        try:
            await query.edit_message_caption(caption=query.message.caption + f"\n\n{E_UNBAN} <b>Subscription Approved ({days} Days Active)! Valid till: {exp_str} BD Time</b>", parse_mode="HTML")
        except Exception:
            pass

        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=f"🎉 <b>Apnar Subscription Approved hoyeche!</b> {days} Diner jonno bot-er sob features active kora hoyeche.\n📅 Valid till: <code>{exp_str}</code> (BD Time)",
                parse_mode="HTML",
                reply_markup=get_main_keyboard(target_id)
            )
        except Exception:
            pass

    elif data.startswith("reject_sub_"):
        target_id = int(data.split("_")[2])
        try:
            await query.edit_message_caption(caption=query.message.caption + f"\n\n{E_CANCEL} <b>Subscription Rejected!</b>", parse_mode="HTML")
        except Exception:
            pass
        try:
            await context.bot.send_message(chat_id=target_id, text="❌ <b>Apnar subscription request-ti batil kora hoyeche.</b>", parse_mode="HTML")
        except Exception:
            pass

def main():
    threading.Thread(target=run_flask, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    # Subscription Flow Handler
    sub_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(sub_start_3, pattern="^buy_sub_3$"),
            CallbackQueryHandler(sub_start_15, pattern="^buy_sub_15$"),
            CallbackQueryHandler(sub_start_30, pattern="^buy_sub_30$")
        ],
        states={
            SUB_TXID: [MessageHandler(filters.TEXT & ~filters.COMMAND, sub_txid_received)],
            SUB_SCREENSHOT: [MessageHandler(filters.PHOTO, sub_screenshot_received)]
        },
        fallbacks=[CallbackQueryHandler(cancel_sub_flow, pattern="^cancel_sub_flow$")]
    )

    # Deposit Conversation Handler
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
        fallbacks=[
            CallbackQueryHandler(deposit_cancel, pattern="^cancel_deposit$")
        ]
    )

    # Admin Action Conversation Handler
    admin_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(handle_callback_query, pattern="^admin_")
        ],
        states={
            ADMIN_BAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ban_process)],
            ADMIN_UNBAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_unban_process)],
            ADMIN_ADD_BAL_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_bal_user_process)],
            ADMIN_ADD_BAL_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_bal_amt_process)],
            ADMIN_ZERO_BAL_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_zero_bal_process)],
            ADMIN_RATE_WA_CL_SET: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_rate_cl_process)],
            ADMIN_RATE_WA_HK_SET: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_rate_hk_process)],
            ADMIN_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_process)],
        },
        fallbacks=[]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(sub_conv)
    app.add_handler(deposit_handler)
    app.add_handler(admin_handler)
    app.add_handler(CallbackQueryHandler(admin_deposit_callback, pattern="^(app_dep|rej_dep):"))
    app.add_handler(CallbackQueryHandler(handle_sub_approval_callbacks, pattern="^(approve_sub_|reject_sub_)"))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))

    logging.info("Starting Telegram Bot...")
    app.run_polling()

if __name__ == "__main__":
    main()
