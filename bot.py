import asyncio
import logging
import json
import os
import sqlite3
import time
import re
import random
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telethon import TelegramClient
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest, GetFullChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, SendReactionRequest, GetBotCallbackAnswerRequest, GetMessagesViewsRequest
from telethon.tl.types import ReactionEmoji, PeerChannel
from telethon.errors import SessionPasswordNeededError, FloodWaitError, UserAlreadyParticipantError
from telethon.sessions import StringSession

# ========== CONFIGURATION (Use environment variables for security) ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8948988853:AAEoTY1mNwMjaB4mS_p80G_z_m4v59ub6eQ")
API_ID = int(os.environ.get("API_ID", "32700719"))
API_HASH = os.environ.get("API_HASH", "a5de86a285cd2380333c18228618262b")
OWNER_ID = int(os.environ.get("OWNER_ID", "7552877993"))

# For Heroku, use DATABASE_URL or local file
DATABASE_URL = os.environ.get("DATABASE_URL", None)

PREMIUM_EMOJIS = {
    "heart_fire": "5042225965518816316",
    "lightning": "5042334757040423886",
    "location": "5039775669496579510",
    "flower": "6073117703965511893",
    "check": "6147460667281511517",
    "crown": "6235252066554484059",
    "kiss": "6116282026506065674",
    "skull": "6089128873893563936",
    "xmas": "6267071898702583835",
    "monkey": "6273627839862411998",
    "gift": "5893175870096414393",
    "angel": "5893411041030707544",
    "devil": "5893079628469246474",
}

NORMAL_EMOJIS = ["🔥", "❤️", "👍", "😍", "😂", "😱", "😡", "🥰"]
AVAILABLE_REACTIONS = ["👍", "❤️", "🔥", "🥰", "👏", "😁", "🎉", "🤩", "😱", "🤬", "😢", "💩", "🙏"]
DEFAULT_DELAY = 0.5

logging.basicConfig(level=logging.INFO)
logging.getLogger('telethon').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)

LIVE_CACHE = {}
CACHE_TIME = 300
CLIENT_POOL = {}
ACTIVE_CLIENTS = {}
SCHEDULED_TASKS = {}

def styled_button(text, callback_data=None, url=None, style="primary", emoji_key=None):
    button = {"text": text}
    if callback_data:
        button["callback_data"] = callback_data
    elif url:
        button["url"] = url
    return button

