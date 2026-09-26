import os
import random
import time
import re
import threading
import requests
import phonenumbers
from phonenumbers import geocoder
import telebot
from telebot.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CopyTextButton
)

# ================== CONFIGURATION ==================
BOT_TOKEN = "8989722756:AAE1hh-0ISdZvuNSJdkVemnB_uAtoeHhbqQ"
ADMIN_IDS = {7030761727}

SUPPORT_USERNAME = "Sifat8191"
SUPPORT_URL = f"https://t.me/{SUPPORT_USERNAME}"

OTP_CHANNEL_URL = "https://t.me/+rv1QQTXHFY02YTVl"

API_BASE_URL = "http://147.135.212.197/crapi/had/viewstats"
API_TOKEN = "Qk9YREFBUzRBYmNVYY9QZkdXdnVYh46BXVVWVXWDV0hgVpmHV42KgQ=="

TELEGRAM_OTP_CHAT_ID = "-1004346608192"

RECORDS_TO_FETCH = 50
POLL_INTERVAL = 10

# Auto-expiry time for generated numbers (seconds)
NUMBER_EXPIRY_SECONDS = 25 * 60   # 25 minutes

# ================== BOT INIT ==================
bot = telebot.TeleBot(BOT_TOKEN)

# ================== IN-MEMORY DATA ==================
user_data = {}
pending_withdrawals = []
services = []
countries = []
admin_states = {}
imported_numbers = {}
number_owners = {}
number_meta = {}
otp_rates = {}
user_otp_stats = {}

# Each entry: phone -> {"expires_at": timestamp}
active_numbers = {}

processed_messages = set()
data_lock = threading.Lock()
first_sync_done = False

# ================== KEYBOARDS ==================
def get_main_keyboard(user_id):
    markup = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    markup.add(
        KeyboardButton("☎️GET NUMBERS", style="success"),
        KeyboardButton("💰BALANCE", style="success"),
        KeyboardButton("💸WITHDRAW", style="success"),
        KeyboardButton("🆘 Support", style="success"),
    )
    if is_admin(user_id):
        markup.add(KeyboardButton("🛠 Admin Panel", style="success"))
    return markup

def get_admin_keyboard():
    markup = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    markup.add(
        KeyboardButton("📥 Import Numbers", style="success"),
        KeyboardButton("📊 Bot Stats", style="success"),
        KeyboardButton("💳 Add Balance", style="success"),
        KeyboardButton("💵 Add Rate", style="success"),
        KeyboardButton("📋 View Rates", style="success"),
        KeyboardButton("🗑️ Delete Rate", style="success"),
        KeyboardButton("📢 Broadcast", style="success"),
        KeyboardButton("⏳ Pending Withdrawals", style="success"),
        KeyboardButton("📝 Add Service", style="success"),
        KeyboardButton("🌍 Add Country", style="success"),
        KeyboardButton("🗑️ Delete Service", style="success"),
        KeyboardButton("🗑️ Delete Country", style="success"),
        KeyboardButton("🗑️ Delete Numbers", style="success"),
        KeyboardButton("📋 View Services", style="success"),
        KeyboardButton("🌐 View Countries", style="success"),
        KeyboardButton("📁 View Imported Numbers", style="success"),
        KeyboardButton("⬅️ User Menu", style="success"),
    )
    return markup

def get_service_keyboard():
    markup = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    if not services:
        markup.add(KeyboardButton("❌ No Services Available", style="success"))
    else:
        for service in services:
            markup.add(KeyboardButton(service, style="success"))
    markup.add(KeyboardButton("⬅️ Back", style="success"))
    return markup

def get_country_keyboard():
    markup = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    if not countries:
        markup.add(KeyboardButton("❌ No Countries Available", style="success"))
    else:
        for country in countries:
            markup.add(KeyboardButton(country, style="success"))
    markup.add(KeyboardButton("⬅️ Back", style="success"))
    return markup

# ================== HELPERS ==================
def is_admin(user_id):
    return user_id in ADMIN_IDS

def send_admin_panel(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ You are not authorized!")
        return
    admin_states.pop(message.from_user.id, None)
    bot.reply_to(
        message,
        "🛠 **Admin Panel**\n\n📌 Available Options:",
        reply_markup=get_admin_keyboard(),
        parse_mode='Markdown'
    )

def finish_admin_action(user_id):
    admin_states.pop(user_id, None)

def ensure_user(user_id):
    if user_id not in user_data:
        user_data[user_id] = {
            "balance": 0,
            "number": None,
            "service": None,
            "country": None,
            "numbers": []
        }
    today = time.strftime("%Y-%m-%d")
    if user_id not in user_otp_stats:
        user_otp_stats[user_id] = {"total": 0, "today": 0, "last_date": today}

def parse_numbers_from_bytes(raw_bytes):
    try:
        content = raw_bytes.decode('utf-8', errors='ignore')
        numbers = []
        for chunk in content.replace(',', '\n').replace(';', '\n').split('\n'):
            chunk = chunk.strip()
            if not chunk:
                continue
            if ' ' in chunk or '\t' in chunk:
                for num in chunk.replace('\t', ' ').split():
                    num = num.strip()
                    if num:
                        numbers.append(num)
            else:
                numbers.append(chunk)
        seen = set()
        clean = []
        for n in numbers:
            if n not in seen:
                seen.add(n)
                clean.append(n)
        return clean
    except Exception as e:
        print(f"PARSE ERROR: {e}")
        return None

def mask_number(num):
    if not num:
        return "XXXX"
    clean = re.sub(r'\D', '', str(num))
    if len(clean) <= 8:
        if len(clean) <= 4:
            return clean
        return clean[:2] + "XXXX" + clean[-2:]
    return clean[:4] + "XXXX" + clean[-4:]

def number_with_plus(num):
    """Ensure the number is shown with a leading + sign."""
    if not num:
        return ""
    clean = str(num).strip()
    if not clean.startswith('+'):
        clean = '+' + clean
    return clean

def has_owner_otp(phone_number):
    """Check whether the owner already received an OTP for this number."""
    with data_lock:
        owner = number_owners.get(str(phone_number).strip())
        if not owner:
            return False
        stats = user_otp_stats.get(owner, {})
        # A number is 'used' if the owner exists and the number is not in active_numbers
        return str(phone_number).strip() not in active_numbers

def expire_loop():
    """Background thread: remove expired numbers from active_numbers."""
    print("⏱️ Expiry thread started (25 min).")
    while True:
        try:
            now = time.time()
            with data_lock:
                expired = [n for n, d in active_numbers.items() if d["expires_at"] <= now]
                for n in expired:
                    active_numbers.pop(n, None)
                    # Also remove ownership so an expired number is freed up
                    if n in number_owners:
                        number_owners.pop(n, None)
                    if n in number_meta:
                        number_meta.pop(n, None)
                if expired:
                    print(f"⏰ Expired {len(expired)} number(s): {expired}")
        except Exception as e:
            print(f"EXPIRY ERROR: {e}")
        time.sleep(60)

# ================== TELEGRAM COMMANDS ==================
@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    ensure_user(user_id)
    bot.reply_to(
        message,
        "👋 **Welcome to Number Bot!**\n\n"
        "☎️ Click 'GET NUMBERS' to get numbers\n"
        "💰 Check your balance\n"
        "💸 Withdraw your balance\n"
        "🆘 Contact support if you need help",
        reply_markup=get_main_keyboard(user_id),
        parse_mode='Markdown'
    )

@bot.message_handler(commands=['admin'])
def admin_command(message):
    send_admin_panel(message)

# ================== MAIN HANDLER ==================
@bot.message_handler(func=lambda message: True)
def handle_buttons(message):
    try:
        user_id = message.from_user.id
        text = message.text
        ensure_user(user_id)

        # ===== SUPPORT =====
        if text == "🆘 Support":
            markup = InlineKeyboardMarkup(row_width=1)
            markup.add(InlineKeyboardButton(
                text="💬 Contact Support",
                url=SUPPORT_URL,
                style="success"
            ))
            markup.add(InlineKeyboardButton(
                text="❌ Close",
                callback_data=f"support_close|{user_id}",
                style="success"
            ))
            bot.reply_to(
                message,
                "💬 **Contact us for any help:**",
                reply_markup=markup,
                parse_mode='Markdown'
            )
            return

        # ===== USER WITHDRAW FLOW =====
        wd_state = admin_states.get(user_id)

        if wd_state and wd_state.startswith("wd_number|||"):
            method = wd_state.split("|||", 1)[1]
            bkash_number = text.strip()
            if not re.match(r'^01[3-9]\d{8}$', bkash_number):
                bot.reply_to(message, f"❌ **Invalid {method} number!**\n\nPlease enter a valid 11-digit number.", parse_mode='Markdown')
                return
            balance = user_data.get(user_id, {}).get("balance", 0)
            if balance <= 0:
                admin_states.pop(user_id, None)
                bot.reply_to(message, "❌ No balance!", reply_markup=get_main_keyboard(user_id))
                return
            admin_states[user_id] = f"wd_amount|||{method}|||{bkash_number}"
            bot.reply_to(message, f"✅ {method} Number: `{bkash_number}`\n\n💰 **Enter withdraw amount** (in BDT):\n\nAvailable: {balance:.2f} BDT\n\n_Example: 50_", parse_mode='Markdown')
            return

        if wd_state and wd_state.startswith("wd_amount|||"):
            parts = wd_state.split("|||")
            method = parts[1]
            bkash_number = parts[2]
            try:
                amount = float(text.strip())
                if amount <= 0:
                    raise ValueError
            except:
                bot.reply_to(message, "❌ Invalid amount! Enter a number (e.g. 50).")
                return
            balance = user_data.get(user_id, {}).get("balance", 0)
            if amount > balance:
                bot.reply_to(message, f"❌ **Insufficient balance!**\n\nAvailable: {balance:.2f} BDT\nRequested: {amount:.2f} BDT", parse_mode='Markdown')
                return
            pending_withdrawals.append({
                "user_id": user_id, "amount": amount, "method": method, "number": bkash_number,
                "service": user_data[user_id].get("service", "Unknown"),
                "country": user_data[user_id].get("country", "Unknown")
            })
            user_data[user_id]["balance"] -= amount
            admin_states.pop(user_id, None)
            for admin_id in ADMIN_IDS:
                try:
                    bot.send_message(admin_id, f"🔔 **New Withdrawal Request!**\n\n👤 User: `{user_id}`\n💰 Amount: {amount:.2f} BDT\nMethod: {method}\n📞 Number: `{bkash_number}`", parse_mode='Markdown')
                except:
                    pass
            bot.reply_to(message, f"✅ **Withdrawal Request Submitted!**\n\n💰 Amount: {amount:.2f} BDT\nMethod: {method}\n📞 Number: `{bkash_number}`\n\n⏳ Status: Pending admin approval", reply_markup=get_main_keyboard(user_id), parse_mode='Markdown')
            return

        # ===== ADMIN PANEL =====
        if text == "🛠 Admin Panel":
            send_admin_panel(message)
            return

        if is_admin(user_id):
            # IMPORT NUMBERS
            if text == "📥 Import Numbers":
                if not services:
                    bot.reply_to(message, "❌ No services! Add a service first.", reply_markup=get_admin_keyboard())
                    return
                if not countries:
                    bot.reply_to(message, "❌ No countries! Add a country first.", reply_markup=get_admin_keyboard())
                    return
                admin_states[user_id] = "import_select_service"
                bot.reply_to(message, "📥 **Step 1/3: Select Service**", reply_markup=get_service_keyboard(), parse_mode='Markdown')
                return

            # ADD RATE
            if text == "💵 Add Rate":
                if not services:
                    bot.reply_to(message, "❌ No services! Add a service first.", reply_markup=get_admin_keyboard())
                    return
                if not countries:
                    bot.reply_to(message, "❌ No countries! Add a country first.", reply_markup=get_admin_keyboard())
                    return
                admin_states[user_id] = "rate_select_service"
                bot.reply_to(message, "💵 **Add Rate — Step 1/3: Select Service**", reply_markup=get_service_keyboard(), parse_mode='Markdown')
                return

            # VIEW RATES
            if text == "📋 View Rates":
                if not otp_rates:
                    bot.reply_to(message, "❌ No rates set yet!", reply_markup=get_admin_keyboard())
                    return
                lines = ["💵 **OTP Rates**\n━━━━━━━━━━━━━━━━"]
                for key, rate in otp_rates.items():
                    service, country = key.split("_", 1) if "_" in key else (key, "?")
                    lines.append(f"{service} | {country}: **{rate}** Taka")
                lines.append("━━━━━━━━━━━━━━━━")
                lines.append(f"Total: {len(otp_rates)}")
                bot.reply_to(message, "\n".join(lines), reply_markup=get_admin_keyboard(), parse_mode='Markdown')
                return

            # DELETE RATE
            if text == "🗑️ Delete Rate":
                if not otp_rates:
                    bot.reply_to(message, "❌ No rates to delete!", reply_markup=get_admin_keyboard())
                    return
                admin_states[user_id] = "rate_delete_confirm"
                lines = ["🗑️ **Select rate to delete:**\n"]
                idx = 1
                for key, rate in otp_rates.items():
                    service, country = key.split("_", 1) if "_" in key else (key, "?")
                    lines.append(f"{idx}. {service} | {country} → {rate}")
                    idx += 1
                lines.append("\nType the number (e.g. 1) to delete.")
                bot.reply_to(message, "\n".join(lines), reply_markup=get_admin_keyboard())
                return

            # DELETE SERVICE
            if text == "🗑️ Delete Service":
                if not services:
                    bot.reply_to(message, "❌ No services to delete!", reply_markup=get_admin_keyboard())
                    return
                admin_states[user_id] = "delete_service"
                service_list = "\n".join([f"{i+1}. {s}" for i, s in enumerate(services)])
                bot.reply_to(message, f"🗑️ **Select service to delete:**\n\n{service_list}\n\nType the number or the service name.", reply_markup=get_admin_keyboard())
                return

            # DELETE COUNTRY
            if text == "🗑️ Delete Country":
                if not countries:
                    bot.reply_to(message, "❌ No countries to delete!", reply_markup=get_admin_keyboard())
                    return
                admin_states[user_id] = "delete_country"
                country_list = "\n".join([f"{i+1}. {c}" for i, c in enumerate(countries)])
                bot.reply_to(message, f"🗑️ **Select country to delete:**\n\n{country_list}\n\nType the number or the country name.", reply_markup=get_admin_keyboard())
                return

            # DELETE NUMBERS
            if text == "🗑️ Delete Numbers":
                admin_states[user_id] = "delete_numbers_confirm"
                bot.reply_to(message, "⚠️ **Are you sure?**\n\nType **YES** to confirm.", reply_markup=get_admin_keyboard(), parse_mode='Markdown')
                return

            # VIEW IMPORTED
            if text == "📁 View Imported Numbers":
                view_imported_numbers(message)
                return

            # PENDING WITHDRAWALS
            if text == "⏳ Pending Withdrawals":
                handle_pending_withdrawals(message)
                return

            # ADD SERVICE
            if text == "📝 Add Service":
                admin_states[user_id] = "add_service"
                bot.reply_to(message, "📝 Enter service name:", parse_mode='Markdown')
                return

            # ADD COUNTRY
            if text == "🌍 Add Country":
                admin_states[user_id] = "add_country"
                bot.reply_to(message, "🌍 Enter country name:", parse_mode='Markdown')
                return

            # VIEW SERVICES
            if text == "📋 View Services":
                if not services:
                    bot.reply_to(message, "❌ No services!", reply_markup=get_admin_keyboard())
                else:
                    service_list = "\n".join([f"• {i+1}. {s}" for i, s in enumerate(services)])
                    bot.reply_to(message, f"📋 Services:\n\n{service_list}\n\nTotal: {len(services)}", reply_markup=get_admin_keyboard())
                return

            # VIEW COUNTRIES
            if text == "🌐 View Countries":
                if not countries:
                    bot.reply_to(message, "❌ No countries!", reply_markup=get_admin_keyboard())
                else:
                    country_list = "\n".join([f"• {i+1}. {c}" for i, c in enumerate(countries)])
                    bot.reply_to(message, f"🌐 Countries:\n\n{country_list}\n\nTotal: {len(countries)}", reply_markup=get_admin_keyboard())
                return

            # BOT STATS
            if text == "📊 Bot Stats":
                total_users = len(user_data)
                total_balance = sum(d.get("balance", 0) for d in user_data.values())
                total_imported = sum(len(nums) for nums in imported_numbers.values())
                bot.reply_to(message,
                    f"📊 **Statistics**\n\n"
                    f"👥 Users: {total_users}\n"
                    f"💰 Balance: {total_balance:.2f} Taka\n"
                    f"📱 Numbers: {total_imported}\n"
                    f"⏳ Pending: {len(pending_withdrawals)}\n"
                    f"📝 Services: {len(services)}\n"
                    f"🌍 Countries: {len(countries)}\n"
                    f"💵 Rates set: {len(otp_rates)}\n"
                    f"🎯 Owners tracked: {len(number_owners)}\n"
                    f"⏱️ Active numbers: {len(active_numbers)}",
                    reply_markup=get_admin_keyboard(), parse_mode='Markdown')
                return

            # ADD BALANCE
            if text == "💳 Add Balance":
                admin_states[user_id] = "add_balance"
                bot.reply_to(message, "Format: user_id amount\n\nExample: 7030761727 100")
                return

            # BROADCAST
            if text == "📢 Broadcast":
                admin_states[user_id] = "broadcast"
                bot.reply_to(message, "📢 Enter broadcast message:", parse_mode='Markdown')
                return

            # USER MENU
            if text == "⬅️ User Menu":
                finish_admin_action(user_id)
                bot.reply_to(message, "✅ Back to user menu", reply_markup=get_main_keyboard(user_id))
                return

            # ADMIN STATES
            state = admin_states.get(user_id)

            if state == "import_select_service" and text in services:
                service = text.strip()
                admin_states[user_id] = f"import_select_country|||{service}"
                bot.reply_to(message, f"✅ Selected: {service}\n\n📥 **Step 2/3: Select Country**", reply_markup=get_country_keyboard(), parse_mode='Markdown')
                return

            if state and state.startswith("import_select_country|||") and text in countries:
                country = text.strip()
                service = state.split("|||", 1)[1]
                admin_states[user_id] = f"import_file|||{service}|||{country}"
                bot.reply_to(message, f"✅ {service} | {country}\n\n📥 **Step 3/3: Send .txt file**", parse_mode='Markdown')
                return

            if state and state.startswith("import_file|||") and text == "⬅️ Back":
                finish_admin_action(user_id)
                bot.reply_to(message, "✅ Import cancelled!", reply_markup=get_admin_keyboard())
                return

            if state == "rate_select_service" and text in services:
                service = text.strip()
                admin_states[user_id] = f"rate_select_country|||{service}"
                bot.reply_to(message, f"✅ Service: {service}\n\n💵 **Step 2/3: Select Country**", reply_markup=get_country_keyboard(), parse_mode='Markdown')
                return

            if state and state.startswith("rate_select_country|||") and text in countries:
                country = text.strip()
                service = state.split("|||", 1)[1]
                admin_states[user_id] = f"rate_input|||{service}|||{country}"
                bot.reply_to(message, f"✅ {service} | {country}\n\n💵 **Step 3/3: Enter rate (Taka per OTP)**\n\nExample: `0.5` or `1` or `2.5`", parse_mode='Markdown')
                return

            if state and state.startswith("rate_input|||"):
                try:
                    rate = float(text.strip())
                    if rate < 0:
                        raise ValueError
                except:
                    bot.reply_to(message, "❌ Invalid rate! Enter a number (e.g. 0.5 or 1).")
                    return
                parts = state.split("|||")
                service = parts[1]
                country = parts[2]
                key = f"{service}_{country}"
                otp_rates[key] = rate
                finish_admin_action(user_id)
                bot.reply_to(message, f"✅ **Rate Saved!**\n\nService: {service}\nCountry: {country}\n💵 Rate: {rate} Taka per OTP", reply_markup=get_admin_keyboard(), parse_mode='Markdown')
                return

            if state == "rate_delete_confirm":
                if not text.isdigit():
                    bot.reply_to(message, "❌ Enter a number (e.g. 1).")
                    return
                index = int(text) - 1
                keys = list(otp_rates.keys())
                if not (0 <= index < len(keys)):
                    bot.reply_to(message, "❌ Invalid number.")
                    return
                key = keys[index]
                removed_rate = otp_rates.pop(key)
                finish_admin_action(user_id)
                bot.reply_to(message, f"✅ Rate deleted: {key} → {removed_rate}", reply_markup=get_admin_keyboard())
                return

            if state == "add_service":
                if text in services:
                    bot.reply_to(message, f"❌ '{text}' already exists!", reply_markup=get_admin_keyboard())
                    return
                services.append(text)
                finish_admin_action(user_id)
                bot.reply_to(message, f"✅ Added: {text}", reply_markup=get_admin_keyboard())
                return

            if state == "add_country":
                if text in countries:
                    bot.reply_to(message, f"❌ '{text}' already exists!", reply_markup=get_admin_keyboard())
                    return
                countries.append(text)
                finish_admin_action(user_id)
                bot.reply_to(message, f"✅ Added: {text}", reply_markup=get_admin_keyboard())
                return

            if state == "delete_service":
                target = text.strip()
                service_to_delete = None
                if target.isdigit():
                    index = int(target) - 1
                    if 0 <= index < len(services):
                        service_to_delete = services[index]
                else:
                    if target in services:
                        service_to_delete = target
                if not service_to_delete:
                    bot.reply_to(message, "❌ Service not found!", reply_markup=get_admin_keyboard())
                    return
                has_numbers = any(k.startswith(f"{service_to_delete}_") and imported_numbers[k] for k in imported_numbers)
                if has_numbers:
                    bot.reply_to(message, f"❌ **'{service_to_delete}' has numbers attached!**", reply_markup=get_admin_keyboard())
                    finish_admin_action(user_id)
                    return
                services.remove(service_to_delete)
                finish_admin_action(user_id)
                bot.reply_to(message, f"✅ **'{service_to_delete}' deleted!**", reply_markup=get_admin_keyboard())
                return

            if state == "delete_country":
                target = text.strip()
                country_to_delete = None
                if target.isdigit():
                    index = int(target) - 1
                    if 0 <= index < len(countries):
                        country_to_delete = countries[index]
                else:
                    if target in countries:
                        country_to_delete = target
                if not country_to_delete:
                    bot.reply_to(message, "❌ Country not found!", reply_markup=get_admin_keyboard())
                    return
                has_numbers = any(k.endswith(f"_{country_to_delete}") and imported_numbers[k] for k in imported_numbers)
                if has_numbers:
                    bot.reply_to(message, f"❌ **'{country_to_delete}' has numbers attached!**", reply_markup=get_admin_keyboard())
                    finish_admin_action(user_id)
                    return
                countries.remove(country_to_delete)
                finish_admin_action(user_id)
                bot.reply_to(message, f"✅ **'{country_to_delete}' deleted!**", reply_markup=get_admin_keyboard())
                return

            if state == "delete_numbers_confirm":
                if text.strip().upper() == "YES":
                    imported_numbers.clear()
                    number_owners.clear()
                    number_meta.clear()
                    active_numbers.clear()
                    finish_admin_action(user_id)
                    bot.reply_to(message, "✅ **All numbers deleted!**", reply_markup=get_admin_keyboard())
                else:
                    finish_admin_action(user_id)
                    bot.reply_to(message, "❌ Cancelled.", reply_markup=get_admin_keyboard())
                return

            if state == "add_balance":
                try:
                    parts = text.split()
                    if len(parts) != 2:
                        raise ValueError
                    target_id = int(parts[0])
                    amount = float(parts[1])
                    if amount <= 0:
                        raise ValueError
                    ensure_user(target_id)
                    user_data[target_id]["balance"] += amount
                    finish_admin_action(user_id)
                    bot.reply_to(message, f"✅ Balance updated!\nUser: {target_id}\nNew: {user_data[target_id]['balance']:.2f} Taka", reply_markup=get_admin_keyboard())
                except:
                    bot.reply_to(message, "❌ Invalid format! Use: user_id amount", reply_markup=get_admin_keyboard())
                return

            if state == "broadcast":
                sent = 0
                for target_id in user_data:
                    try:
                        bot.send_message(target_id, f"📢 Admin:\n\n{text}")
                        sent += 1
                        time.sleep(0.05)
                    except:
                        pass
                finish_admin_action(user_id)
                bot.reply_to(message, f"✅ Sent to {sent} users", reply_markup=get_admin_keyboard())
                return

        # ===== USER BUTTONS =====
        if text == "☎️GET NUMBERS":
            if not services or not countries:
                bot.reply_to(message, "❌ No services available! Contact admin.", reply_markup=get_main_keyboard(user_id))
                return
            markup = InlineKeyboardMarkup(row_width=2)
            for service in services:
                markup.add(InlineKeyboardButton(text=service, callback_data=f"svc|{user_id}|{service}", style="success"))
            markup.add(InlineKeyboardButton(text="⬅️ Back", callback_data=f"back|{user_id}", style="success"))
            bot.reply_to(message, "📱 **Select a service:**", reply_markup=markup, parse_mode='Markdown')
            return

        elif text == "💰BALANCE":
            handle_wallet(message)
            return

        elif text == "💸WITHDRAW":
            handle_withdraw(message)
            return

        else:
            bot.reply_to(message, "❌ Use buttons below:", reply_markup=get_main_keyboard(user_id))
    except Exception as e:
        print(f"❌ Handler error: {e}")

# ================== INLINE CALLBACKS ==================
@bot.callback_query_handler(func=lambda call: not (call.data.startswith('approve_') or call.data.startswith('reject_')))
def handle_inline_callbacks(call):
    try:
        data = call.data
        user_id = call.from_user.id

        # SUPPORT CLOSE
        if data.startswith("support_close|"):
            user_id_from_data = int(data.split("|")[1])
            if user_id != user_id_from_data:
                bot.answer_callback_query(call.id, "❌ This button is not for you!", show_alert=True)
                return
            bot.answer_callback_query(call.id, "✅ Closed")
            try:
                bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
            except:
                pass
            return

        # WALLET CLOSE
        if data.startswith("wallet_close|"):
            user_id_from_data = int(data.split("|")[1])
            if user_id != user_id_from_data:
                bot.answer_callback_query(call.id, "❌ This button is not for you!", show_alert=True)
                return
            bot.answer_callback_query(call.id, "✅ Closed")
            try:
                bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
            except:
                pass
            return

        # WALLET WITHDRAW
        if data.startswith("wallet_withdraw|"):
            user_id_from_data = int(data.split("|")[1])
            if user_id != user_id_from_data:
                bot.answer_callback_query(call.id, "❌ This button is not for you!", show_alert=True)
                return
            balance = user_data.get(user_id, {}).get("balance", 0)
            if balance <= 0:
                bot.answer_callback_query(call.id, "❌ No balance to withdraw!", show_alert=True)
                return
            markup = InlineKeyboardMarkup(row_width=1)
            markup.add(InlineKeyboardButton(text="Bkash", callback_data=f"wd_method|{user_id}|Bkash", style="success"))
            markup.add(InlineKeyboardButton(text="❌ Cancel", callback_data=f"wd_cancel|{user_id}", style="success"))
            bot.answer_callback_query(call.id)
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  text=f"💳 **Select Payment Method**\n\n💰 Available Balance: {balance:.2f} BDT",
                                  reply_markup=markup, parse_mode='Markdown')
            return

        if data.startswith("wd_method|"):
            parts = data.split("|")
            user_id_from_data = int(parts[1])
            method = parts[2]
            if user_id != user_id_from_data:
                bot.answer_callback_query(call.id, "❌ This button is not for you!", show_alert=True)
                return
            balance = user_data.get(user_id, {}).get("balance", 0)
            if balance <= 0:
                bot.answer_callback_query(call.id, "❌ No balance!", show_alert=True)
                return
            admin_states[user_id] = f"wd_number|||{method}"
            bot.answer_callback_query(call.id, f"✅ {method} selected")
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  text=f"📱 **{method} Withdrawal**\n\nPlease type your **{method} number**:\n\n_Example: 01712345678_",
                                  reply_markup=None, parse_mode='Markdown')
            return

        if data.startswith("wd_cancel|"):
            user_id_from_data = int(data.split("|")[1])
            if user_id != user_id_from_data:
                bot.answer_callback_query(call.id, "❌ This button is not for you!", show_alert=True)
                return
            admin_states.pop(user_id, None)
            bot.answer_callback_query(call.id, "❌ Cancelled")
            try:
                bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
            except:
                pass
            return

        # BACK
        if data.startswith("back|"):
            bot.answer_callback_query(call.id)
            try:
                bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                      text="✅ Back to main menu", reply_markup=None)
            except:
                pass
            return

        # OTP CHANNEL (blue button)
        if data.startswith("otp_channel|"):
            bot.answer_callback_query(call.id, "📢 Opening OTP Channel...", show_alert=False)
            try:
                bot.send_message(
                    call.from_user.id,
                    "📢 **OTP Channel:**\n\n👉 Join here: " + OTP_CHANNEL_URL,
                    parse_mode='Markdown'
                )
            except:
                pass
            return

        # SERVICE SELECTION
        if data.startswith("svc|"):
            parts = data.split("|")
            user_id_from_data = int(parts[1])
            service = parts[2]
            if user_id != user_id_from_data:
                bot.answer_callback_query(call.id, "❌ This button is not for you!", show_alert=True)
                return
            user_data[user_id]["service"] = service
            markup = InlineKeyboardMarkup(row_width=2)
            for country in countries:
                markup.add(InlineKeyboardButton(text=country, callback_data=f"cntry|{user_id}|{service}|{country}", style="success"))
            markup.add(InlineKeyboardButton(text="⬅️ Back", callback_data=f"back|{user_id}", style="success"))
            bot.answer_callback_query(call.id, f"✅ {service} selected")
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  text=f"✅ Selected: **{service}**\n\n🌍 **Select a country:**",
                                  reply_markup=markup, parse_mode='Markdown')
            return

        # COUNTRY SELECTION → GENERATE NUMBERS
        if data.startswith("cntry|"):
            parts = data.split("|")
            user_id_from_data = int(parts[1])
            service = parts[2]
            country = parts[3]
            if user_id != user_id_from_data:
                bot.answer_callback_query(call.id, "❌ This button is not for you!", show_alert=True)
                return
            user_data[user_id]["country"] = country
            key = f"{service}_{country}"
            available = imported_numbers.get(key, [])

            if not available:
                bot.answer_callback_query(call.id, "❌ No numbers available!", show_alert=True)
                bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                      text=f"❌ **No numbers available!**\n\nService: {service}\nCountry: {country}",
                                      reply_markup=None, parse_mode='Markdown')
                return

            numbers_to_give = []
            for _ in range(min(4, len(available))):
                numbers_to_give.append(available.pop(0))

            # Save ownership + expiry
            now = time.time()
            with data_lock:
                for num in numbers_to_give:
                    key_num = str(num).strip()
                    number_owners[key_num] = user_id
                    number_meta[key_num] = {"service": service, "country": country}
                    active_numbers[key_num] = {"expires_at": now + NUMBER_EXPIRY_SECONDS}

            print(f"[OWNER] user {user_id} → {numbers_to_give} ({service}/{country})")

            user_data[user_id]["numbers"] = numbers_to_give
            user_data[user_id]["number"] = numbers_to_give[0]

            send_number_screen(call, user_id, service, country, numbers_to_give)
            return

        # CHANGE NUMBERS
        if data.startswith("change|"):
            user_id_from_data = int(data.split("|")[1])
            if user_id != user_id_from_data:
                bot.answer_callback_query(call.id, "❌ This button is not for you!", show_alert=True)
                return
            user = user_data.get(user_id)
            if not user or not user.get("numbers"):
                bot.answer_callback_query(call.id, "❌ You have no numbers!", show_alert=True)
                return
            service = user.get("service")
            country = user.get("country")
            if not service or not country:
                bot.answer_callback_query(call.id, "❌ Service or country missing!", show_alert=True)
                return
            key = f"{service}_{country}"
            available = imported_numbers.get(key, [])
            if not available:
                bot.answer_callback_query(call.id, "❌ No more numbers left!", show_alert=True)
                return
            new_numbers = []
            for _ in range(min(4, len(available))):
                new_numbers.append(available.pop(0))

            now = time.time()
            with data_lock:
                for num in new_numbers:
                    key_num = str(num).strip()
                    number_owners[key_num] = user_id
                    number_meta[key_num] = {"service": service, "country": country}
                    active_numbers[key_num] = {"expires_at": now + NUMBER_EXPIRY_SECONDS}

            print(f"[OWNER] user {user_id} → {new_numbers} ({service}/{country})")

            user["numbers"] = new_numbers
            user["number"] = new_numbers[0]

            send_number_screen(call, user_id, service, country, new_numbers)
            return

        bot.answer_callback_query(call.id, "❌ Invalid request!")

    except Exception as e:
        print(f"❌ Callback error: {e}")
        try:
            bot.answer_callback_query(call.id, "❌ Something went wrong!", show_alert=True)
        except:
            pass