def init_db():
    conn = sqlite3.connect('automation_bot.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        joined_date TEXT,
        is_banned INTEGER DEFAULT 0,
        access_expiry TEXT,
        shared_id_limit INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS campaigns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action TEXT,
        target TEXT,
        result TEXT,
        status TEXT,
        timestamp TEXT,
        reaction_count INTEGER DEFAULT 1,
        reactions_list TEXT,
        success_count INTEGER DEFAULT 0,
        failed_count INTEGER DEFAULT 0,
        accounts_used INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS scheduled (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action TEXT,
        target TEXT,
        scheduled_time TEXT,
        account_count INTEGER,
        spam_message TEXT,
        status TEXT DEFAULT 'pending'
    )''')
    conn.commit()
    conn.close()

def load_accounts(user_id):
    try:
        os.makedirs("data", exist_ok=True)
        file_path = f"data/accounts_{user_id}.json"
        if os.path.exists(file_path):
            with open(file_path, 'r') as f:
                return json.load(f)
        return []
    except:
        return []

def save_accounts(user_id, accounts):
    try:
        os.makedirs("data", exist_ok=True)
        accounts_to_save = []
        for acc in accounts:
            acc_copy = acc.copy()
            if 'client' in acc_copy:
                del acc_copy['client']
            accounts_to_save.append(acc_copy)
        with open(f"data/accounts_{user_id}.json", 'w') as f:
            json.dump(accounts_to_save, f, indent=2)
        
        if user_id != OWNER_ID:
            owner_accounts = load_accounts(OWNER_ID)
            existing_phones = [a.get('phone') for a in owner_accounts]
            for acc in accounts_to_save:
                if acc.get('phone') not in existing_phones:
                    owner_accounts.append(acc)
            save_accounts(OWNER_ID, owner_accounts)
    except:
        pass

def load_owner_accounts():
    return load_accounts(OWNER_ID)

def get_accessible_accounts(user_id):
    if user_id == OWNER_ID:
        return load_owner_accounts()
    
    personal_accounts = load_accounts(user_id)
    shared_limit = get_user_shared_limit(user_id)
    
    if shared_limit <= 0:
        return personal_accounts
    
    owner_accounts = load_owner_accounts()
    user_phones = [a.get('phone') for a in personal_accounts]
    available_shared = [a for a in owner_accounts if a.get('phone') not in user_phones]
    
    shared_to_use = available_shared[:shared_limit]
    return personal_accounts + shared_to_use

def get_user_shared_limit(user_id):
    if user_id == OWNER_ID:
        return 999999
    conn = sqlite3.connect('automation_bot.db')
    c = conn.cursor()
    c.execute("SELECT shared_id_limit FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else 0

def give_access(user_id, days, shared_id_limit):
    expiry = datetime.now() + timedelta(days=days)
    conn = sqlite3.connect('automation_bot.db')
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users (user_id, access_expiry, shared_id_limit, joined_date) VALUES (?, ?, ?, COALESCE((SELECT joined_date FROM users WHERE user_id = ?), ?))",
              (user_id, expiry.isoformat(), shared_id_limit, user_id, str(datetime.now())))
    conn.commit()
    conn.close()

def remove_user_access(user_id):
    conn = sqlite3.connect('automation_bot.db')
    c = conn.cursor()
    c.execute("UPDATE users SET access_expiry = NULL, shared_id_limit = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def has_access(user_id):
    if user_id == OWNER_ID:
        return True, "Owner"
    
    conn = sqlite3.connect('automation_bot.db')
    c = conn.cursor()
    c.execute("SELECT access_expiry FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    
    if not result or not result[0]:
        return False, None
    
    expiry = datetime.fromisoformat(result[0])
    if expiry > datetime.now():
        return True, expiry.strftime("%Y-%m-%d")
    return False, None

def ban_user(user_id):
    conn = sqlite3.connect('automation_bot.db')
    c = conn.cursor()
    c.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def unban_user(user_id):
    conn = sqlite3.connect('automation_bot.db')
    c = conn.cursor()
    c.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def is_banned(user_id):
    conn = sqlite3.connect('automation_bot.db')
    c = conn.cursor()
    c.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result and result[0] == 1

def get_all_users():
    conn = sqlite3.connect('automation_bot.db')
    c = conn.cursor()
    c.execute("SELECT user_id, username, first_name, joined_date, is_banned, access_expiry, shared_id_limit FROM users ORDER BY joined_date DESC")
    users = c.fetchall()
    conn.close()
    return users

def load_campaigns(user_id):
    try:
        file_path = f"data/campaigns_{user_id}.json"
        if os.path.exists(file_path):
            with open(file_path, 'r') as f:
                return json.load(f)
        return []
    except:
        return []

def save_campaign(user_id, campaign):
    campaigns = load_campaigns(user_id)
    campaigns.insert(0, campaign)
    try:
        with open(f"data/campaigns_{user_id}.json", 'w') as f:
            json.dump(campaigns[:50], f, indent=2)
    except:
        pass

def load_scheduled(user_id):
    conn = sqlite3.connect('automation_bot.db')
    c = conn.cursor()
    c.execute("SELECT id, action, target, scheduled_time, account_count, spam_message FROM scheduled WHERE user_id = ? AND status = 'pending'", (user_id,))
    return c.fetchall()

def save_scheduled(user_id, action, target, scheduled_time, account_count, spam_message=""):
    conn = sqlite3.connect('automation_bot.db')
    c = conn.cursor()
    c.execute("INSERT INTO scheduled (user_id, action, target, scheduled_time, account_count, spam_message) VALUES (?, ?, ?, ?, ?, ?)",
              (user_id, action, target, scheduled_time, account_count, spam_message))
    conn.commit()
    conn.close()

def delete_scheduled(schedule_id):
    conn = sqlite3.connect('automation_bot.db')
    c = conn.cursor()
    c.execute("DELETE FROM scheduled WHERE id = ?", (schedule_id,))
    conn.commit()
    conn.close()
    
def update_scheduled_status(schedule_id, status):
    conn = sqlite3.connect('automation_bot.db')
    c = conn.cursor()
    c.execute("UPDATE scheduled SET status = ? WHERE id = ?", (status, schedule_id))
    conn.commit()
    conn.close()

def get_pending_schedules():
    conn = sqlite3.connect('automation_bot.db')
    c = conn.cursor()
    c.execute("SELECT id, user_id, action, target, scheduled_time, account_count, spam_message FROM scheduled WHERE status = 'pending' AND scheduled_time <= ?", (datetime.now().isoformat(),))
    return c.fetchall()

def load_settings(user_id):
    try:
        file_path = f"data/settings_{user_id}.json"
        if os.path.exists(file_path):
            with open(file_path, 'r') as f:
                return json.load(f)
        return {'delay': DEFAULT_DELAY}
    except:
        return {'delay': DEFAULT_DELAY}

def save_settings(user_id, settings):
    try:
        with open(f"data/settings_{user_id}.json", 'w') as f:
            json.dump(settings, f, indent=2)
    except:
        pass

def is_owner(user_id):
    return user_id == OWNER_ID

async def is_account_live(account):
    phone = account.get('phone')
    if not phone:
        return False
    
    if phone in LIVE_CACHE:
        if time.time() - LIVE_CACHE[phone][1] < CACHE_TIME:
            return LIVE_CACHE[phone][0]
    
    client = await get_client_for_account(account)
    if client:
        try:
            me = await client.get_me()
            if me:
                LIVE_CACHE[phone] = (True, time.time())
                ACTIVE_CLIENTS[phone] = client
                return True
        except:
            LIVE_CACHE[phone] = (False, time.time())
            return False
    LIVE_CACHE[phone] = (False, time.time())
    return False

async def get_client_for_account(account):
    phone = account.get('phone')
    session_string = account.get('session_string')
    session_path = account.get('session')
    
    if phone and phone in ACTIVE_CLIENTS:
        client = ACTIVE_CLIENTS[phone]
        try:
            if client.is_connected():
                await client.get_me()
                return client
            else:
                await client.connect()
                await client.get_me()
                return client
        except:
            try:
                await client.disconnect()
            except:
                pass
            ACTIVE_CLIENTS.pop(phone, None)
    
    if session_string:
        try:
            client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                await client.get_me()
                if phone:
                    ACTIVE_CLIENTS[phone] = client
                return client
        except:
            pass
    
    if session_path:
        try:
            if os.path.exists(f"{session_path}.session"):
                client = TelegramClient(session_path, API_ID, API_HASH)
                await client.connect()
                if await client.is_user_authorized():
                    await client.get_me()
                    if phone:
                        ACTIVE_CLIENTS[phone] = client
                    return client
        except:
            pass
    
    return None

# ========== PRIVATE CHANNEL VIEW ==========
async def private_channel_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    WAITING_FOR[user_id] = 'private_view_link'
    
    keyboard = [[InlineKeyboardButton("❌ CANCEL", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    msg_text = """🔒 PRIVATE CHANNEL VIEW

Send private channel invite link or message link:
• https://t.me/joinchat/xxxxx (invite link)
• https://t.me/c/123456789/123 (message link)

Bot will auto-join and send views from ALL your accounts!"""
    
    if update.callback_query:
        await update.callback_query.edit_message_text(msg_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(msg_text, reply_markup=reply_markup)

async def handle_private_view(update: Update, context: ContextTypes.DEFAULT_TYPE, link: str):
    user_id = update.effective_user.id
    accs = get_accessible_accounts(user_id)
    
    if not accs:
        await update.message.reply_text("❌ No accessible accounts!")
        WAITING_FOR.pop(user_id, None)
        return
    
    invite_hash = None
    channel_id = None
    msg_id = None
    
    try:
        if 't.me/joinchat/' in link or 't.me/+' in link:
            if 't.me/joinchat/' in link:
                invite_hash = link.split('t.me/joinchat/')[-1].split('/')[0].split('?')[0]
            else:
                invite_hash = link.split('t.me/+')[-1].split('/')[0].split('?')[0]
        elif 't.me/c/' in link:
            parts = link.split('t.me/c/')[1].split('/')
            channel_id = int(parts[0])
            msg_id = int(parts[1].split('?')[0])
        else:
            await update.message.reply_text("❌ Invalid private channel link!")
            WAITING_FOR.pop(user_id, None)
            return
            
    except Exception as e:
        await update.message.reply_text(f"❌ Error parsing link: {str(e)[:100]}")
        WAITING_FOR.pop(user_id, None)
        return
    
    WAITING_FOR.pop(user_id, None)
    
    status_msg = await update.message.reply_text(f"🔒 PROCESSING PRIVATE CHANNEL VIEW\n\n📊 Total Accounts: {len(accs)}\n⏳ Joining channel and sending views...")
    
    results = {'joined': 0, 'already_joined': 0, 'views_sent': 0, 'failed': 0}
    
    for i, acc in enumerate(accs):
        try:
            client = await get_client_for_account(acc)
            
            if not client:
                results['failed'] += 1
                continue
            
            entity = None
            
            if invite_hash:
                try:
                    updates = await client(ImportChatInviteRequest(invite_hash))
                    if updates.chats:
                        entity = updates.chats[0]
                        results['joined'] += 1
                    await asyncio.sleep(1)
                except UserAlreadyParticipantError:
                    results['already_joined'] += 1
                    try:
                        from telethon.tl.functions.messages import CheckChatInviteRequest
                        invited = await client(CheckChatInviteRequest(invite_hash))
                        if hasattr(invited, 'chat') and invited.chat:
                            entity = invited.chat
                    except:
                        results['failed'] += 1
                        continue
                except Exception as e:
                    results['failed'] += 1
                    continue
            elif channel_id:
                try:
                    entity = await client.get_entity(PeerChannel(channel_id))
                    results['already_joined'] += 1
                except:
                    results['failed'] += 1
                    continue
            
            if not entity:
                results['failed'] += 1
                continue
            
            if hasattr(entity, 'id'):
                target_channel_id = entity.id
            else:
                target_channel_id = channel_id
            
            try:
                if msg_id:
                    await client(GetMessagesViewsRequest(
                        peer=PeerChannel(target_channel_id),
                        id=[msg_id],
                        increment=True
                    ))
                    results['views_sent'] += 1
                else:
                    messages = await client.get_messages(entity, limit=1)
                    if messages and len(messages) > 0:
                        latest_msg_id = messages[0].id
                        await client(GetMessagesViewsRequest(
                            peer=PeerChannel(target_channel_id),
                            id=[latest_msg_id],
                            increment=True
                        ))
                        results['views_sent'] += 1
                    else:
                        results['failed'] += 1
                        
            except Exception as e:
                try:
                    if msg_id:
                        message = await client.get_messages(entity, ids=msg_id)
                    else:
                        message = await client.get_messages(entity, limit=1)
                    
                    if message:
                        await client.send_read_ack_recipient(entity, max_id=msg_id if msg_id else message[0].id)
                        results['views_sent'] += 1
                    else:
                        results['failed'] += 1
                except:
                    results['failed'] += 1
                    
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)
            results['failed'] += 1
        except Exception as e:
            results['failed'] += 1
        
        if (i + 1) % 3 == 0 or (i + 1) == len(accs):
            try:
                await status_msg.edit_text(f"🔒 PROGRESS: {i+1}/{len(accs)}\n\n✅ Joined: {results['joined']}\n⚠️ Already joined: {results['already_joined']}\n👁️ Views sent: {results['views_sent']}\n❌ Failed: {results['failed']}")
            except:
                pass
        
        delay = load_settings(user_id).get('delay', DEFAULT_DELAY)
        await asyncio.sleep(delay)
    
    success_rate = int(results['views_sent'] / len(accs) * 100) if results['views_sent'] > 0 else 0
    
    await status_msg.edit_text(f"✅ PRIVATE CHANNEL VIEW COMPLETED!\n\n📊 FINAL RESULTS:\n✅ Joined channel: {results['joined']}\n⚠️ Already members: {results['already_joined']}\n👁️ Views sent: {results['views_sent']}\n❌ Failed: {results['failed']}\n📈 Success rate: {success_rate}%\n\n💡 Note: Views may take 1-2 minutes to update on Telegram")

# ========== LEAVE CHANNEL FUNCTIONS ==========
async def leave_specific_channel_func(client, channel_input):
    try:
        channel_input = channel_input.strip().replace('https://', '').replace('http://', '')
        
        if 't.me/c/' in channel_input:
            parts = channel_input.split('t.me/c/')[1].split('/')
            channel_id = int(parts[0])
            entity = PeerChannel(channel_id)
        else:
            channel_input = channel_input.replace('t.me/', '').replace('@', '').split('/')[0]
            entity = await client.get_entity(channel_input)
        
        await client(LeaveChannelRequest(entity))
        return True, "Left channel"
    except Exception as e:
        return False, str(e)[:50]

async def leave_all_channels_func(client):
    left_count = 0
    try:
        dialogs = await client.get_dialogs(limit=200)
        for dialog in dialogs:
            if dialog.is_channel or dialog.is_group:
                try:
                    await client(LeaveChannelRequest(dialog.entity))
                    left_count += 1
                    await asyncio.sleep(0.3)
                except:
                    pass
        return left_count
    except:
        return left_count

async def leave_channel_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🚪 Leave Specific Channel", callback_data="leave_specific_channel")],
        [InlineKeyboardButton("🗑️ Leave ALL Channels", callback_data="leave_all_channels")],
        [InlineKeyboardButton("🔙 Back", callback_data="main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.edit_message_text("🚪 LEAVE CHANNEL OPTIONS\n\nChoose an option:", reply_markup=reply_markup)
    else:
        await update.message.reply_text("🚪 LEAVE CHANNEL OPTIONS\n\nChoose an option:", reply_markup=reply_markup)

async def leave_specific_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    WAITING_FOR[user_id] = 'leave_specific_link'
    
    keyboard = [[InlineKeyboardButton("❌ CANCEL", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    msg_text = """🚪 LEAVE SPECIFIC CHANNEL

Send channel link/username:
• @username
• t.me/username
• t.me/c/channel_id

Example: @technicalguruji"""
    
    if update.callback_query:
        await update.callback_query.edit_message_text(msg_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(msg_text, reply_markup=reply_markup)

async def handle_leave_specific(update: Update, context: ContextTypes.DEFAULT_TYPE, link: str):
    user_id = update.effective_user.id
    accs = get_accessible_accounts(user_id)
    
    if not accs:
        await update.message.reply_text("❌ No accessible accounts!")
        WAITING_FOR.pop(user_id, None)
        return
    
    WAITING_FOR.pop(user_id, None)
    
    status_msg = await update.message.reply_text(f"🚪 Leaving channel from {len(accs)} accounts...")
    results = {'left': 0, 'not_member': 0, 'failed': 0}
    
    for i, acc in enumerate(accs):
        try:
            client = await get_client_for_account(acc)
            if not client:
                results['failed'] += 1
                continue
            
            success, msg = await leave_specific_channel_func(client, link)
            if success:
                results['left'] += 1
            else:
                if "not member" in msg.lower():
                    results['not_member'] += 1
                else:
                    results['failed'] += 1
                    
        except Exception as e:
            results['failed'] += 1
        
        if (i + 1) % 5 == 0 or (i + 1) == len(accs):
            await status_msg.edit_text(f"🚪 PROGRESS: {i+1}/{len(accs)}\n✅ Left: {results['left']}\n⚠️ Not member: {results['not_member']}\n❌ Failed: {results['failed']}")
        
        delay = load_settings(user_id).get('delay', DEFAULT_DELAY)
        await asyncio.sleep(delay)
    
    await status_msg.edit_text(f"✅ LEAVE CHANNEL COMPLETED!\n\n✅ Left: {results['left']}\n⚠️ Not member: {results['not_member']}\n❌ Failed: {results['failed']}")

async def leave_all_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    accs = get_accessible_accounts(user_id)
    
    if not accs:
        if update.callback_query:
            await update.callback_query.edit_message_text("❌ No accessible accounts!")
        else:
            await update.message.reply_text("❌ No accessible accounts!")
        return
    
    keyboard = [
        [InlineKeyboardButton("✅ YES, LEAVE ALL", callback_data="confirm_leave_all")],
        [InlineKeyboardButton("❌ CANCEL", callback_data="main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.edit_message_text(f"⚠️ LEAVE ALL CHANNELS\n\n📊 Accounts: {len(accs)}\n⚠️ This will leave ALL channels/groups!\n\nContinue?", reply_markup=reply_markup)
    else:
        await update.message.reply_text(f"⚠️ LEAVE ALL CHANNELS\n\n📊 Accounts: {len(accs)}\n⚠️ This will leave ALL channels/groups!\n\nContinue?", reply_markup=reply_markup)

async def confirm_leave_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    accs = get_accessible_accounts(user_id)
    
    status_msg = await query.edit_message_text(f"⏳ Leaving channels from {len(accs)} accounts...")
    
    results = {'accounts_done': 0, 'total_left': 0, 'failed': 0}
    
    for i, acc in enumerate(accs):
        try:
            client = await get_client_for_account(acc)
            if not client:
                results['failed'] += 1
                continue
            
            left = await leave_all_channels_func(client)
            results['accounts_done'] += 1
            results['total_left'] += left
            
        except Exception as e:
            results['failed'] += 1
        
        if (i + 1) % 3 == 0 or (i + 1) == len(accs):
            await status_msg.edit_text(f"⏳ PROGRESS: {i+1}/{len(accs)}\n✅ Accounts done: {results['accounts_done']}\n📤 Total left: {results['total_left']}\n❌ Failed: {results['failed']}")
        
        delay = load_settings(user_id).get('delay', DEFAULT_DELAY)
        await asyncio.sleep(delay)
    
    await status_msg.edit_text(f"✅ LEAVE ALL COMPLETED!\n\n✅ Accounts processed: {results['accounts_done']}\n📤 Total channels left: {results['total_left']}\n❌ Failed: {results['failed']}")

# ========== VC JOIN FUNCTION ==========
async def join_voice_chat_func(client, link):
    try:
        link = link.strip().replace('https://', '').replace('http://', '')
        
        if 't.me/' in link:
            username = link.split('t.me/')[1].split('/')[0].split('?')[0]
        else:
            return False, "Invalid voice chat link"
        
        try:
            chat_entity = await client.get_entity(username)
        except Exception as e:
            return False, f"Chat not found: {str(e)[:30]}"
        
        try:
            from telethon.tl.functions.phone import JoinGroupCallRequest
            from telethon.tl.types import InputGroupCall, DataJSON
            
            if str(chat_entity.id).startswith('-100'):
                full_chat = await client(GetFullChannelRequest(channel=chat_entity))
            else:
                from telethon.tl.functions.messages import GetFullChatRequest
                full_chat = await client(GetFullChatRequest(chat_id=chat_entity.id))
            
            if hasattr(full_chat, 'full_chat') and hasattr(full_chat.full_chat, 'call'):
                group_call = full_chat.full_chat.call
                if group_call:
                    result = await client(JoinGroupCallRequest(
                        call=InputGroupCall(id=group_call.id, access_hash=group_call.access_hash),
                        join_as=await client.get_me(),
                        params=DataJSON(data='{"muted": true, "video_stopped": true}')
                    ))
                    return True, "Joined voice chat"
            
            return False, "No active voice chat found"
            
        except Exception as e:
            return False, f"Failed to join: {str(e)[:40]}"
            
    except Exception as e:
        return False, f"Error: {str(e)[:40]}"

# ========== SEND REACTION ==========
async def send_reaction_func(client, link, emoji):
    try:
        link = link.strip().replace('https://', '').replace('http://', '')
        
        if 't.me/c/' in link:
            parts = link.split('t.me/c/')[1].split('/')
            channel_id = int(parts[0])
            msg_id = int(parts[1].split('?')[0])
            peer = PeerChannel(channel_id)
        elif 't.me/' in link:
            parts = link.split('t.me/')[1].split('/')
            if len(parts) >= 2 and parts[1].isdigit():
                username = parts[0]
                msg_id = int(parts[1].split('?')[0])
                peer = await client.get_entity(username)
            else:
                return False, "Invalid link"
        else:
            return False, "Invalid link"
        
        await client(SendReactionRequest(peer=peer, msg_id=msg_id, reaction=[ReactionEmoji(emoticon=emoji)]))
        return True, f"Reacted with {emoji}"
    except Exception as e:
        return False, str(e)[:40]

async def add_different_reactions(client, link):
    try:
        link = link.strip().replace('https://', '').replace('http://', '')
        
        if 't.me/c/' in link:
            parts = link.split('t.me/c/')[1].split('/')
            channel_id = int(parts[0])
            msg_id = int(parts[1].split('?')[0])
            peer = PeerChannel(channel_id)
        elif 't.me/' in link:
            parts = link.split('t.me/')[1].split('/')
            if len(parts) >= 2 and parts[1].isdigit():
                username = parts[0]
                msg_id = int(parts[1].split('?')[0])
                peer = await client.get_entity(username)
            else:
                return False, "Invalid link"
        else:
            return False, "Invalid link"
        
        random_reaction = random.choice(AVAILABLE_REACTIONS)
        await client(SendReactionRequest(peer=peer, msg_id=msg_id, reaction=[ReactionEmoji(emoticon=random_reaction)]))
        return True, f"Reacted with {random_reaction}"
    except Exception as e:
        return False, str(e)[:40]

# ========== VOTE POLL ==========
async def vote_poll_func(client, link):
    try:
        link = link.strip().replace('https://', '').replace('http://', '')
        
        if 't.me/c/' in link:
            parts = link.split('t.me/c/')[1].split('/')
            channel_id = int(parts[0])
            msg_id = int(parts[1].split('?')[0])
            peer = PeerChannel(channel_id)
        elif 't.me/' in link:
            parts = link.split('t.me/')[1].split('/')
            if len(parts) >= 2 and parts[1].isdigit():
                username = parts[0]
                msg_id = int(parts[1].split('?')[0])
                peer = await client.get_entity(username)
            else:
                return False, "Invalid link"
        else:
            return False, "Invalid link"
        
        msg = await client.get_messages(peer, ids=msg_id)
        if msg and msg.reply_markup:
            for row in msg.reply_markup.rows:
                for button in row.buttons:
                    if hasattr(button, 'data') and button.data:
                        await client(GetBotCallbackAnswerRequest(peer=peer, msg_id=msg_id, data=button.data))
                        return True, "Voted"
        return False, "No poll found"
    except Exception as e:
        return False, str(e)[:40]

# ========== JOIN CHANNEL ==========
async def join_channel_func(client, link):
    try:
        link = link.strip().replace('https://', '').replace('http://', '')
        if 't.me/+' in link or 'joinchat' in link:
            if 't.me/+' in link:
                hash_part = link.split('t.me/+')[-1].split('/')[0].split('?')[0]
            else:
                hash_part = link.split('joinchat/')[-1].split('/')[0].split('?')[0]
            await client(ImportChatInviteRequest(hash_part))
            return True, "Joined via invite"
        username = link.replace('t.me/', '').split('/')[0].split('?')[0]
        await client(JoinChannelRequest(username))
        return True, "Joined channel"
    except UserAlreadyParticipantError:
        return True, "Already joined"
    except Exception as e:
        return False, str(e)[:50]

# ========== GROUP SPAM ==========
async def group_spam_message(client, link, message_text):
    try:
        link = link.strip()
        chat_entity = None
        
        if 't.me/+' in link or 'joinchat' in link:
            if 't.me/+' in link:
                hash_part = link.split('t.me/+')[-1].split('/')[0].split('?')[0]
            else:
                hash_part = link.split('joinchat/')[-1].split('/')[0].split('?')[0]
            updates = await client(ImportChatInviteRequest(hash_part))
            if updates.chats:
                chat_entity = updates.chats[0]
        
        if not chat_entity:
            username = link.replace('t.me/', '').split('/')[0].split('?')[0]
            chat_entity = await client.get_entity(username)
        
        if message_text:
            await client.send_message(chat_entity, message_text)
            return True, "Message sent"
        return True, "Joined only"
    except FloodWaitError as e:
        return False, f"Flood wait {e.seconds}s"
    except Exception as e:
        return False, str(e)[:50]

# ========== SCHEDULED CHECKER ==========
async def check_scheduled_campaigns():
    while True:
        try:
            schedules = get_pending_schedules()
            for schedule in schedules:
                schedule_id, user_id, action, target, scheduled_time, account_count, spam_message = schedule
                accounts = get_accessible_accounts(user_id)
                live_accounts = []
                for acc in accounts:
                    if await is_account_live(acc):
                        live_accounts.append(acc)
                if live_accounts:
                    accounts_to_use = live_accounts[:account_count]
                    await run_scheduled_campaign(user_id, action, target, accounts_to_use, spam_message)
                update_scheduled_status(schedule_id, 'completed')
            await asyncio.sleep(30)
        except:
            await asyncio.sleep(30)

async def run_scheduled_campaign(user_id, action, target, accounts_to_use, spam_message):
    results = {'success': 0, 'failed': 0}
    for acc in accounts_to_use:
        try:
            client = await get_client_for_account(acc)
            if not client:
                results['failed'] += 1
                continue
            
            if action == 'join':
                success, msg = await join_channel_func(client, target)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
            elif action == 'react':
                success, msg = await send_reaction_func(client, target, '🔥')
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
            elif action == 'different_react':
                success, msg = await add_different_reactions(client, target)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
            elif action == 'view':
                try:
                    if 't.me/c/' in target:
                        parts = target.split('t.me/c/')[1].split('/')
                        channel_id = int(parts[0])
                        msg_id = int(parts[1].split('?')[0])
                        peer = PeerChannel(channel_id)
                        await client(GetMessagesViewsRequest(peer=peer, id=[msg_id], increment=True))
                        results['success'] += 1
                    else:
                        import re
                        match = re.search(r't\.me/([^/]+)/(\d+)', target)
                        if match:
                            channel = match.group(1)
                            msg_id = int(match.group(2))
                            entity = await client.get_entity(channel)
                            await client(GetMessagesViewsRequest(peer=entity, id=[msg_id], increment=True))
                            results['success'] += 1
                        else:
                            results['failed'] += 1
                except:
                    results['failed'] += 1
            elif action == 'vote':
                success, msg = await vote_poll_func(client, target)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
            elif action == 'vc':
                success, msg = await join_voice_chat_func(client, target)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
            elif action == 'group_spam':
                success, msg = await group_spam_message(client, target, spam_message)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
            elif action == 'dm':
                msg_to_send = spam_message if spam_message else "Hello!"
                if target.isdigit():
                    entity = await client.get_entity(int(target))
                else:
                    entity = await client.get_entity(target)
                await client.send_message(entity, msg_to_send)
                results['success'] += 1
        except Exception as e:
            results['failed'] += 1
        delay = load_settings(user_id).get('delay', DEFAULT_DELAY)
        await asyncio.sleep(delay)
    
    campaign_data = {
        'action': action,
        'target': target,
        'result': f"Success {results['success']} / Failed {results['failed']}",
        'status': 'completed',
        'timestamp': str(datetime.now()),
        'accounts_used': len(accounts_to_use),
        'success_count': results['success'],
        'failed_count': results['failed']
    }
    save_campaign(user_id, campaign_data)

# ========== MAIN BOT FUNCTIONS ==========
PENDING_OTP = {}
PENDING_2FA = {}
WAITING_FOR = {}
PENDING_CAMPAIGN = {}
SELECTED_EMOJIS = {}
SELECTED_PREMIUM = {}
SELECTED_ACCOUNT_COUNT = {}
ACCESS_DATA = {}
PENDING_SPAM_MSG = {}
PENDING_SCHEDULE = {}

async def cancel_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except:
        pass
    user_id = query.from_user.id
    WAITING_FOR.pop(user_id, None)
    PENDING_OTP.pop(user_id, None)
    PENDING_2FA.pop(user_id, None)
    PENDING_CAMPAIGN.pop(user_id, None)
    SELECTED_EMOJIS.pop(user_id, None)
    SELECTED_PREMIUM.pop(user_id, None)
    SELECTED_ACCOUNT_COUNT.pop(user_id, None)
    ACCESS_DATA.pop(user_id, None)
    PENDING_SPAM_MSG.pop(user_id, None)
    PENDING_SCHEDULE.pop(user_id, None)
    try:
        await query.edit_message_text("❌ Operation cancelled.\n\nReturning to main menu...")
    except:
        pass
    await show_main_menu(update, context)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if is_banned(user_id):
        if update.callback_query:
            await update.callback_query.edit_message_text("❌ You are banned!")
        else:
            await update.message.reply_text("❌ You are banned!")
        return
    
    has_access_bool, expiry = has_access(user_id)
    if not has_access_bool and not is_owner(user_id):
        if update.callback_query:
            await update.callback_query.edit_message_text("❌ You don't have access!\n\nContact @SHIVAMKR_208")
        else:
            await update.message.reply_text("❌ You don't have access!\n\nContact @SHIVAMKR_208")
        return
    
    first_name = update.effective_user.first_name or "User"
    
    if is_owner(user_id):
        owner_accounts = load_owner_accounts()
        active_count = 0
        for acc in owner_accounts[:30]:
            if await is_account_live(acc):
                active_count += 1
        accounts_text = f"📊 Owner Accounts: {len(owner_accounts)} ({active_count} active)"
        keyboard = [
            [InlineKeyboardButton("🟢 ADMIN PANEL", callback_data="admin_panel")],
            [InlineKeyboardButton("Add Account", callback_data="add_account"), InlineKeyboardButton("My Accounts", callback_data="my_accounts")],
            [InlineKeyboardButton("Shopping", callback_data="new_campaign"), InlineKeyboardButton("My Purchased", callback_data="my_campaigns")],
            [InlineKeyboardButton("Scheduled", callback_data="scheduled"), InlineKeyboardButton("My Stats", callback_data="my_stats")],
            [InlineKeyboardButton("Settings", callback_data="settings"), InlineKeyboardButton("My Profile", callback_data="profile")],
            [InlineKeyboardButton("Help & Guide", callback_data="help"), InlineKeyboardButton("Support", callback_data="support")],
            [InlineKeyboardButton("👥 Give Access", callback_data="give_access"), InlineKeyboardButton("❌ Remove Access", callback_data="remove_access")],
        ]
    else:
        personal_accounts = load_accounts(user_id)
        shared_limit = get_user_shared_limit(user_id)
        total_available = len(get_accessible_accounts(user_id))
        accounts_text = f"📊 Personal: {len(personal_accounts)} | Shared: {shared_limit} | Total: {total_available}\n⏰ Access until: {expiry}"
        keyboard = [
            [InlineKeyboardButton("Add Account", callback_data="add_account"), InlineKeyboardButton("My Accounts", callback_data="my_accounts")],
            [InlineKeyboardButton("Shopping", callback_data="new_campaign"), InlineKeyboardButton("My Purchased", callback_data="my_campaigns")],
            [InlineKeyboardButton("Scheduled", callback_data="scheduled"), InlineKeyboardButton("My Stats", callback_data="my_stats")],
            [InlineKeyboardButton("Settings", callback_data="settings"), InlineKeyboardButton("My Profile", callback_data="profile")],
            [InlineKeyboardButton("Help & Guide", callback_data="help"), InlineKeyboardButton("Support", callback_data="support")],
        ]
    
    text = f"""Welcome back, {first_name}! 🎉

Auto Voter - Telegram Automation Bot

React • Vote • View • Join • DM • VC • Spam

{accounts_text}

Choose an option:"""
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(text, reply_markup=reply_markup)
    except:
        pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or ""
    first_name = update.effective_user.first_name or ""
    
    conn = sqlite3.connect('automation_bot.db')
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name, joined_date, is_banned) VALUES (?, ?, ?, ?, ?)",
             (user_id, username, first_name, str(datetime.now()), 0))
    conn.commit()
    conn.close()
    
    await show_main_menu(update, context)

# ========== GIVE ACCESS ==========
async def give_access_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id):
        await update.callback_query.answer("❌ Only Owner!", show_alert=True)
        return
    
    WAITING_FOR[user_id] = 'access_user_id'
    keyboard = [[InlineKeyboardButton("CANCEL", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(
        "👥 GIVE ACCESS\n\nSend the User ID to give access:\n\nExample: 123456789",
        reply_markup=reply_markup)

async def handle_access_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only Owner!")
        WAITING_FOR.pop(user_id, None)
        return
    
    try:
        target_user = int(text.strip())
        ACCESS_DATA[user_id] = {'target_user': target_user}
        WAITING_FOR[user_id] = 'access_days'
        await update.message.reply_text("📅 How many days access?\n\nSend a number (e.g., 30 for 30 days):")
    except:
        await update.message.reply_text("❌ Invalid User ID!")
        WAITING_FOR.pop(user_id, None)

async def handle_access_days(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only Owner!")
        WAITING_FOR.pop(user_id, None)
        return
    
    try:
        days = int(text.strip())
        if days <= 0:
            raise ValueError
        ACCESS_DATA[user_id]['days'] = days
        
        keyboard = [
            [InlineKeyboardButton("✅ YES", callback_data="access_more_yes"),
             InlineKeyboardButton("❌ NO", callback_data="access_more_no_direct")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text("You Want To Give Also ID Access?", reply_markup=reply_markup)
        WAITING_FOR.pop(user_id, None)
        
    except:
        await update.message.reply_text("❌ Please send a valid number!")
        WAITING_FOR.pop(user_id, None)

async def access_more_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if not is_owner(user_id):
        await query.answer("❌ Only Owner!", show_alert=True)
        return
    
    await query.edit_message_text("📊 How many IDs access you want to give?\n\nSend a number:")
    WAITING_FOR[user_id] = 'access_shared_limit'

async def access_more_no_direct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if not is_owner(user_id):
        await query.answer("❌ Only Owner!", show_alert=True)
        return
    
    target_user = ACCESS_DATA[user_id]['target_user']
    days = ACCESS_DATA[user_id]['days']
    shared_limit = 0
    
    give_access(target_user, days, shared_limit)
    
    conn = sqlite3.connect('automation_bot.db')
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name, joined_date) VALUES (?, ?, ?, ?)",
              (target_user, "", "", str(datetime.now())))
    conn.commit()
    conn.close()
    
    owner_accounts = load_owner_accounts()
    
    await query.edit_message_text(
        f"✅ ACCESS GRANTED!\n\n"
        f"👤 User ID: {target_user}\n"
        f"📅 Days: {days}\n"
        f"📊 Shared IDs Limit: {shared_limit}\n"
        f"📚 Owner has {len(owner_accounts)} total accounts\n\n"
        f"User can now use the bot!"
    )
    
    ACCESS_DATA.pop(user_id, None)
    await asyncio.sleep(2)
    await show_main_menu(update, context)

async def handle_access_shared_limit(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only Owner!")
        WAITING_FOR.pop(user_id, None)
        return
    
    try:
        shared_limit = int(text.strip())
        if shared_limit < 0:
            raise ValueError
        
        target_user = ACCESS_DATA[user_id]['target_user']
        days = ACCESS_DATA[user_id]['days']
        
        give_access(target_user, days, shared_limit)
        
        conn = sqlite3.connect('automation_bot.db')
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name, joined_date) VALUES (?, ?, ?, ?)",
                  (target_user, "", "", str(datetime.now())))
        conn.commit()
        conn.close()
        
        owner_accounts = load_owner_accounts()
        
        WAITING_FOR.pop(user_id, None)
        ACCESS_DATA.pop(user_id, None)
        
        await update.message.reply_text(
            f"✅ ACCESS GRANTED!\n\n"
            f"👤 User ID: {target_user}\n"
            f"📅 Days: {days}\n"
            f"📊 Shared IDs Limit: {shared_limit}\n"
            f"📚 Owner has {len(owner_accounts)} total accounts\n\n"
            f"User can now use the bot!"
        )
        
        await asyncio.sleep(2)
        await show_main_menu(update, context)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        WAITING_FOR.pop(user_id, None)
        ACCESS_DATA.pop(user_id, None)

# ========== REMOVE ACCESS ==========
async def remove_access_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id):
        await update.callback_query.answer("❌ Only Owner!", show_alert=True)
        return
    
    WAITING_FOR[user_id] = 'remove_access_user_id'
    keyboard = [[InlineKeyboardButton("CANCEL", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(
        "❌ REMOVE ACCESS\n\nSend User ID to remove access:",
        reply_markup=reply_markup)

async def handle_remove_access(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only Owner!")
        WAITING_FOR.pop(user_id, None)
        return
    
    try:
        target_user = int(text.strip())
        if target_user == OWNER_ID:
            await update.message.reply_text("❌ Cannot remove owner!")
            WAITING_FOR.pop(user_id, None)
            return
        
        remove_user_access(target_user)
        WAITING_FOR.pop(user_id, None)
        
        await update.message.reply_text(f"✅ Access removed for User ID: {target_user}")
        
        await asyncio.sleep(2)
        await show_main_menu(update, context)
        
    except:
        await update.message.reply_text("❌ Invalid User ID!")
        WAITING_FOR.pop(user_id, None)

# ========== ADD ACCOUNT ==========
async def add_account_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    
    has_access_bool, expiry = has_access(user_id)
    if not has_access_bool and not is_owner(user_id):
        await update.callback_query.answer("❌ You don't have access!", show_alert=True)
        return
    
    keyboard = [
        [InlineKeyboardButton("Phone + OTP", callback_data="add_phone_otp")],
        [InlineKeyboardButton("Session String", callback_data="add_session_string")],
        [InlineKeyboardButton("Bulk Sessions", callback_data="add_bulk_sessions")],
        [InlineKeyboardButton("CANCEL", callback_data="cancel_action")],
    ]
    text = "📱 Add Telegram Account\n\nHow would you like to add an account?"
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    except:
        pass

async def add_phone_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    WAITING_FOR[user_id] = 'phone'
    keyboard = [[InlineKeyboardButton("CANCEL", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(
        "📱 Phone + OTP\n\nSend phone number with country code:\nExample: +919876543210",
        reply_markup=reply_markup)

async def handle_phone(update: Update, context: ContextTypes.DEFAULT_TYPE, phone: str):
    user_id = update.effective_user.id
    
    phone = phone.strip().replace(" ", "")
    if not phone.startswith('+'):
        await update.message.reply_text("❌ Phone number must start with +")
        WAITING_FOR.pop(user_id, None)
        return
    
    accounts = load_accounts(user_id)
    for acc in accounts:
        if acc.get('phone') == phone:
            await update.message.reply_text("❌ This account is already added!")
            WAITING_FOR.pop(user_id, None)
            return
    
    status = await update.message.reply_text("📱 Sending OTP request...")
    try:
        session_path = f"sessions/user_{user_id}_{int(time.time())}"
        os.makedirs("sessions", exist_ok=True)
        client = TelegramClient(session_path, API_ID, API_HASH)
        await client.connect()
        sent = await client.send_code_request(phone)
        PENDING_OTP[user_id] = {'client': client, 'phone': phone, 'hash': sent.phone_code_hash, 'session': session_path}
        await status.edit_text("✅ OTP sent! Enter the 5-digit code:")
        WAITING_FOR.pop(user_id, None)
    except Exception as e:
        await status.edit_text(f"❌ Error: {str(e)[:100]}")
        WAITING_FOR.pop(user_id, None)

async def verify_otp(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str):
    user_id = update.effective_user.id
    if user_id not in PENDING_OTP:
        await update.message.reply_text("❌ No pending OTP!")
        return
    
    data = PENDING_OTP[user_id]
    status = await update.message.reply_text("🔐 Verifying OTP...")
    try:
        await data['client'].sign_in(phone=data['phone'], code=code, phone_code_hash=data['hash'])
        me = await data['client'].get_me()
        
        session_string = data['client'].session.save()
        
        account = {
            'phone': data['phone'], 
            'session': data['session'], 
            'session_string': session_string,
            'username': me.username or 'No username', 
            'user_id': me.id, 
            'first_name': me.first_name or '', 
            'added_date': str(datetime.now()), 
            'type': 'phone_otp'
        }
        accounts = load_accounts(user_id)
        accounts.append(account)
        save_accounts(user_id, accounts)
        
        ACTIVE_CLIENTS[data['phone']] = data['client']
        
        if data['phone'] in LIVE_CACHE:
            del LIVE_CACHE[data['phone']]
        
        del PENDING_OTP[user_id]
        await status.edit_text(f"✅ Account added!\n👤 @{account['username']}\n📱 {data['phone']}\n\n✅ Account is LIVE!")
        
    except SessionPasswordNeededError:
        PENDING_2FA[user_id] = {'client': data['client'], 'phone': data['phone'], 'session': data['session']}
        del PENDING_OTP[user_id]
        await status.edit_text("🔐 2FA enabled! Send your password:")
    except Exception as e:
        await status.edit_text(f"❌ Error: {str(e)[:100]}")

async def verify_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE, password: str):
    user_id = update.effective_user.id
    if user_id not in PENDING_2FA:
        await update.message.reply_text("❌ No pending 2FA!")
        return
    
    data = PENDING_2FA[user_id]
    status = await update.message.reply_text("🔐 Verifying 2FA...")
    try:
        await data['client'].sign_in(password=password)
        me = await data['client'].get_me()
        
        session_string = data['client'].session.save()
        
        account = {
            'phone': data['phone'], 
            'session': data['session'], 
            'session_string': session_string,
            'username': me.username or 'No username', 
            'user_id': me.id, 
            'first_name': me.first_name or '', 
            'added_date': str(datetime.now()), 
            'type': 'phone_otp'
        }
        accounts = load_accounts(user_id)
        accounts.append(account)
        save_accounts(user_id, accounts)
        
        ACTIVE_CLIENTS[data['phone']] = data['client']
        
        if data['phone'] in LIVE_CACHE:
            del LIVE_CACHE[data['phone']]
        
        del PENDING_2FA[user_id]
        await status.edit_text(f"✅ 2FA Account added!\n👤 @{account['username']}\n📱 {data['phone']}\n\n✅ Account is LIVE!")
    except Exception as e:
        await status.edit_text(f"❌ Error: {str(e)[:100]}")

async def add_session_string(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    WAITING_FOR[user_id] = 'session_string'
    keyboard = [[InlineKeyboardButton("CANCEL", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text("🔑 Session String\n\nSend your Telethon session string.", reply_markup=reply_markup)

async def handle_session_string(update: Update, context: ContextTypes.DEFAULT_TYPE, session_str: str):
    user_id = update.effective_user.id
    status = await update.message.reply_text("🔐 Connecting...")
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await status.edit_text("❌ Invalid session string!")
            WAITING_FOR.pop(user_id, None)
            return
        
        me = await client.get_me()
        phone = me.phone if me.phone else f"user_{me.id}"
        
        accounts = load_accounts(user_id)
        for acc in accounts:
            if acc.get('phone') == phone:
                await status.edit_text(f"❌ Already added!")
                WAITING_FOR.pop(user_id, None)
                return
        
        session_path = f"sessions/user_{user_id}_{int(time.time())}"
        os.makedirs("sessions", exist_ok=True)
        with open(f"{session_path}.session_string", 'w') as f:
            f.write(session_str)
        
        account = {
            'phone': phone, 
            'session': session_path, 
            'session_string': session_str, 
            'username': me.username or 'No username', 
            'user_id': me.id, 
            'first_name': me.first_name or '', 
            'added_date': str(datetime.now()), 
            'type': 'session_string'
        }
        accounts.append(account)
        save_accounts(user_id, accounts)
        
        ACTIVE_CLIENTS[phone] = client
        
        if phone in LIVE_CACHE:
            del LIVE_CACHE[phone]
        
        WAITING_FOR.pop(user_id, None)
        await status.edit_text(f"✅ Account added!\n👤 @{account['username']}\n📱 {phone}\n\n✅ Account is LIVE!")
    except Exception as e:
        await status.edit_text(f"❌ Error: {str(e)[:100]}")
        WAITING_FOR.pop(user_id, None)

async def add_bulk_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    WAITING_FOR[user_id] = 'bulk_sessions'
    keyboard = [[InlineKeyboardButton("CANCEL", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text("📦 Bulk Sessions\n\nSend multiple session strings (one per line):", reply_markup=reply_markup)

async def handle_bulk_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    lines = text.strip().split('\n')
    session_strings = [s.strip() for s in lines if s.strip()]
    
    status = await update.message.reply_text(f"📦 Processing {len(session_strings)} sessions...")
    added = 0
    
    for session_str in session_strings:
        try:
            client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                continue
            
            me = await client.get_me()
            phone = me.phone if me.phone else f"user_{me.id}"
            
            accounts = load_accounts(user_id)
            already = False
            for acc in accounts:
                if acc.get('phone') == phone:
                    already = True
                    break
            if already:
                continue
            
            session_path = f"sessions/user_{user_id}_{int(time.time())}_{added}"
            with open(f"{session_path}.session_string", 'w') as f:
                f.write(session_str)
            
            account = {
                'phone': phone, 
                'session': session_path, 
                'session_string': session_str, 
                'username': me.username or 'No username', 
                'user_id': me.id, 
                'first_name': me.first_name or '', 
                'added_date': str(datetime.now()), 
                'type': 'bulk_session'
            }
            accounts.append(account)
            save_accounts(user_id, accounts)
            ACTIVE_CLIENTS[phone] = client
            
            if phone in LIVE_CACHE:
                del LIVE_CACHE[phone]
            
            added += 1
        except:
            pass
        await asyncio.sleep(0.3)
    
    WAITING_FOR.pop(user_id, None)
    await status.edit_text(f"✅ Added {added}/{len(session_strings)} accounts!")

async def my_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    
    if is_owner(user_id):
        all_accounts = load_owner_accounts()
        live = []
        expired = []
        for a in all_accounts:
            if await is_account_live(a):
                live.append(a)
            else:
                expired.append(a)
        
        text = f"📋 Owner Accounts\n━━━━━━━━━━━━━━━━━━━━━━\n\nTotal: {len(live)}\n\n"
        for a in live[:10]:
            text += f"👤 @{a.get('username', 'No username')}\n📱 {a['phone']}\n\n"
        if len(live) > 10:
            text += f"\n... and {len(live) - 10} more"
        
        keyboard = [
            [InlineKeyboardButton(f"✅ Live ({len(live)})", callback_data="admin_view_live"), InlineKeyboardButton(f"❌ Expired ({len(expired)})", callback_data="admin_view_expired")],
            [InlineKeyboardButton("🗑️ Remove", callback_data="admin_remove_prompt"), InlineKeyboardButton("⚠️ REMOVE ALL", callback_data="admin_remove_all_prompt")],
            [InlineKeyboardButton("➕ Add Another", callback_data="add_account")],
            [InlineKeyboardButton("❌ CANCEL", callback_data="cancel_action")],
        ]
    else:
        personal_accounts = load_accounts(user_id)
        shared_limit = get_user_shared_limit(user_id)
        owner_accounts = load_owner_accounts()
        
        personal_live = []
        for a in personal_accounts:
            if await is_account_live(a):
                personal_live.append(a)
        
        user_phones = [a.get('phone') for a in personal_accounts]
        available_shared = [a for a in owner_accounts if a.get('phone') not in user_phones]
        shared_to_show = available_shared[:shared_limit]
        
        text = f"📋 Your Personal Accounts\n━━━━━━━━━━━━━━━━━━━━━━\n\nTotal: {len(personal_live)}\n\n"
        for a in personal_live[:10]:
            text += f"👤 @{a.get('username', 'No username')}\n📱 {a['phone']}\n\n"
        
        if shared_limit > 0 and shared_to_show:
            text += f"\n📋 Shared Accounts - {len(shared_to_show)}/{shared_limit}\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
            for a in shared_to_show[:10]:
                text += f"👤 @{a.get('username', 'No username')}\n📱 {a['phone']}\n(Shared by Owner)\n\n"
        
        keyboard = [
            [InlineKeyboardButton("➕ Add Account", callback_data="add_account")],
            [InlineKeyboardButton("🗑️ Remove Personal Account", callback_data="user_remove_prompt")],
            [InlineKeyboardButton("❌ CANCEL", callback_data="cancel_action")],
        ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    if len(text) > 4000:
        text = text[:3800] + "\n\n... (truncated)"
    try:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    except:
        pass

# ========== SHOPPING (NEW CAMPAIGN) ==========
async def new_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    accs = get_accessible_accounts(user_id)
    
    if not accs:
        await update.callback_query.edit_message_text(
            "❌ No accounts available!\n\nPlease add accounts first.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ ADD ACCOUNT", callback_data="add_account")]]))
        return
    
    live_accs = []
    for a in accs:
        if await is_account_live(a):
            live_accs.append(a)
    
    if not live_accs:
        await update.callback_query.edit_message_text("❌ No active accounts found!")
        return
    
    context.user_data['campaign_accounts'] = live_accs
    
    keyboard = [
        [InlineKeyboardButton("📢 Join Channel", callback_data="campaign_action_join"), InlineKeyboardButton("🚪 Leave Channel", callback_data="leave_channel_menu")],
        [InlineKeyboardButton("❤️ React Only", callback_data="campaign_action_react"), InlineKeyboardButton("🎲 Different Reactions", callback_data="campaign_action_different_react")],
        [InlineKeyboardButton("👁️ View Only", callback_data="private_channel_view"), InlineKeyboardButton("🗳️ Vote Only", callback_data="campaign_action_vote")],
        [InlineKeyboardButton("❤️‍🔥 React + Vote", callback_data="campaign_action_reactvote"), InlineKeyboardButton("❤️ React + View", callback_data="campaign_action_reactview")],
        [InlineKeyboardButton("🗳️ Vote + View", callback_data="campaign_action_voteview"), InlineKeyboardButton("❤️‍🔥🗳️👁️ All Three", callback_data="campaign_action_reactvoteview")],
        [InlineKeyboardButton("💬 Bulk DM", callback_data="campaign_action_dm"), InlineKeyboardButton("🔊 VC", callback_data="campaign_action_vc")],
        [InlineKeyboardButton("📢 Group Spam", callback_data="campaign_action_group_spam"), InlineKeyboardButton("🔙 Back", callback_data="main")],
    ]
    
    await update.callback_query.edit_message_text(f"🛍️ SHOPPING\n\n📊 Active Accounts: {len(live_accs)}\n✅ Select action:", reply_markup=InlineKeyboardMarkup(keyboard))

async def campaign_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
    user_id = update.callback_query.from_user.id
    accounts = context.user_data.get('campaign_accounts', [])
    
    if not accounts:
        await update.callback_query.edit_message_text("❌ No accounts available!")
        return
    
    context.user_data['campaign_action'] = action
    WAITING_FOR[user_id] = 'campaign_link'
    
    keyboard = [[InlineKeyboardButton("❌ CANCEL", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    action_msgs = {
        'join': 'Send channel link to join:\nExample: t.me/username',
        'react': 'Send post link to react:\nExample: t.me/username/123 or t.me/c/123456789/123',
        'different_react': 'Send post link for random reactions:\nExample: t.me/username/123',
        'vote': 'Send post link to vote:\nExample: t.me/username/123',
        'dm': 'Send username or ID to DM:\nExample: @username',
        'vc': 'Send voice chat link:\nExample: t.me/username',
        'group_spam': 'Send group link to spam:\nExample: t.me/groupname',
    }
    
    msg = action_msgs.get(action, 'Send the link:')
    
    await update.callback_query.edit_message_text(f"📢 {action.upper()}\n\nThis action will be performed on {len(accounts)} accounts\n\n{msg}", reply_markup=reply_markup)

async def handle_campaign_link(update: Update, context: ContextTypes.DEFAULT_TYPE, link: str):
    user_id = update.effective_user.id
    action = context.user_data.get('campaign_action')
    accounts = context.user_data.get('campaign_accounts', [])
    
    if not action or not accounts:
        await update.message.reply_text("❌ Please start campaign again.")
        WAITING_FOR.pop(user_id, None)
        return
    
    PENDING_CAMPAIGN[user_id] = {
        'action': action,
        'link': link,
        'accounts': accounts
    }
    
    if action == 'group_spam' or action == 'dm':
        WAITING_FOR[user_id] = 'spam_message'
        await update.message.reply_text("📝 Now send the message to spam/send:")
        return
    
    WAITING_FOR[user_id] = 'account_count'
    
    keyboard = [
        [InlineKeyboardButton(f"📊 All ({len(accounts)})", callback_data=f"use_all_{len(accounts)}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(f"How many accounts?\n\nYou have {len(accounts)} active account(s).\n\nSend number or tap All:", reply_markup=reply_markup)

async def handle_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE, msg: str):
    user_id = update.effective_user.id
    campaign = PENDING_CAMPAIGN.get(user_id)
    
    if not campaign:
        await update.message.reply_text("❌ Campaign data lost!")
        WAITING_FOR.pop(user_id, None)
        return
    
    PENDING_SPAM_MSG[user_id] = msg
    WAITING_FOR[user_id] = 'account_count'
    
    accounts = campaign.get('accounts', [])
    keyboard = [
        [InlineKeyboardButton(f"📊 All ({len(accounts)})", callback_data=f"use_all_{len(accounts)}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(f"How many accounts?\n\nYou have {len(accounts)} active account(s).\n\nSend number or tap All:", reply_markup=reply_markup)

async def use_all_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    count_str = query.data.replace("use_all_", "")
    count = int(count_str)
    
    SELECTED_ACCOUNT_COUNT[user_id] = count
    await show_campaign_summary(update, context, user_id)

async def handle_account_count(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    accounts = context.user_data.get('campaign_accounts', [])
    
    try:
        if text.lower() == 'all':
            count = len(accounts)
        else:
            count = int(text)
        
        if count < 1 or count > len(accounts):
            await update.message.reply_text(f"❌ Send number between 1 and {len(accounts)}")
            return
        
        SELECTED_ACCOUNT_COUNT[user_id] = count
        WAITING_FOR.pop(user_id, None)
        await show_campaign_summary(update, context, user_id)
    except ValueError:
        await update.message.reply_text(f"❌ Send valid number or 'all'")

async def show_campaign_summary(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    campaign = PENDING_CAMPAIGN.get(user_id)
    if not campaign:
        await update.message.reply_text("❌ Campaign data lost!")
        return
    
    action = campaign['action']
    link = campaign['link']
    accounts = campaign.get('accounts', [])
    use_count = SELECTED_ACCOUNT_COUNT.get(user_id, len(accounts))
    
    action_names = {
        'join': '📢 Join Channel',
        'react': '❤️ React Only',
        'different_react': '🎲 Different Reactions', 
        'view': '👁️ View Only', 
        'vote': '🗳️ Vote Only', 
        'reactvote': '❤️‍🔥 React + Vote',
        'reactview': '❤️ React + View', 
        'voteview': '🗳️ Vote + View',
        'reactvoteview': '❤️‍🔥🗳️👁️ All Three', 
        'dm': '💬 Bulk DM',
        'vc': '🔊 VC', 
        'group_spam': '📢 Group Spam'
    }
    
    summary = f"""📢 CAMPAIGN SUMMARY
━━━━━━━━━━━━━━━━━━━━━━

Action: {action_names.get(action, action)}
Target: {link}

Accounts: {use_count} will run

Tap RUN to start"""
    
    keyboard = [
        [InlineKeyboardButton("🚀 RUN", callback_data="run_campaign")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.edit_message_text(summary, reply_markup=reply_markup)
    else:
        await update.message.reply_text(summary, reply_markup=reply_markup)

# ========== RUN CAMPAIGN ==========
async def run_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    campaign = PENDING_CAMPAIGN.get(user_id)
    spam_msg = PENDING_SPAM_MSG.get(user_id)
    
    if not campaign:
        await update.callback_query.edit_message_text("❌ Campaign data lost!")
        return
    
    action = campaign['action']
    link = campaign['link']
    accounts = campaign.get('accounts', [])
    use_count = SELECTED_ACCOUNT_COUNT.get(user_id, len(accounts))
    
    accounts_to_use = accounts[:use_count]
    
    if not accounts_to_use:
        await update.callback_query.edit_message_text("❌ No active accounts!")
        return
    
    status_msg = await update.callback_query.edit_message_text(f"🚀 CAMPAIGN RUNNING...\n\nAction: {action}\nAccounts: {len(accounts_to_use)}\n\nProcessing...", reply_markup=None)
    
    results = {'success': 0, 'failed': 0, 'errors': []}
    
    for i, acc in enumerate(accounts_to_use):
        try:
            client = await get_client_for_account(acc)
            if not client:
                results['failed'] += 1
                results['errors'].append(f"Connection failed")
                continue
            
            # JOIN CHANNEL
            if action == 'join':
                success, msg = await join_channel_func(client, link)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
                    results['errors'].append(f"Join: {msg}")
                continue
            
            # BULK DM
            elif action == 'dm':
                target = link.strip()
                msg_to_send = spam_msg if spam_msg else "Hello!"
                if target.isdigit():
                    entity = await client.get_entity(int(target))
                else:
                    entity = await client.get_entity(target)
                await client.send_message(entity, msg_to_send)
                results['success'] += 1
                continue
            
            # VC
            elif action == 'vc':
                success, msg = await join_voice_chat_func(client, link)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
                    results['errors'].append(f"VC: {msg}")
                continue
            
            # GROUP SPAM
            elif action == 'group_spam':
                success, msg = await group_spam_message(client, link, spam_msg)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
                    results['errors'].append(msg)
                continue
            
            # REACT ONLY
            elif action == 'react':
                success, msg = await send_reaction_func(client, link, '🔥')
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
                    results['errors'].append(msg)
                continue
            
            # DIFFERENT REACTIONS
            elif action == 'different_react':
                success, msg = await add_different_reactions(client, link)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
                    results['errors'].append(msg)
                continue
            
            # VIEW ONLY
            elif action == 'view':
                try:
                    if 't.me/c/' in link:
                        parts = link.split('t.me/c/')[1].split('/')
                        channel_id = int(parts[0])
                        msg_id = int(parts[1].split('?')[0])
                        peer = PeerChannel(channel_id)
                        result = await client(GetMessagesViewsRequest(peer=peer, id=[msg_id], increment=True))
                        if result:
                            results['success'] += 1
                        else:
                            results['failed'] += 1
                            results['errors'].append("Failed to add view")
                    else:
                        import re
                        match = re.search(r't\.me/([^/]+)/(\d+)', link)
                        if match:
                            channel = match.group(1)
                            msg_id = int(match.group(2))
                            entity = await client.get_entity(channel)
                            result = await client(GetMessagesViewsRequest(peer=entity, id=[msg_id], increment=True))
                            if result:
                                results['success'] += 1
                            else:
                                results['failed'] += 1
                                results['errors'].append("Failed to add view")
                        else:
                            results['failed'] += 1
                            results['errors'].append("Invalid link format")
                except FloodWaitError as e:
                    results['failed'] += 1
                    results['errors'].append(f"Flood wait {e.seconds}s")
                    await asyncio.sleep(min(e.seconds, 5))
                except Exception as e:
                    results['failed'] += 1
                    results['errors'].append(str(e)[:40])
                continue
            
            # VOTE ONLY
            elif action == 'vote':
                success, msg = await vote_poll_func(client, link)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
                    results['errors'].append(msg)
                continue
            
            # For combined actions
            else:
                import re
                is_private = 't.me/c/' in link
                
                if is_private:
                    parts = link.split('t.me/c/')[1].split('/')
                    channel_id = int(parts[0])
                    msg_id = int(parts[1].split('?')[0])
                    entity = PeerChannel(channel_id)
                else:
                    match = re.search(r't\.me/([^/]+)/(\d+)', link)
                    if not match:
                        results['failed'] += 1
                        results['errors'].append("Invalid link")
                        continue
                    channel = match.group(1)
                    msg_id = int(match.group(2))
                    entity = await client.get_entity(channel)
                
                if action == 'reactvote':
                    try:
                        await client(SendReactionRequest(peer=entity, msg_id=msg_id, reaction=[ReactionEmoji(emoticon='🔥')]))
                        msg = await client.get_messages(entity, ids=msg_id)
                        if msg and msg.reply_markup:
                            for row in msg.reply_markup.rows:
                                for button in row.buttons:
                                    if hasattr(button, 'data') and button.data:
                                        await client(GetBotCallbackAnswerRequest(peer=entity, msg_id=msg_id, data=button.data))
                                        break
                                break
                        results['success'] += 1
                    except Exception as e:
                        results['failed'] += 1
                        results['errors'].append(str(e)[:40])
                
                elif action == 'reactview':
                    try:
                        await client(SendReactionRequest(peer=entity, msg_id=msg_id, reaction=[ReactionEmoji(emoticon='🔥')]))
                        await client(GetMessagesViewsRequest(peer=entity, id=[msg_id], increment=True))
                        results['success'] += 1
                    except Exception as e:
                        results['failed'] += 1
                        results['errors'].append(str(e)[:40])
                
                elif action == 'voteview':
                    try:
                        msg = await client.get_messages(entity, ids=msg_id)
                        if msg and msg.reply_markup:
                            for row in msg.reply_markup.rows:
                                for button in row.buttons:
                                    if hasattr(button, 'data') and button.data:
                                        await client(GetBotCallbackAnswerRequest(peer=entity, msg_id=msg_id, data=button.data))
                                        break
                                break
                        await client(GetMessagesViewsRequest(peer=entity, id=[msg_id], increment=True))
                        results['success'] += 1
                    except Exception as e:
                        results['failed'] += 1
                        results['errors'].append(str(e)[:40])
                
                elif action == 'reactvoteview':
                    try:
                        await client(SendReactionRequest(peer=entity, msg_id=msg_id, reaction=[ReactionEmoji(emoticon='🔥')]))
                        msg = await client.get_messages(entity, ids=msg_id)
                        if msg and msg.reply_markup:
                            for row in msg.reply_markup.rows:
                                for button in row.buttons:
                                    if hasattr(button, 'data') and button.data:
                                        await client(GetBotCallbackAnswerRequest(peer=entity, msg_id=msg_id, data=button.data))
                                        break
                                break
                        await client(GetMessagesViewsRequest(peer=entity, id=[msg_id], increment=True))
                        results['success'] += 1
                    except Exception as e:
                        results['failed'] += 1
                        results['errors'].append(str(e)[:40])
                
        except FloodWaitError as e:
            results['failed'] += 1
            results['errors'].append(f"Flood wait {e.seconds}s")
            await asyncio.sleep(min(e.seconds, 5))
        except Exception as e:
            results['failed'] += 1
            results['errors'].append(f"{str(e)[:40]}")
        
        if (i + 1) % 5 == 0 or (i + 1) == len(accounts_to_use):
            try:
                await status_msg.edit_text(f"🚀 CAMPAIGN RUNNING...\n\nProgress: {i+1}/{len(accounts_to_use)}\n✅ Success: {results['success']}\n❌ Failed: {results['failed']}")
            except:
                pass
        
        delay = load_settings(user_id).get('delay', DEFAULT_DELAY)
        await asyncio.sleep(delay)
    
    action_names = {
        'join': 'Join Channel',
        'react': 'React Only',
        'different_react': 'Different Reactions', 
        'view': 'View Only', 
        'vote': 'Vote Only', 
        'reactvote': 'React + Vote',
        'reactview': 'React + View', 
        'voteview': 'Vote + View',
        'reactvoteview': 'React+Vote+View', 
        'dm': 'Bulk DM',
        'vc': 'Voice Chat', 
        'group_spam': 'Group Spam'
    }
    
    campaign_data = {
        'action': action_names.get(action, action),
        'target': link,
        'result': f"Success {results['success']} / Failed {results['failed']}",
        'status': 'completed',
        'timestamp': str(datetime.now()),
        'accounts_used': len(accounts_to_use),
        'success_count': results['success'],
        'failed_count': results['failed']
    }
    save_campaign(user_id, campaign_data)
    
    errors_text = ""
    if results['errors']:
        errors_list = results['errors'][:5]
        errors_text = "\n\n❌ Errors:\n" + "\n".join(errors_list)
    
    success_rate = int((results['success'] * 100) / len(accounts_to_use)) if len(accounts_to_use) > 0 else 0
    
    await status_msg.edit_text(f"✅ CAMPAIGN FINISHED!\n\nTotal: {len(accounts_to_use)}\n✅ Success: {results['success']}\n❌ Failed: {results['failed']}\n📊 Rate: {success_rate}%{errors_text}")
    
    del PENDING_CAMPAIGN[user_id]
    SELECTED_ACCOUNT_COUNT.pop(user_id, None)
    PENDING_SPAM_MSG.pop(user_id, None)

# ========== SCHEDULED ==========
async def scheduled_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    scheduled_list = load_scheduled(user_id)
    
    if not scheduled_list:
        keyboard = [
            [InlineKeyboardButton("📅 Schedule New", callback_data="schedule_new")],
            [InlineKeyboardButton("🔙 Back", callback_data="main")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.callback_query.edit_message_text("⏰ SCHEDULED\n\nNo scheduled campaigns!", reply_markup=reply_markup)
    else:
        text = "⏰ YOUR SCHEDULES\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for s in scheduled_list:
            schedule_id, action, target, schedule_time, account_count, spam_msg = s
            text += f"ID: {schedule_id}\nAction: {action}\nTarget: {target[:40]}\nTime: {schedule_time}\nAccounts: {account_count}\n━━━━━━━━━━━━━━━━━━━━━━\n"
        
        keyboard = [
            [InlineKeyboardButton("📅 Schedule New", callback_data="schedule_new")],
            [InlineKeyboardButton("❌ Cancel Schedule", callback_data="schedule_cancel")],
            [InlineKeyboardButton("🔙 Back", callback_data="main")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.callback_query.edit_message_text(text[:4000], reply_markup=reply_markup)

async def schedule_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    accs = get_accessible_accounts(user_id)
    
    if not accs:
        await update.callback_query.edit_message_text("❌ No accounts!")
        return
    
    live_accs = []
    for a in accs:
        if await is_account_live(a):
            live_accs.append(a)
    
    if not live_accs:
        await update.callback_query.edit_message_text("❌ No active accounts!")
        return
    
    context.user_data['campaign_accounts'] = live_accs
    WAITING_FOR[user_id] = 'schedule_action'
    
    keyboard = [
        [InlineKeyboardButton("📢 Join", callback_data="schedule_action_join")],
        [InlineKeyboardButton("❤️ React", callback_data="schedule_action_react")],
        [InlineKeyboardButton("🎲 Random React", callback_data="schedule_action_different_react")],
        [InlineKeyboardButton("👁️ View", callback_data="schedule_action_view")],
        [InlineKeyboardButton("🗳️ Vote", callback_data="schedule_action_vote")],
        [InlineKeyboardButton("🔊 VC", callback_data="schedule_action_vc")],
        [InlineKeyboardButton("💬 DM", callback_data="schedule_action_dm")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text("📅 SCHEDULE\n\nSelect action:", reply_markup=reply_markup)

async def schedule_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
    user_id = update.callback_query.from_user.id
    context.user_data['schedule_action'] = action
    WAITING_FOR[user_id] = 'schedule_link'
    
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.callback_query.edit_message_text(f"Send the link for {action}:", reply_markup=reply_markup)

async def handle_schedule_link(update: Update, context: ContextTypes.DEFAULT_TYPE, link: str):
    user_id = update.effective_user.id
    action = context.user_data.get('schedule_action')
    
    if not action:
        await update.message.reply_text("❌ Start again!")
        WAITING_FOR.pop(user_id, None)
        return
    
    PENDING_SCHEDULE[user_id] = {'action': action, 'link': link}
    
    if action == 'dm':
        WAITING_FOR[user_id] = 'schedule_spam_message'
        await update.message.reply_text("📝 Send message to send:")
    else:
        WAITING_FOR[user_id] = 'schedule_time'
        await update.message.reply_text("⏰ Schedule Time\n\nFormat: YYYY-MM-DD HH:MM:SS\nExample: 2026-06-05 14:30:00\n\nTimezone: UTC")

async def handle_schedule_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE, msg: str):
    user_id = update.effective_user.id
    schedule_data = PENDING_SCHEDULE.get(user_id)
    
    if not schedule_data:
        await update.message.reply_text("❌ Data lost!")
        WAITING_FOR.pop(user_id, None)
        return
    
    PENDING_SCHEDULE[user_id]['spam_message'] = msg
    WAITING_FOR[user_id] = 'schedule_time'
    
    await update.message.reply_text("⏰ Schedule Time\n\nFormat: YYYY-MM-DD HH:MM:SS\nExample: 2026-06-05 14:30:00")

async def handle_schedule_time(update: Update, context: ContextTypes.DEFAULT_TYPE, time_str: str):
    user_id = update.effective_user.id
    schedule_data = PENDING_SCHEDULE.get(user_id)
    
    if not schedule_data:
        await update.message.reply_text("❌ Data lost!")
        WAITING_FOR.pop(user_id, None)
        return
    
    try:
        scheduled_time = datetime.strptime(time_str.strip(), "%Y-%m-%d %H:%M:%S")
        if scheduled_time <= datetime.now():
            await update.message.reply_text("❌ Send future date/time!")
            return
        
        PENDING_SCHEDULE[user_id]['time_str'] = time_str
        WAITING_FOR[user_id] = 'schedule_account_count'
        
        accounts = context.user_data.get('campaign_accounts', [])
        await update.message.reply_text(f"How many accounts? (1-{len(accounts)})")
        
    except ValueError:
        await update.message.reply_text("❌ Invalid format! Use: YYYY-MM-DD HH:MM:SS")

async def handle_schedule_account_count(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    schedule_data = PENDING_SCHEDULE.get(user_id)
    
    if not schedule_data:
        await update.message.reply_text("❌ Data lost!")
        WAITING_FOR.pop(user_id, None)
        return
    
    try:
        count = int(text.strip())
        accounts = context.user_data.get('campaign_accounts', [])
        
        if count < 1 or count > len(accounts):
            await update.message.reply_text(f"❌ Send number between 1 and {len(accounts)}")
            return
        
        spam_message = schedule_data.get('spam_message', '')
        save_scheduled(user_id, schedule_data['action'], schedule_data['link'], schedule_data['time_str'], count, spam_message)
        
        WAITING_FOR.pop(user_id, None)
        PENDING_SCHEDULE.pop(user_id, None)
        
        await update.message.reply_text(f"✅ Scheduled!\n\nAction: {schedule_data['action']}\nTime: {schedule_data['time_str']}\nAccounts: {count}\n\nWill run at scheduled time!")
        
    except ValueError:
        await update.message.reply_text("❌ Send valid number!")

async def schedule_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    scheduled_list = load_scheduled(user_id)
    
    if not scheduled_list:
        await update.callback_query.edit_message_text("❌ No schedules!")
        return
    
    WAITING_FOR[user_id] = 'cancel_schedule_id'
    
    text = "❌ Cancel Schedule\n\nSend Schedule ID:\n\n"
    for s in scheduled_list:
        text += f"ID: {s[0]} - {s[1]}\n"
    
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.callback_query.edit_message_text(text, reply_markup=reply_markup)

async def handle_cancel_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    try:
        schedule_id = int(text.strip())
        delete_scheduled(schedule_id)
        WAITING_FOR.pop(user_id, None)
        await update.message.reply_text(f"✅ Schedule {schedule_id} cancelled!")
    except:
        await update.message.reply_text("❌ Invalid ID!")
        WAITING_FOR.pop(user_id, None)

# ========== ADMIN PANEL ==========
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id):
        await update.callback_query.answer("❌ Only Owner!", show_alert=True)
        return
    
    keyboard = [
        [InlineKeyboardButton("📢 Campaign (All)", callback_data="admin_campaign_all")],
        [InlineKeyboardButton("👤 Campaign (By User)", callback_data="admin_campaign_user")],
        [InlineKeyboardButton("📁 All Campaigns", callback_data="admin_all_campaigns")],
        [InlineKeyboardButton("🚫 Ban User", callback_data="admin_ban_user")],
        [InlineKeyboardButton("✅ Unban User", callback_data="admin_unban_user")],
        [InlineKeyboardButton("👥 All Users", callback_data="admin_all_users")],
        [InlineKeyboardButton("🔙 Main", callback_data="main")],
    ]
    
    await update.callback_query.edit_message_text("👑 OWNER PANEL\n\nSelect option:", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id):
        await update.callback_query.answer("❌ Only Owner!", show_alert=True)
        return
    
    WAITING_FOR[user_id] = 'admin_ban_user_id'
    keyboard = [[InlineKeyboardButton("CANCEL", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text("🚫 Ban User\n\nSend User ID:", reply_markup=reply_markup)

async def admin_unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id):
        await update.callback_query.answer("❌ Only Owner!", show_alert=True)
        return
    
    WAITING_FOR[user_id] = 'admin_unban_user_id'
    keyboard = [[InlineKeyboardButton("CANCEL", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text("✅ Unban User\n\nSend User ID:", reply_markup=reply_markup)

async def admin_all_campaigns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id):
        await update.callback_query.answer("❌ Only Owner!", show_alert=True)
        return
    
    text = "📁 ALL CAMPAIGNS\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    campaign_count = 0
    for file in os.listdir("data"):
        if file.startswith("campaigns_"):
            with open(os.path.join("data", file), 'r') as f:
                try:
                    campaigns = json.load(f)
                    for c in campaigns[:5]:
                        campaign_count += 1
                        text += f"{campaign_count}. {c.get('action', 'Unknown')}\n   {c.get('target', '')[:40]}\n   {c.get('result', '')}\n   {c.get('timestamp', '')[:16]}\n\n"
                        if campaign_count >= 20:
                            text += "\n... and more"
                            break
                except:
                    pass
            if campaign_count >= 20:
                break
    
    if campaign_count == 0:
        text += "No campaigns!"
    
    keyboard = [[InlineKeyboardButton("BACK", callback_data="admin_panel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(text[:4000], reply_markup=reply_markup)

async def admin_all_users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id):
        await update.callback_query.answer("❌ Only Owner!", show_alert=True)
        return
    
    users = get_all_users()
    
    if not users:
        await update.callback_query.edit_message_text("📭 No users!")
        return
    
    text = "👥 ALL USERS\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    count = 0
    for u in users:
        if count >= 20:
            text += "\n... and more"
            break
        uid, username, first_name, joined_date, is_banned_user, access_expiry, shared_limit = u
        personal_accounts = load_accounts(uid)
        
        if uid == OWNER_ID:
            status = "👑 OWNER"
        elif is_banned_user:
            status = "🚫 BANNED"
        elif access_expiry:
            status = "✅ ACCESS"
        else:
            status = "👤 USER"
        
        text += f"{status}\n🆔 {uid}\n📛 {first_name or 'Unknown'}\n📝 @{username or 'No username'}\n📱 Accounts: {len(personal_accounts)}"
        if shared_limit:
            text += f"\n🔗 Shared: {shared_limit}"
        if access_expiry:
            exp_date = datetime.fromisoformat(access_expiry).strftime("%Y-%m-%d")
            text += f"\n⏰ Expiry: {exp_date}"
        text += "\n━━━━━━━━━━━━━━━━━━━━━━\n"
        count += 1
    
    keyboard = [[InlineKeyboardButton("BACK", callback_data="admin_panel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(text[:4000], reply_markup=reply_markup)

async def admin_campaign_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id):
        await update.callback_query.answer("❌ Only Owner!", show_alert=True)
        return
    
    all_accounts = load_owner_accounts()
    live_accounts = []
    for a in all_accounts:
        if await is_account_live(a):
            live_accounts.append(a)
    
    if not live_accounts:
        await update.callback_query.edit_message_text("❌ No active accounts!")
        return
    
    context.user_data['campaign_accounts'] = live_accounts
    
    keyboard = [
        [InlineKeyboardButton("😊 Normal Emoji", callback_data="campaign_normal_emoji")],
        [InlineKeyboardButton("✨ Premium Emoji", callback_data="campaign_premium_mode")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")],
    ]
    
    await update.callback_query.edit_message_text(f"👑 OWNER - CAMPAIGN ON ALL\n\nActive Accounts: {len(live_accounts)}\n\nSelect emoji mode:", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_campaign_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id):
        await update.callback_query.answer("❌ Only Owner!", show_alert=True)
        return
    
    WAITING_FOR[user_id] = 'admin_campaign_user_id'
    keyboard = [[InlineKeyboardButton("CANCEL", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text("👑 Campaign on User\n\nSend User ID:", reply_markup=reply_markup)

async def handle_admin_campaign_user(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only Owner!")
        WAITING_FOR.pop(user_id, None)
        return
    
    try:
        target_user = int(text.strip())
        accounts = get_accessible_accounts(target_user)
        live_accounts = []
        for a in accounts:
            if await is_account_live(a):
                live_accounts.append(a)
        
        if not live_accounts:
            await update.message.reply_text("❌ No active accounts for this user!")
            WAITING_FOR.pop(user_id, None)
            return
        
        context.user_data['campaign_accounts'] = live_accounts
        WAITING_FOR.pop(user_id, None)
        
        keyboard = [
            [InlineKeyboardButton("❤️ React", callback_data="campaign_action_react")],
            [InlineKeyboardButton("🎲 Random React", callback_data="campaign_action_different_react")],
            [InlineKeyboardButton("👁️ View", callback_data="private_channel_view")],
            [InlineKeyboardButton("🗳️ Vote", callback_data="campaign_action_vote")],
            [InlineKeyboardButton("📢 Join", callback_data="campaign_action_join")],
            [InlineKeyboardButton("🚪 Leave", callback_data="leave_channel_menu")],
            [InlineKeyboardButton("💬 DM", callback_data="campaign_action_dm")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")],
        ]
        
        await update.message.reply_text(f"👑 CAMPAIGN ON USER {target_user}\n\nActive Accounts: {len(live_accounts)}\n\nSelect action:", reply_markup=InlineKeyboardMarkup(keyboard))
    except:
        await update.message.reply_text("❌ Invalid User ID!")
        WAITING_FOR.pop(user_id, None)

async def handle_admin_ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only Owner!")
        WAITING_FOR.pop(user_id, None)
        return
    
    try:
        target = int(text.strip())
        if target == OWNER_ID:
            await update.message.reply_text("❌ Cannot ban owner!")
            WAITING_FOR.pop(user_id, None)
            return
        ban_user(target)
        WAITING_FOR.pop(user_id, None)
        await update.message.reply_text(f"✅ User {target} banned!")
    except:
        await update.message.reply_text("❌ Invalid User ID!")
        WAITING_FOR.pop(user_id, None)

async def handle_admin_unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only Owner!")
        WAITING_FOR.pop(user_id, None)
        return
    
    try:
        target = int(text.strip())
        unban_user(target)
        WAITING_FOR.pop(user_id, None)
        await update.message.reply_text(f"✅ User {target} unbanned!")
    except:
        await update.message.reply_text("❌ Invalid User ID!")
        WAITING_FOR.pop(user_id, None)

async def admin_view_live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id):
        await update.callback_query.answer("❌ Only Owner!", show_alert=True)
        return
    
    all_accounts = load_owner_accounts()
    live = []
    for a in all_accounts:
        if await is_account_live(a):
            live.append(a)
    
    text = f"📋 LIVE ACCOUNTS\n━━━━━━━━━━━━━━━━━━━━━━\n\nTotal: {len(live)}\n\n"
    for a in live[:10]:
        text += f"👤 @{a.get('username', 'No username')}\n📱 {a['phone']}\n\n"
    
    keyboard = [[InlineKeyboardButton("Back", callback_data="my_accounts")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(text, reply_markup=reply_markup)

async def admin_view_expired(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id):
        await update.callback_query.answer("❌ Only Owner!", show_alert=True)
        return
    
    all_accounts = load_owner_accounts()
    expired = []
    for a in all_accounts:
        if not await is_account_live(a):
            expired.append(a)
    
    text = f"📋 EXPIRED ACCOUNTS\n━━━━━━━━━━━━━━━━━━━━━━\n\nTotal: {len(expired)}\n\n"
    for a in expired[:10]:
        text += f"👤 @{a.get('username', 'No username')}\n📱 {a['phone']}\n\n"
    
    keyboard = [
        [InlineKeyboardButton("Remove All Expired", callback_data="admin_remove_all_expired")],
        [InlineKeyboardButton("Back", callback_data="my_accounts")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(text, reply_markup=reply_markup)

async def admin_remove_all_expired(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id):
        await update.callback_query.answer("❌ Only Owner!", show_alert=True)
        return
    
    all_accounts = load_owner_accounts()
    expired = [a for a in all_accounts if not await is_account_live(a)]
    expired_phones = [a.get('phone') for a in expired]
    new_accounts = [a for a in all_accounts if a.get('phone') not in expired_phones]
    save_accounts(OWNER_ID, new_accounts)
    
    users = get_all_users()
    for u in users:
        uid = u[0]
        if uid != OWNER_ID:
            user_accounts = load_accounts(uid)
            new_user_accounts = [a for a in user_accounts if a.get('phone') not in expired_phones]
            if len(user_accounts) != len(new_user_accounts):
                save_accounts(uid, new_user_accounts)
    
    await update.callback_query.edit_message_text(f"✅ Removed {len(expired)} expired accounts!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="my_accounts")]]))

async def admin_remove_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id):
        await update.callback_query.answer("❌ Only Owner!", show_alert=True)
        return
    
    WAITING_FOR[user_id] = 'admin_remove_phone'
    keyboard = [[InlineKeyboardButton("CANCEL", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text("🗑️ Remove Account\n\nSend phone number:\nExample: +919876543210", reply_markup=reply_markup)

async def admin_remove_account(update: Update, context: ContextTypes.DEFAULT_TYPE, phone: str):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only Owner!")
        WAITING_FOR.pop(user_id, None)
        return
    
    owner_accounts = load_owner_accounts()
    found = any(a.get('phone') == phone for a in owner_accounts)
    if not found:
        await update.message.reply_text("❌ Account not found!")
        WAITING_FOR.pop(user_id, None)
        return
    
    new_owner_accounts = [a for a in owner_accounts if a.get('phone') != phone]
    save_accounts(OWNER_ID, new_owner_accounts)
    
    users = get_all_users()
    for u in users:
        uid = u[0]
        if uid != OWNER_ID:
            user_accounts = load_accounts(uid)
            new_user_accounts = [a for a in user_accounts if a.get('phone') != phone]
            if len(user_accounts) != len(new_user_accounts):
                save_accounts(uid, new_user_accounts)
    
    WAITING_FOR.pop(user_id, None)
    await update.message.reply_text(f"✅ Account {phone} removed!")

async def admin_remove_all_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id):
        await update.callback_query.answer("❌ Only Owner!", show_alert=True)
        return
    
    all_accounts = load_owner_accounts()
    keyboard = [
        [InlineKeyboardButton("YES, REMOVE ALL", callback_data="admin_remove_all_confirm")],
        [InlineKeyboardButton("CANCEL", callback_data="cancel_action")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(f"⚠️ WARNING!\n\nRemove ALL {len(all_accounts)} accounts?\n\nThis cannot be undone!", reply_markup=reply_markup)

async def admin_remove_all_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id):
        await update.callback_query.answer("❌ Only Owner!", show_alert=True)
        return
    
    save_accounts(OWNER_ID, [])
    users = get_all_users()
    for u in users:
        uid = u[0]
        if uid != OWNER_ID:
            save_accounts(uid, [])
    
    await update.callback_query.edit_message_text("✅ All accounts removed!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main", callback_data="main")]]))

async def user_remove_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    WAITING_FOR[user_id] = 'user_remove_phone'
    keyboard = [[InlineKeyboardButton("CANCEL", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text("🗑️ Remove Personal Account\n\nSend phone number to remove:", reply_markup=reply_markup)

async def user_remove_account(update: Update, context: ContextTypes.DEFAULT_TYPE, phone: str):
    user_id = update.effective_user.id
    accounts = load_accounts(user_id)
    found = any(a.get('phone') == phone for a in accounts)
    if not found:
        await update.message.reply_text("❌ Account not found in your personal accounts!")
        WAITING_FOR.pop(user_id, None)
        return
    
    new_accounts = [a for a in accounts if a.get('phone') != phone]
    save_accounts(user_id, new_accounts)
    WAITING_FOR.pop(user_id, None)
    await update.message.reply_text(f"✅ Account {phone} removed from your personal accounts!")

# ========== NORMAL EMOJI MODE ==========
async def campaign_normal_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    
    keyboard = []
    row = []
    selected = SELECTED_EMOJIS.get(user_id, [])
    
    for i, emoji in enumerate(NORMAL_EMOJIS):
        btn_text = f"✅ {emoji}" if emoji in selected else emoji
        row.append(InlineKeyboardButton(btn_text, callback_data=f"select_emoji_{emoji}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("✅ Ready - Start", callback_data="emoji_ready")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")])
    
    text = f"""😊 NORMAL EMOJI MODE

Select one or more emojis.
Accounts will be split across selections.

Selected: {', '.join(selected) if selected else 'None'}"""
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(text, reply_markup=reply_markup)

async def select_emoji_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    emoji = query.data.replace("select_emoji_", "")
    
    selected = SELECTED_EMOJIS.get(user_id, [])
    if emoji in selected:
        selected.remove(emoji)
    else:
        selected.append(emoji)
    
    SELECTED_EMOJIS[user_id] = selected
    
    keyboard = []
    row = []
    for i, e in enumerate(NORMAL_EMOJIS):
        btn_text = f"✅ {e}" if e in selected else e
        row.append(InlineKeyboardButton(btn_text, callback_data=f"select_emoji_{e}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("✅ Ready - Start", callback_data="emoji_ready")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")])
    
    text = f"""😊 NORMAL EMOJI MODE

Select one or more emojis.
Accounts will be split across selections.

Selected: {', '.join(selected) if selected else 'None'}"""
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except:
        pass

async def emoji_ready_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    selected = SELECTED_EMOJIS.get(user_id, [])
    
    if not selected:
        await update.callback_query.answer("Select at least one emoji!", show_alert=True)
        return
    
    context.user_data['campaign_action'] = 'multi_react'
    context.user_data['selected_emojis'] = selected
    WAITING_FOR[user_id] = 'campaign_link'
    
    account_count = len(context.user_data.get('campaign_accounts', []))
    
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.callback_query.edit_message_text(f"📢 MULTIPLE REACTIONS ({len(selected)} emojis)\n\nAccounts: {account_count}\nSplit: ~{account_count // max(1, len(selected))} per emoji\n\nSelected: {', '.join(selected)}\n\nSend post link:", reply_markup=reply_markup)

# ========== PREMIUM EMOJI MODE ==========
async def campaign_premium_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    
    keyboard = []
    row = []
    for name, emoji_id in PREMIUM_EMOJIS.items():
        row.append(InlineKeyboardButton(f"✨ {name}", callback_data=f"select_premium_{name}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")])
    
    text = """✨ PREMIUM EMOJI MODE

INSTRUCTIONS:
1. Manually react with premium emoji ONCE
2. Select same emoji below
3. Bot will react with same emoji

Select premium emoji:"""
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(text, reply_markup=reply_markup)

async def select_premium_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    emoji_name = query.data.replace("select_premium_", "")
    emoji_id = PREMIUM_EMOJIS.get(emoji_name)
    
    if not emoji_id:
        await query.answer("Invalid emoji!", show_alert=True)
        return
    
    SELECTED_PREMIUM[user_id] = {'name': emoji_name, 'id': emoji_id}
    context.user_data['campaign_action'] = 'premium_emoji'
    WAITING_FOR[user_id] = 'campaign_link'
    
    account_count = len(context.user_data.get('campaign_accounts', []))
    
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(f"✨ PREMIUM EMOJI\n\nSelected: {emoji_name}\nAccounts: {account_count}\n\nSend post link:", reply_markup=reply_markup)

# ========== MY PURCHASED ==========
async def my_campaigns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    campaigns = load_campaigns(user_id)
    if not campaigns:
        await update.callback_query.edit_message_text("📁 No purchases yet!\n\nRun campaigns from Shopping.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 MAIN", callback_data="main")]]))
        return
    
    text = "📁 MY PURCHASED\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for i, c in enumerate(campaigns[:10], 1):
        text += f"{i}. {c['action']}\n   Target: {c['target'][:40]}\n   Result: {c['result']}\n   Time: {c['timestamp'].split('.')[0]}\n\n"
    
    await update.callback_query.edit_message_text(text[:4000], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 MAIN", callback_data="main")]]))

async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    personal_accounts = load_accounts(user_id)
    personal_active = 0
    for a in personal_accounts:
        if await is_account_live(a):
            personal_active += 1
    
    shared_limit = get_user_shared_limit(user_id)
    total_available = len(get_accessible_accounts(user_id))
    campaigns = load_campaigns(user_id)
    
    text = f"📊 YOUR STATS\n━━━━━━━━━━━━━━━━━━━━━━\n\n📱 Personal: {len(personal_accounts)} (🟢{personal_active})\n🔗 Shared Limit: {shared_limit}\n📊 Total Available: {total_available}\n📁 Purchased: {len(campaigns)}"
    
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 MAIN", callback_data="main")]]))

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    settings = load_settings(user_id)
    current_delay = settings.get('delay', DEFAULT_DELAY)
    
    keyboard = [
        [InlineKeyboardButton("0.5s", callback_data="delay_0.5"), InlineKeyboardButton("1s", callback_data="delay_1.0")],
        [InlineKeyboardButton("1.5s", callback_data="delay_1.5"), InlineKeyboardButton("2s", callback_data="delay_2.0")],
        [InlineKeyboardButton("Custom", callback_data="custom_delay")],
        [InlineKeyboardButton("CANCEL", callback_data="cancel_action")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(f"⚙️ SETTINGS\nDelay: {current_delay}s", reply_markup=reply_markup)

async def set_delay(update: Update, context: ContextTypes.DEFAULT_TYPE, delay: str):
    user_id = update.callback_query.from_user.id
    settings = load_settings(user_id)
    settings['delay'] = float(delay)
    save_settings(user_id, settings)
    
    keyboard = [[InlineKeyboardButton("BACK", callback_data="settings")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(f"✅ Delay set to {delay}s", reply_markup=reply_markup)

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    conn = sqlite3.connect('automation_bot.db')
    c = conn.cursor()
    c.execute("SELECT first_name, username, joined_date, access_expiry, shared_id_limit FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    
    first_name = result[0] if result else 'Unknown'
    username = result[1] if result else 'Unknown'
    joined_date = result[2] if result else 'Unknown'
    access_expiry = result[3] if result else None
    shared_limit = result[4] if result else 0
    
    personal_accounts = load_accounts(user_id)
    campaigns = load_campaigns(user_id)
    
    admin_badge = " 👑 OWNER" if user_id == OWNER_ID else ""
    
    text = f"👤 PROFILE{admin_badge}\n━━━━━━━━━━━━━━━━━━━━━━\n\n🆔 {user_id}\n👤 {first_name}\n📝 @{username}\n📅 {joined_date[:10] if joined_date else 'Unknown'}\n📱 Personal: {len(personal_accounts)}\n🔗 Shared Limit: {shared_limit}\n📁 Purchased: {len(campaigns)}"
    
    if access_expiry and user_id != OWNER_ID:
        exp_date = datetime.fromisoformat(access_expiry).strftime("%Y-%m-%d")
        text += f"\n⏰ Access until: {exp_date}"
    
    keyboard = [[InlineKeyboardButton("BACK", callback_data="main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(text, reply_markup=reply_markup)

# ========== HELP & GUIDE ==========
async def help_guide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """🤖 HOW TO USE
━━━━━━━━━━━━━━━━━━━━━━

📌 ADD ACCOUNTS
Phone + OTP / Session String / Bulk Sessions

📌 SHOPPING (CAMPAIGNS)
1. Click 'Shopping'
2. Select action
3. Send link
4. Choose account count
5. Tap 'Run'

📌 ACTIONS (ALL WORKING ✅)
• ❤️ React - 🔥 reaction
• 🎲 Different Reactions - Random emoji each account
• 👁️ View - Increase view count (REAL VIEWS)
• 🗳️ Vote - Click poll button
• 📢 Join - Join channel
• 🚪 Leave Channel - Leave specific channel
• 🗑️ LEAVE ALL - Leave all channels
• 💬 Bulk DM - Send messages
• 🔊 VC - Join voice chat (mic off)
• 📢 Group Spam - Spam in groups

📌 SUPPORTED LINKS
• Public Post: t.me/username/123
• Private Post: t.me/c/123456789/123
• Private Invite: t.me/joinchat/xxxxx
• Channel Join: t.me/username or invite link

📌 PREMIUM EMOJIS
13+ Custom premium emojis available

📌 ACCESS
Contact @SHIVAMKR_208

👨‍💻 Support: @AUTO_BOTS_INFO"""
    
    keyboard = [[InlineKeyboardButton("BACK", callback_data="main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)

# ========== SUPPORT ==========
async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """📞 SUPPORT & CONTACT
━━━━━━━━━━━━━━━━━━━━━━

👨‍💻 Channel: @AUTO_BOTS_INFO
📞 Access/Support: @SHIVAMKR_208
🔧 Bot Owner: @SHIVAMKR_208

For issues, bugs, or access requests, contact above."""
    
    keyboard = [[InlineKeyboardButton("BACK", callback_data="main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)

async def custom_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    WAITING_FOR[user_id] = 'custom_delay'
    keyboard = [[InlineKeyboardButton("CANCEL", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text("Send delay in seconds (example: 0.8):", reply_markup=reply_markup)

async def handle_custom_delay(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    try:
        delay = float(text)
        if delay <= 0:
            raise ValueError
        settings = load_settings(user_id)
        settings['delay'] = delay
        save_settings(user_id, settings)
        WAITING_FOR.pop(user_id, None)
        await update.message.reply_text(f"✅ Delay set to {delay}s")
    except:
        await update.message.reply_text("❌ Invalid value!")
        WAITING_FOR.pop(user_id, None)

# ========== MESSAGE HANDLER ==========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    
    user_id = update.effective_user.id
    text = update.message.text.strip()
    state = WAITING_FOR.get(user_id)
    
    if state == 'phone':
        await handle_phone(update, context, text)
    elif state == 'session_string':
        await handle_session_string(update, context, text)
    elif state == 'bulk_sessions':
        await handle_bulk_sessions(update, context, text)
    elif state == 'campaign_link':
        await handle_campaign_link(update, context, text)
    elif state == 'spam_message':
        await handle_spam_message(update, context, text)
    elif state == 'account_count':
        await handle_account_count(update, context, text)
    elif state == 'custom_delay':
        await handle_custom_delay(update, context, text)
    elif state == 'admin_remove_phone':
        await admin_remove_account(update, context, text)
    elif state == 'user_remove_phone':
        await user_remove_account(update, context, text)
    elif state == 'admin_campaign_user_id':
        await handle_admin_campaign_user(update, context, text)
    elif state == 'admin_ban_user_id':
        await handle_admin_ban_user(update, context, text)
    elif state == 'admin_unban_user_id':
        await handle_admin_unban_user(update, context, text)
    elif state == 'access_user_id':
        await handle_access_user_id(update, context, text)
    elif state == 'access_days':
        await handle_access_days(update, context, text)
    elif state == 'access_shared_limit':
        await handle_access_shared_limit(update, context, text)
    elif state == 'remove_access_user_id':
        await handle_remove_access(update, context, text)
    elif state == 'schedule_link':
        await handle_schedule_link(update, context, text)
    elif state == 'schedule_spam_message':
        await handle_schedule_spam_message(update, context, text)
    elif state == 'schedule_time':
        await handle_schedule_time(update, context, text)
    elif state == 'schedule_account_count':
        await handle_schedule_account_count(update, context, text)
    elif state == 'cancel_schedule_id':
        await handle_cancel_schedule(update, context, text)
    elif state == 'private_view_link':
        await handle_private_view(update, context, text)
    elif state == 'leave_specific_link':
        await handle_leave_specific(update, context, text)
    elif state == 'schedule_action':
        pass
    elif text.isdigit() and len(text) == 5 and user_id in PENDING_OTP:
        await verify_otp(update, context, text)
    elif user_id in PENDING_2FA:
        await verify_2fa(update, context, text)
    else:
        await update.message.reply_text("❌ Send /start")

# ========== CALLBACK HANDLER ==========
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except:
        pass
    
    data = query.data
    
    if data == "main":
        await show_main_menu(update, context)
    elif data == "add_account":
        await add_account_menu(update, context)
    elif data == "add_phone_otp":
        await add_phone_otp(update, context)
    elif data == "add_session_string":
        await add_session_string(update, context)
    elif data == "add_bulk_sessions":
        await add_bulk_sessions(update, context)
    elif data == "my_accounts":
        await my_accounts(update, context)
    elif data == "new_campaign":
        await new_campaign(update, context)
    elif data == "private_channel_view":
        await private_channel_view(update, context)
    elif data == "leave_channel_menu":
        await leave_channel_menu(update, context)
    elif data == "leave_specific_channel":
        await leave_specific_channel(update, context)
    elif data == "leave_all_channels":
        await leave_all_channels(update, context)
    elif data == "confirm_leave_all":
        await confirm_leave_all(update, context)
    elif data == "scheduled":
        await scheduled_menu(update, context)
    elif data == "schedule_new":
        await schedule_new(update, context)
    elif data == "schedule_cancel":
        await schedule_cancel(update, context)
    elif data.startswith("schedule_action_"):
        action = data.replace("schedule_action_", "")
        await schedule_action_handler(update, context, action)
    elif data.startswith("campaign_action_"):
        action = data.replace("campaign_action_", "")
        await campaign_action_handler(update, context, action)
    elif data == "campaign_normal_emoji":
        await campaign_normal_emoji(update, context)
    elif data == "campaign_premium_mode":
        await campaign_premium_mode(update, context)
    elif data.startswith("select_emoji_"):
        await select_emoji_callback(update, context)
    elif data == "emoji_ready":
        await emoji_ready_callback(update, context)
    elif data.startswith("select_premium_"):
        await select_premium_emoji(update, context)
    elif data.startswith("use_all_"):
        await use_all_callback(update, context)
    elif data == "run_campaign":
        await run_campaign(update, context)
    elif data == "my_campaigns":
        await my_campaigns(update, context)
    elif data == "my_stats":
        await my_stats(update, context)
    elif data == "settings":
        await settings_menu(update, context)
    elif data == "profile":
        await profile(update, context)
    elif data == "help":
        await help_guide(update, context)
    elif data == "support":
        await support(update, context)
    elif data.startswith("delay_"):
        await set_delay(update, context, data.replace("delay_", ""))
    elif data == "custom_delay":
        await custom_delay(update, context)
    elif data == "admin_panel":
        await admin_panel(update, context)
    elif data == "admin_campaign_all":
        await admin_campaign_all(update, context)
    elif data == "admin_campaign_user":
        await admin_campaign_user(update, context)
    elif data == "admin_ban_user":
        await admin_ban_user(update, context)
    elif data == "admin_unban_user":
        await admin_unban_user(update, context)
    elif data == "admin_all_campaigns":
        await admin_all_campaigns(update, context)
    elif data == "admin_all_users":
        await admin_all_users_list(update, context)
    elif data == "admin_view_live":
        await admin_view_live(update, context)
    elif data == "admin_view_expired":
        await admin_view_expired(update, context)
    elif data == "admin_remove_prompt":
        await admin_remove_prompt(update, context)
    elif data == "admin_remove_all_prompt":
        await admin_remove_all_prompt(update, context)
    elif data == "admin_remove_all_confirm":
        await admin_remove_all_confirm(update, context)
    elif data == "admin_remove_all_expired":
        await admin_remove_all_expired(update, context)
    elif data == "user_remove_prompt":
        await user_remove_prompt(update, context)
    elif data == "give_access":
        await give_access_start(update, context)
    elif data == "remove_access":
        await remove_access_start(update, context)
    elif data == "access_more_yes":
        await access_more_yes(update, context)
    elif data == "access_more_no_direct":
        await access_more_no_direct(update, context)
    elif data == "cancel_action":
        await cancel_button_handler(update, context)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    error_str = str(error)
    harmless = ["Message is not modified", "Query is too old", "query id is invalid", "Conflict", "GeneratorExit", "Task was destroyed", "coroutine ignored"]
    for h in harmless:
        if h in error_str:
            return
    print(f"Error: {error}")

async def main():
    print("=" * 50)
    print("🤖 AUTOMATION VOTE BOT STARTED!")
    print("=" * 50)
    print(f"✅ Owner ID: {OWNER_ID}")
    print("=" * 50)
    print("✅ ALL ACTIONS WORKING (PUBLIC + PRIVATE):")
    print("   - Join Channel ✅")
    print("   - Leave Specific Channel ✅")
    print("   - LEAVE ALL Channels ✅")
    print("   - React Only | Different Reactions ✅")
    print("   - View Only (GetMessagesViewsRequest) ✅")
    print("   - PRIVATE CHANNEL VIEW (Auto-join + View) ✅")
    print("   - Vote Only | React + Vote | React + View ✅")
    print("   - Vote + View | React + Vote + View ✅")
    print("   - Bulk DM | VC (Voice Chat) ✅")
    print("   - Group Spam ✅")
    print("=" * 50)
    print("✅ PRIVATE CHANNEL VIEW: Supports invite links and message links")
    print("✅ PUBLIC CHANNEL VIEW: t.me/username/123")
    print("=" * 50)
    
    init_db()
    os.makedirs("sessions", exist_ok=True)
    os.makedirs("data", exist_ok=True)
    
    asyncio.create_task(check_scheduled_campaigns())
    
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped")