# ================== NUMBER SCREEN (Screenshot Layout) ==================
def send_number_screen(call, user_id, service, country, numbers):
    """
    Render the number-screen matching the user's screenshot:
    - Header line: ✅ [Service] ☎️ - [Country]
    - Waiting message with 25m expiry
    - 4 green number buttons
    - Blue "Change" + Blue "OTP" buttons side by side
    - Red "Back" button
    """
    header = f"✅ {service} ☎️ - {country}"

    # Build inline keyboard
    markup = InlineKeyboardMarkup(row_width=1)

    # Green number buttons (each on its own row)
    for num in numbers:
        markup.add(InlineKeyboardButton(
            text=f"📋 {number_with_plus(num)}",
            copy_text=CopyTextButton(text=str(num)),
            style="success"
        ))

    # Row 2: Change (blue) + OTP (blue)
    # Note: pyTelegramBotAPI doesn't have a native 'primary' style. We'll use
    # style="primary" which renders blue on supported Telegram clients.
    change_btn = InlineKeyboardButton(
        text="🔄 Change",
        callback_data=f"change|{user_id}",
        style="primary"
    )
    otp_btn = InlineKeyboardButton(
        text="👀 OTP",
        url=OTP_CHANNEL_URL
    )
    try:
        otp_btn.style = "primary"
    except Exception:
        pass
    markup.row(change_btn, otp_btn)

    # Row 3: Back (red) - style="danger" → red on supported clients
    markup.add(InlineKeyboardButton(
        text="⬅️ Back",
        callback_data=f"back|{user_id}",
        style="danger"
    ))

    text = (
        f"{header}\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏳ **Waiting for OTP...** (Auto-expiry: 25m)"
    )

    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=text,
            reply_markup=markup,
            parse_mode='Markdown'
        )
    except Exception:
        try:
            bot.send_message(
                chat_id=call.message.chat.id,
                text=text,
                reply_markup=markup,
                parse_mode='Markdown'
            )
        except Exception as e:
            print(f"❌ send_number_screen error: {e}")

# ================== DOCUMENT HANDLER ==================
@bot.message_handler(content_types=['document'])
def handle_document(message):
    try:
        user_id = message.from_user.id
        if not is_admin(user_id):
            bot.reply_to(message, "❌ Not authorized!")
            return
        state = admin_states.get(user_id)
        if not state or not state.startswith("import_file|||"):
            bot.reply_to(message, "❌ Use 'Import Numbers' first!")
            return
        file_info = message.document
        if not file_info.file_name.lower().endswith('.txt'):
            bot.reply_to(message, "❌ Please send a .txt file!")
            return
        parts = state.split("|||")
        if len(parts) < 3:
            bot.reply_to(message, "❌ Invalid state. Restart import.", reply_markup=get_admin_keyboard())
            finish_admin_action(user_id)
            return
        service = parts[1]
        country = parts[2]
        try:
            file_path = bot.get_file(file_info.file_id).file_path
            raw_bytes = bot.download_file(file_path)
        except Exception as e:
            print(f"DOWNLOAD ERROR: {e}")
            bot.reply_to(message, "❌ Failed to download the file!", reply_markup=get_admin_keyboard())
            return
        numbers = parse_numbers_from_bytes(raw_bytes)
        if not numbers:
            bot.reply_to(message, "❌ **Failed to read the file!**\n\nMake sure it is a valid .txt file.",
                         reply_markup=get_admin_keyboard(), parse_mode='Markdown')
            return
        key = f"{service}_{country}"
        if key not in imported_numbers:
            imported_numbers[key] = []
        existing = set(imported_numbers[key])
        new_numbers = [n for n in numbers if n not in existing]
        imported_numbers[key].extend(new_numbers)
        finish_admin_action(user_id)
        rate_note = f"\n💵 Rate: {otp_rates[key]} Taka per OTP" if key in otp_rates else "\n⚠️ No rate set. Use 'Add Rate'."
        bot.reply_to(message,
                     f"✅ **Imported!**\n\nService: {service}\nCountry: {country}\n✅ New: {len(new_numbers)}\n📊 Total: {len(imported_numbers[key])}{rate_note}",
                     reply_markup=get_admin_keyboard(), parse_mode='Markdown')
    except Exception as e:
        print(f"❌ Document error: {e}")
        bot.reply_to(message, f"❌ Error: {e}", reply_markup=get_admin_keyboard())

# ================== VIEW IMPORTED ==================
def view_imported_numbers(message):
    if not imported_numbers:
        bot.reply_to(message, "❌ No numbers!", reply_markup=get_admin_keyboard())
        return
    response = "📁 **Imported Numbers**\n━━━━━━━━━━━━━━━━\n\n"
    total = 0
    for key, numbers in imported_numbers.items():
        service, country = key.split("_", 1) if "_" in key else (key, "?")
        count = len(numbers)
        total += count
        rate = otp_rates.get(key, "—")
        response += f"{service} | {country}: {count} (Rate: {rate})\n"
    response += f"\n━━━━━━━━━━━━━━━━\n📊 Total: {total}\n💵 Rates: {len(otp_rates)}\n🎯 Owners: {len(number_owners)}\n⏱️ Active: {len(active_numbers)}"
    bot.reply_to(message, response, reply_markup=get_admin_keyboard(), parse_mode='Markdown')

# ================== PENDING WITHDRAWALS ==================
def handle_pending_withdrawals(message):
    if not pending_withdrawals:
        bot.reply_to(message, "✅ No pending!", reply_markup=get_admin_keyboard())
        return
    for i, withdrawal in enumerate(pending_withdrawals):
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{i}", style="success"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{i}", style="success")
        )
        bot.send_message(
            message.chat.id,
            f"⏳ **Withdrawal #{i+1}**\n\n👤 User: `{withdrawal['user_id']}`\n💰 Amount: {withdrawal['amount']:.2f} Taka\nMethod: {withdrawal.get('method', 'N/A')}\n📞 Number: `{withdrawal.get('number', 'N/A')}`\nService: {withdrawal.get('service', 'N/A')} | {withdrawal.get('country', 'N/A')}",
            reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data.startswith('approve_') or call.data.startswith('reject_'))
def handle_withdrawal_action(call):
    try:
        if not is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "❌ Not authorized!")
            return
        action, index = call.data.split('_')
        index = int(index)
        if index >= len(pending_withdrawals):
            bot.answer_callback_query(call.id, "❌ Not found!")
            return
        withdrawal = pending_withdrawals[index]
        target_user = withdrawal['user_id']
        amount = withdrawal['amount']
        method = withdrawal.get('method', 'N/A')
        number = withdrawal.get('number', 'N/A')
        if action == 'approve':
            pending_withdrawals.pop(index)
            bot.send_message(target_user, f"✅ **Withdrawal Approved!**\n\n💰 Amount: {amount:.2f} BDT\nMethod: {method}\n📞 Number: `{number}`\n\nYour payment has been sent!", parse_mode='Markdown')
            bot.answer_callback_query(call.id, "✅ Approved!")
            bot.edit_message_text(f"✅ **Approved!**\nUser: `{target_user}`\nAmount: {amount:.2f} Taka\nMethod: {method}\nNumber: `{number}`",
                                  call.message.chat.id, call.message.message_id, parse_mode='Markdown')
        else:
            pending_withdrawals.pop(index)
            ensure_user(target_user)
            user_data[target_user]["balance"] += amount
            bot.send_message(target_user, f"❌ **Withdrawal Rejected!**\n\n💰 Amount: {amount:.2f} BDT\nMethod: {method}\n\nYour balance has been refunded.", parse_mode='Markdown')
            bot.answer_callback_query(call.id, "❌ Rejected!")
            bot.edit_message_text(f"❌ **Rejected!**\nUser: `{target_user}`\nAmount: {amount:.2f} Taka\nMethod: {method}\nNumber: `{number}`",
                                  call.message.chat.id, call.message.message_id, parse_mode='Markdown')
    except Exception as e:
        print(f"❌ Withdrawal error: {e}")

# ================== USER FUNCTIONS ==================
def handle_wallet(message):
    user_id = message.from_user.id
    ensure_user(user_id)
    balance = user_data[user_id]["balance"]
    today = time.strftime("%Y-%m-%d")
    stats = user_otp_stats[user_id]
    if stats["last_date"] != today:
        stats["today"] = 0
        stats["last_date"] = today
    name = message.from_user.first_name or "User"
    if message.from_user.last_name:
        name += f" {message.from_user.last_name}"
    text = (
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "       👤 MY PROFILE 👤\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 Name: {name}\n"
        f"🆔 User ID: {user_id}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💲 Balance: {balance:.2f} BDT\n"
        f"💬 Total OTPs: {stats['total']}\n"
        f"📊 Today OTPs: {stats['today']}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(InlineKeyboardButton("💵 WITHDRAW", callback_data=f"wallet_withdraw|{user_id}", style="success"))
    markup.add(InlineKeyboardButton("❌ Close", callback_data=f"wallet_close|{user_id}", style="success"))
    bot.reply_to(message, text, reply_markup=markup)

def handle_withdraw(message):
    user_id = message.from_user.id
    ensure_user(user_id)
    balance = user_data[user_id]["balance"]
    if balance <= 0:
        bot.reply_to(message, "❌ No balance to withdraw!", reply_markup=get_main_keyboard(user_id))
        return
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(InlineKeyboardButton("Bkash", callback_data=f"wd_method|{user_id}|Bkash", style="success"))
    markup.add(InlineKeyboardButton("❌ Cancel", callback_data=f"wd_cancel|{user_id}", style="success"))
    bot.reply_to(message, f"💳 **Select Payment Method**\n\n💰 Available Balance: {balance:.2f} BDT",
                 reply_markup=markup, parse_mode='Markdown')

# ================== OTP FETCHER ==================
def get_country_info(phone_number):
    try:
        if not phone_number:
            return "Unknown"
        phone_number = str(phone_number).strip()
        if not phone_number.startswith('+'):
            phone_number = '+' + phone_number
        parsed = phonenumbers.parse(phone_number, None)
        return geocoder.description_for_number(parsed, "en") or "Unknown"
    except Exception:
        return "Unknown"

def extract_otp(message_text):
    if not message_text:
        return "N/A"
    match = re.search(r'\b\d{4,8}\b', str(message_text))
    return match.group(0) if match else "N/A"

def find_owner_by_last4(phone_number, owners_dict):
    if not phone_number:
        return None, None
    clean = re.sub(r'\D', '', str(phone_number))
    if len(clean) < 4:
        return None, None
    last4 = clean[-4:]
    for stored_num, uid in owners_dict.items():
        stored_clean = re.sub(r'\D', '', str(stored_num))
        if len(stored_clean) >= 4 and stored_clean[-4:] == last4:
            return stored_num, uid
    return None, None

def send_to_otp_group(num, cli, message_content, dt=""):
    telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    country = get_country_info(num)
    service = cli if cli else "Unknown"
    otp_code = extract_otp(message_content)
    masked_num = mask_number(num)

    formatted_text = (
        "🔑 *OTP RECEIVED!*\n"
        "\n"
        f"🔢 *Number:* `{masked_num}`\n"
        "\n"
        f"🌍 *Country:* {country}\n"
        "\n"
        f"👤 *Service:* {service}\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )

    inline_keyboard = {
        "inline_keyboard": [[
            {"text": f"🔑 {otp_code}", "copy_text": {"text": otp_code}, "style": "success"},
            {"text": "📋 FULL SMS", "copy_text": {"text": message_content}, "style": "success"}
        ]]
    }

    payload = {
        "chat_id": TELEGRAM_OTP_CHAT_ID,
        "text": formatted_text,
        "parse_mode": "Markdown",
        "reply_markup": inline_keyboard
    }

    try:
        r = requests.post(telegram_url, json=payload, timeout=10)
        if r.status_code == 200:
            print(f"[{num}] ✅ Sent to OTP group (masked: {masked_num}).")
        else:
            print(f"[{num}] Group send failed: {r.status_code} - {r.text[:200]}")
    except Exception as e:
        print(f"[{num}] Group error: {e}")

def send_to_owner(owner_id, num, cli, message_content, dt="", rate=None):
    telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    country_name = get_country_info(num)
    service_name = cli if cli else "Unknown"
    otp_code = extract_otp(message_content)
    rate_line = f"💵 *Earned:* +{rate} Taka\n" if rate is not None else ""

    formatted_text = (
        "🎯 *YOUR OTP ARRIVED!*\n"
        "\n"
        f"🔢 *Number:* `{num}`\n"
        "\n"
        f"🌍 *Country:* {country_name}\n"
        "\n"
        f"👤 *Service:* {service_name}\n"
        "\n"
        f"🔑 *OTP:* `{otp_code}`\n"
        "\n"
        f"{rate_line}"
        "━━━━━━━━━━━━━━━━━━━━━"
    )

    inline_keyboard = {
        "inline_keyboard": [[
            {"text": f"🔑 {otp_code}", "copy_text": {"text": otp_code}, "style": "success"},
            {"text": "📋 FULL SMS", "copy_text": {"text": message_content}, "style": "success"}
        ]]
    }

    payload = {
        "chat_id": owner_id,
        "text": formatted_text,
        "parse_mode": "Markdown",
        "reply_markup": inline_keyboard
    }

    try:
        r = requests.post(telegram_url, json=payload, timeout=10)
        if r.status_code == 200:
            print(f"[{num}] 🎯 Forwarded to owner {owner_id}")
            return True
        else:
            print(f"[{num}] Owner failed: {r.status_code} - {r.text[:200]}")
            return False
    except Exception as e:
        print(f"[{num}] Owner error: {e}")
        return False

def otp_polling_loop():
    global first_sync_done
    print("🎧 OTP polling thread started.")
    while True:
        try:
            params = {"token": API_TOKEN, "records": RECORDS_TO_FETCH}
            response = requests.get(API_BASE_URL, params=params, timeout=15)

            if response.status_code != 200:
                print(f"API ERROR: {response.status_code}")
                time.sleep(POLL_INTERVAL)
                continue

            try:
                res_data = response.json()
            except Exception:
                print("JSON PARSE ERROR:", response.text[:200])
                time.sleep(POLL_INTERVAL)
                continue

            messages = []
            if isinstance(res_data, dict):
                if res_data.get("status") == "success":
                    messages = res_data.get("data", [])
                elif "data" in res_data:
                    messages = res_data.get("data", [])
                elif "messages" in res_data:
                    messages = res_data.get("messages", [])
                elif "result" in res_data:
                    messages = res_data.get("result", [])
                else:
                    for v in res_data.values():
                        if isinstance(v, list):
                            messages = v
                            break
            elif isinstance(res_data, list):
                messages = res_data

            if not first_sync_done:
                count = 0
                for msg in messages:
                    if not isinstance(msg, dict):
                        continue
                    dt = msg.get("dt") or msg.get("date") or msg.get("datetime") or ""
                    num = msg.get("num") or msg.get("number") or msg.get("phone") or ""
                    cli = msg.get("cli") or msg.get("sender") or msg.get("senderid") or ""
                    message_content = (msg.get("message") or msg.get("msg") or msg.get("text") or msg.get("sms") or "")
                    unique_id = f"{dt}_{num}_{cli}_{message_content}"
                    processed_messages.add(unique_id)
                    count += 1
                first_sync_done = True
                print(f"🔄 First sync done. Skipped {count} old messages. Now watching for NEW OTPs...")
                time.sleep(POLL_INTERVAL)
                continue

            if messages:
                for msg in reversed(messages):
                    if not isinstance(msg, dict):
                        continue
                    dt = msg.get("dt") or msg.get("date") or msg.get("datetime") or ""
                    num = msg.get("num") or msg.get("number") or msg.get("phone") or ""
                    cli = msg.get("cli") or msg.get("sender") or msg.get("senderid") or ""
                    message_content = (msg.get("message") or msg.get("msg") or msg.get("text") or msg.get("sms") or "")
                    if not num and not message_content:
                        continue
                    unique_id = f"{dt}_{num}_{cli}_{message_content}"
                    if unique_id in processed_messages:
                        continue

                    print(f"🆕 NEW OTP FOUND: {num}")
                    send_to_otp_group(num, cli, message_content, dt)

                    with data_lock:
                        stored_num, owner_id = find_owner_by_last4(num, number_owners)
                        meta = number_meta.get(stored_num, {}) if stored_num else {}

                    if owner_id:
                        svc = meta.get("service")
                        ctry = meta.get("country")
                        rate = otp_rates.get(f"{svc}_{ctry}") if svc and ctry else None

                        with data_lock:
                            ensure_user(owner_id)
                            if rate is not None:
                                user_data[owner_id]["balance"] += rate
                            today = time.strftime("%Y-%m-%d")
                            if owner_id not in user_otp_stats:
                                user_otp_stats[owner_id] = {"total": 0, "today": 0, "last_date": today}
                            stats = user_otp_stats[owner_id]
                            if stats["last_date"] != today:
                                stats["today"] = 0
                                stats["last_date"] = today
                            stats["total"] += 1
                            stats["today"] += 1

                            # mark number as used (remove from active)
                            if stored_num:
                                active_numbers.pop(stored_num, None)

                        if rate is not None:
                            print(f"[{num}] 💰 +{rate} Taka → user {owner_id} (OTP #{stats['total']})")
                        else:
                            print(f"[{num}] ⚠️ No rate set for {svc}/{ctry} — OTP counted, no balance added")

                        send_to_owner(owner_id, num, cli, message_content, dt, rate=rate)
                    else:
                        print(f"[{num}] No owner (last 4 digits didn't match).")

                    processed_messages.add(unique_id)
                    if len(processed_messages) > 2000:
                        processed_messages.pop()

                    time.sleep(1)

        except Exception as e:
            print(f"OTP LOOP ERROR: {e}")

        time.sleep(POLL_INTERVAL)

# ================== RUN ==================
if __name__ == "__main__":
    print("=" * 60)
    print("🤖 BOT + OTP FORWARDER + RATE + WITHDRAW + SUPPORT")
    print("=" * 60)
    print(f"✅ Services: {len(services)}")
    print(f"✅ Countries: {len(countries)}")
    print(f"💵 Rates set: {len(otp_rates)}")
    print(f"💬 Support: @{SUPPORT_USERNAME}")
    print(f"📢 OTP Channel: {OTP_CHANNEL_URL}")
    print("🚀 Bot starting...")
    print("=" * 60)

    # Start OTP polling thread
    otp_thread = threading.Thread(target=otp_polling_loop, daemon=True)
    otp_thread.start()

    # Start expiry thread (25-min auto-expire)
    expiry_thread = threading.Thread(target=expire_loop, daemon=True)
    expiry_thread.start()

    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except Exception as e:
            print(f"❌ Bot error: {e}")
            time.sleep(5)