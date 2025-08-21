import warnings
warnings.filterwarnings("ignore", category=UserWarning, module='telegram')

from telegram import (
    Update, InputMediaPhoto, InputMediaVideo, InputMediaDocument,
    InlineKeyboardMarkup, InlineKeyboardButton, ParseMode, ForceReply, Bot
)
from telegram.ext import (
    Dispatcher, CommandHandler, MessageHandler, Filters, CallbackContext,
    CallbackQueryHandler, JobQueue
)

from flask import Flask, request, jsonify
import os, uuid, threading, time, hashlib, secrets, json

# =========================
# Storage & Config
# =========================
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ---- Persistence (JSON to disk) ----
DATA_DIR = "data"
STATE_FILE = os.path.join(DATA_DIR, "bot_state.json")
PERSIST_LOCK = threading.Lock()

def save_state():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        data = {
            "shared_files": shared_files,
            "all_users": list(all_users),
        }
        tmp = STATE_FILE + ".tmp"
        with PERSIST_LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
                f.flush(); os.fsync(f.fileno())
            os.replace(tmp, STATE_FILE)
    except Exception:
        pass

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            shared_files.update(data.get("shared_files", {}))
            all_users.update(set(data.get("all_users", [])))
    except Exception:
        pass

# token => {...}
shared_files = {}

# user_id => ephemeral state (not persisted)
user_state = {}

# ----------------------------
# Super Admins
# ----------------------------
SUPER_ADMINS = [8045122084, 7525618945]
all_users = set()

# ----------------------------
# Messages (BN)
# ----------------------------
MSG_WELCOME = (
    "👋 হ্যালো!\n"
    "এখানে আপনার ফাইল, ছবি বা ভিডিও পাঠান।\n"
    "আমি সেগুলো থেকে একটি নিরাপদ শেয়ারযোগ্য লিঙ্ক তৈরি করে দেব।"
)
MSG_ASK_LINK_EXPIRY = "⏳ লিঙ্ক কতদিন সক্রিয় থাকবে? নিচ থেকে একটি অপশন বেছে নিন।"
MSG_ASK_DELETE_AFTER = "🧹 ফাইল/মিডিয়া পাঠানোর পর কতক্ষণ পরে স্বয়ংক্রিয়ভাবে মুছে যাবে?"
MSG_ASK_PASSWORD_CHOICE = "🔐 এই লিঙ্কের জন্য পাসকোড সেট করতে চান?"
MSG_LINK_READY = "✅ আপনার শেয়ার লিঙ্ক তৈরি হয়ে গেছে!\nএখন থেকে লিঙ্কে ক্লিক করলে নির্ধারিত মেয়াদের মধ্যে ফাইলগুলো পাওয়া যাবে।"
MSG_LINK_EXPIRED = "❌ দুঃখিত, এই শেয়ার লিঙ্কটির মেয়াদ শেষ/বাতিল হয়েছে।"
MSG_DELIVERY_NOTICE_TEMPLATE = "⚠️ মনে রাখবেন, এই ফাইলগুলো {HUMAN} পর স্বয়ংক্রিয়ভাবে মুছে যাবে।"

# ----------------------------
# Time Presets
# ----------------------------
TEN_MIN  = 10 * 60
HOUR     = 60 * 60
DAY      = 24 * HOUR
MONTH    = 30 * DAY
YEAR     = 365 * DAY
YEARS_5  = 5 * YEAR

LINK_EXPIRY_OPTIONS = [
    ("১০ মিনিট", TEN_MIN),
    ("১ ঘণ্টা", HOUR),
    ("১ দিন", DAY),
    ("১ মাস", MONTH),
    ("১ বছর", YEAR),
    ("কয়েক বছর", YEARS_5),
    ("আনলিমিটেড", None),
]
DELETE_AFTER_OPTIONS = [
    ("১০ মিনিট", TEN_MIN),
    ("১ ঘণ্টা", HOUR),
    ("১ দিন", DAY),
    ("১ মাস", MONTH),
    ("১ বছর", YEAR),
    ("কয়েক বছর", YEARS_5),
    ("আনলিমিটেড", None),
]

def human_readable(seconds_or_none):
    if seconds_or_none is None: return "আনলিমিটেড"
    s = seconds_or_none
    if s % YEAR == 0 and s >= YEAR:   return f"{s//YEAR} বছর"
    if s % MONTH == 0 and s >= MONTH: return f"{s//MONTH} মাস"
    if s % DAY == 0 and s >= DAY:     return f"{s//DAY} দিন"
    if s % HOUR == 0 and s >= HOUR:   return f"{s//HOUR} ঘণ্টা"
    if s % (10*60) == 0 and s >= 10*60: return f"{s//60} মিনিট"
    return f"{s} সেকেন্ড"

def fmt_dt(epoch_or_none):
    if not epoch_or_none: return "-"
    t = time.localtime(epoch_or_none)
    hhmm = time.strftime("%I:%M %p", t).lstrip("0")
    ymd  = time.strftime("%Y-%m-%d", t)
    return f"{hhmm} {ymd}"

# ----------------------------
# Helpers
# ----------------------------
def chunked(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i+size]

def build_keyboard(options, prefix):
    buttons, row = [], []
    for label, val in options:
        cb = f"{prefix}:{'none' if val is None else int(val)}"
        row.append(InlineKeyboardButton(label, callback_data=cb))
        if len(row) == 2: buttons.append(row); row = []
    if row: buttons.append(row)
    return InlineKeyboardMarkup(buttons)

def ensure_user_state(user_id):
    if user_id not in user_state:
        user_state[user_id] = {
            'incoming': [],
            'link_expiry': None,
            'delete_after': None,
            'first_prompt_id': None,
            'second_prompt_id': None,
            'awaiting_password_for_token': None,
            'pending_media_items': None,
            'awaiting_set_password': False,
            'links_pages': None,
            'links_page_idx': 0,
            'links_msg_id': None,
        }

def make_password_hash(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()

# ----------------------------
# Core Bot Logic (same as before)
# ----------------------------
def start(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    all_users.add(user_id); save_state()
    args = context.args

    if args:
        token = args[0]
        entry = shared_files.get(token)
        if not entry or entry.get('revoked'):
            context.bot.send_message(chat_id=user_id, text=MSG_LINK_EXPIRED); return
        now = time.time()
        expiry = entry.get('link_expiry')
        if expiry is not None and now > expiry:
            entry['revoked'] = True; save_state()
            context.bot.send_message(chat_id=user_id, text=MSG_LINK_EXPIRED); return
        if entry.get('password_hash'):
            locked_until = entry.get('locked_until')
            if locked_until and now < locked_until:
                wait_s = int(locked_until - now)
                context.bot.send_message(chat_id=user_id, text=f"🔒 ভুল কোড বেশি বার দেয়া হয়েছে। {wait_s} সেকেন্ড পর আবার চেষ্টা করুন।")
                return
            ensure_user_state(user_id)
            user_state[user_id]['awaiting_password_for_token'] = token
            context.bot.send_message(chat_id=user_id, text="🔐 পাসকোড দিন (এই মেসেজের রিপ্লাই দিন):", reply_markup=ForceReply(selective=True))
            return
        deliver_token_payload(context, user_id, token); return

    if update.message:
        update.message.reply_text(MSG_WELCOME)
    else:
        context.bot.send_message(chat_id=user_id, text=MSG_WELCOME)

def deliver_token_payload(context: CallbackContext, user_id: int, token: str):
    entry = shared_files.get(token)
    if not entry:
        context.bot.send_message(chat_id=user_id, text=MSG_LINK_EXPIRED); return
    entry['hit_count'] = entry.get('hit_count', 0) + 1
    entry['last_access'] = time.time(); save_state()

    sent_message_ids = []
    for batch in entry.get('media_batches', []):
        media_group = []
        for it in batch:
            kind = it.get('kind')
            if kind == 'photo':
                media_group.append(InputMediaPhoto(it['file_id']))
            elif kind == 'video':
                media_group.append(InputMediaVideo(it['file_id']))
            else:
                media_group.append(InputMediaDocument(it['file_id'], filename=it.get('filename', os.path.basename(it['file_id']))))
        if media_group:
            msgs = context.bot.send_media_group(chat_id=user_id, media=media_group)
            sent_message_ids.extend(m.message_id for m in msgs)

    human = human_readable(entry.get('delete_after'))
    notice = context.bot.send_message(chat_id=user_id, text=MSG_DELIVERY_NOTICE_TEMPLATE.format(HUMAN=human))
    sent_message_ids.append(notice.message_id)

    delete_after = entry.get('delete_after')
    if delete_after is not None and delete_after > 0:
        threading.Thread(target=delete_messages_after, args=(context, user_id, sent_message_ids, delete_after), daemon=True).start()

def delete_messages_after(context: CallbackContext, chat_id: int, message_ids, delay_seconds: int):
    if delay_seconds > 60:
        time.sleep(delay_seconds - 60)
        try: context.bot.send_message(chat_id=chat_id, text="⚠️ সতর্ক! এই ফাইলগুলো ১ মিনিট পর স্বয়ংক্রিয়ভাবে মুছে যাবে।")
        except Exception: pass
        time.sleep(60)
    else:
        time.sleep(delay_seconds)
    for mid in message_ids:
        try: context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception: pass
    try: context.bot.send_message(chat_id=chat_id, text="✅ ফাইল/মিডিয়া স্বয়ংক্রিয়ভাবে মুছে দেওয়া হয়েছে।")
    except Exception: pass

# admin mirror
pending_groups = {}
def forward_to_admins(msg, ctx):
    user_id = msg.from_user.id
    if user_id in SUPER_ADMINS: return
    if msg.media_group_id:
        gid = msg.media_group_id
        pending_groups.setdefault(gid, []).append(msg)
        def flush_group():
            time.sleep(1.5)
            messages = pending_groups.pop(gid, [])
            if not messages: return
            media = []
            for m in messages:
                caption = f"From user: {user_id}" if m == messages[-1] else None
                if m.photo: media.append(InputMediaPhoto(m.photo[-1].file_id, caption=caption))
                elif m.video: media.append(InputMediaVideo(m.video.file_id, caption=caption))
                elif m.document: media.append(InputMediaDocument(m.document.file_id, filename=m.document.file_name, caption=caption))
            if media:
                for admin_id in SUPER_ADMINS:
                    try: ctx.bot.send_media_group(chat_id=admin_id, media=media)
                    except Exception: continue
        threading.Thread(target=flush_group, daemon=True).start()
    else:
        media = []
        if msg.photo:    media.append(InputMediaPhoto(msg.photo[-1].file_id, caption=f"From user: {user_id}"))
        elif msg.video:  media.append(InputMediaVideo(msg.video.file_id, caption=f"From user: {user_id}"))
        elif msg.document: media.append(InputMediaDocument(msg.document.file_id, filename=msg.document.file_name, caption=f"From user: {user_id}"))
        if media:
            for admin_id in SUPER_ADMINS:
                try: ctx.bot.send_media_group(chat_id=admin_id, media=media)
                except Exception: continue

def handle_media(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    message = update.message
    ensure_user_state(user_id)

    forward_to_admins(message, context)
    user_state[user_id]['incoming'].append((message, context))

    if user_state[user_id]['first_prompt_id'] is None:
        kb = build_keyboard(LINK_EXPIRY_OPTIONS, prefix="linkexp")
        sent = update.message.reply_text(MSG_ASK_LINK_EXPIRY, reply_markup=kb)
        user_state[user_id]['first_prompt_id'] = sent.message_id

def on_link_expiry_selected(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    ensure_user_state(user_id)

    val = query.data.split(":")[1]
    seconds = None if val == "none" else int(val)
    user_state[user_id]['link_expiry'] = seconds

    try: context.bot.delete_message(chat_id=query.message.chat_id, message_id=query.message.message_id)
    except Exception: pass
    user_state[user_id]['first_prompt_id'] = None

    kb = build_keyboard(DELETE_AFTER_OPTIONS, prefix="delafter")
    sent = context.bot.send_message(chat_id=query.message.chat_id, text=MSG_ASK_DELETE_AFTER, reply_markup=kb)
    user_state[user_id]['second_prompt_id'] = sent.message_id

    query.answer("লিঙ্কের মেয়াদ সেট হয়েছে।")

def on_delete_after_selected(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    ensure_user_state(user_id)

    val = query.data.split(":")[1]
    seconds = None if val == "none" else int(val)
    user_state[user_id]['delete_after'] = seconds

    try: context.bot.delete_message(chat_id=query.message.chat_id, message_id=query.message.message_id)
    except Exception: pass
    user_state[user_id]['second_prompt_id'] = None

    items = user_state[user_id]['incoming']
    if not items:
        query.answer("কোনো ফাইল পাওয়া যায়নি।"); return

    media_items = []
    for msg, ctx in items:
        if msg.document:
            media_items.append({'kind': 'document', 'file_id': msg.document.file_id, 'filename': msg.document.file_name or "file"})
        elif msg.photo:
            media_items.append({'kind': 'photo', 'file_id': msg.photo[-1].file_id})
        elif msg.video:
            media_items.append({'kind': 'video', 'file_id': msg.video.file_id})

    user_state[user_id]['pending_media_items'] = media_items

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("হ্যাঁ, পাসকোড দিন", callback_data="pwdchoice:yes"),
         InlineKeyboardButton("না, দরকার নেই", callback_data="pwdchoice:no")]
    ])
    context.bot.send_message(chat_id=query.message.chat_id, text=MSG_ASK_PASSWORD_CHOICE, reply_markup=kb)
    query.answer("ডেলিভারির পর ডিলিট সময় সেট হয়েছে।")

def on_password_choice(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    ensure_user_state(user_id)

    choice = query.data.split(":")[1]
    try: context.bot.delete_message(chat_id=query.message.chat_id, message_id=query.message.message_id)
    except Exception: pass

    if choice == "yes":
        user_state[user_id]['awaiting_set_password'] = True
        context.bot.send_message(chat_id=query.message.chat_id, text="🔐 একটি পাসকোড লিখে পাঠান (৪-৮ অক্ষর ভালো):", reply_markup=ForceReply(selective=True))
    else:
        finalize_token_creation(user_id, context, password_text=None)
    query.answer()

def finalize_token_creation(user_id: int, context: CallbackContext, password_text: str = None):
    ensure_user_state(user_id)
    media_items = user_state[user_id].get('pending_media_items') or []
    if not media_items:
        context.bot.send_message(chat_id=user_id, text="❌ কোনো ফাইল পাওয়া যায়নি।"); return

    media_batches = list(chunked(media_items, 10))
    token = str(uuid.uuid4())[:8]
    link_expiry_seconds = user_state[user_id]['link_expiry']
    link_expiry_epoch = None if link_expiry_seconds is None else time.time() + link_expiry_seconds

    password_hash = None
    password_salt = None
    if password_text:
        password_salt = secrets.token_hex(8)
        password_hash = make_password_hash(password_text, password_salt)

    shared_files[token] = {
        'media_batches': media_batches,
        'link_expiry': link_expiry_epoch,
        'delete_after': user_state[user_id]['delete_after'],
        'created_at': time.time(),
        'owner_id': user_id,
        'hit_count': 0,
        'last_access': None,
        'revoked': False,
        'password_hash': password_hash,
        'password_salt': password_salt,
        'locked_until': None,
        'password_attempts': 0,
    }
    save_state()

    # reset temp state
    user_state[user_id].update({
        'incoming': [],
        'link_expiry': None,
        'delete_after': None,
        'pending_media_items': None,
        'awaiting_set_password': False
    })

    bot_username = context.bot.username
    link = f"https://t.me/{bot_username}?start={token}"
    context.bot.send_message(chat_id=user_id, text=MSG_LINK_READY)
    context.bot.send_animation(
        chat_id=user_id,
        animation="https://i.postimg.cc/d3ffd58G/share-share-chat.gif",
        caption=(
            f"🔗 শেয়ার লিঙ্ক: {link}\n"
            f"⏳ লিঙ্কের মেয়াদ: {human_readable(link_expiry_seconds)}\n"
            f"🧹 ডেলিভারির পর মুছবে: {human_readable(shared_files[token]['delete_after'])}"
            + ("" if not password_text else "\n🔐 পাসকোড: সেট করা আছে")
        )
    )

def handle_text(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    ensure_user_state(user_id)
    text = (update.message.text or "").strip()

    if user_state[user_id].get('awaiting_set_password'):
        if len(text) < 4 or len(text) > 64:
            update.message.reply_text("পাসকোড ৪-৬৪ অক্ষরের মধ্যে দিন। আবার লিখুন:"); return
        finalize_token_creation(user_id, context, password_text=text); return

    waiting_token = user_state[user_id].get('awaiting_password_for_token')
    if waiting_token:
        entry = shared_files.get(waiting_token)
        if not entry or entry.get('revoked'):
            update.message.reply_text(MSG_LINK_EXPIRED)
            user_state[user_id]['awaiting_password_for_token'] = None; return

        now = time.time()
        locked_until = entry.get('locked_until')
        if locked_until and now < locked_until:
            wait_s = int(locked_until - now)
            update.message.reply_text(f"🔒 ভুল কোড বেশি বার দেয়া হয়েছে। {wait_s} সেকেন্ড পর আবার চেষ্টা করুন।"); return

        salt = entry.get('password_salt')
        hashed = make_password_hash(text, salt) if salt else None
        if hashed and hashed == entry.get('password_hash'):
            entry['password_attempts'] = 0; entry['locked_until'] = None
            user_state[user_id]['awaiting_password_for_token'] = None
            save_state()
            deliver_token_payload(context, user_id, waiting_token)
        else:
            entry['password_attempts'] = entry.get('password_attempts', 0) + 1
            if entry['password_attempts'] >= 5:
                entry['locked_until'] = now + 15 * 60
                update.message.reply_text("❌ ভুল কোড। অনেকবার ভুল হয়েছে, ১৫ মিনিট পর চেষ্টা করুন।")
            else:
                left = 5 - entry['password_attempts']
                update.message.reply_text(f"❌ ভুল কোড। আবার চেষ্টা করুন। (বাকি সুযোগ: {left})")
            save_state()

# ----- /links (card-style + pagination) & revoke -----
def card_line(token: str, entry: dict) -> str:
    exp = entry.get('link_expiry')
    exp_txt = "∞" if exp is None else fmt_dt(exp)
    hits = entry.get('hit_count', 0)
    la_txt = fmt_dt(entry.get('last_access'))
    pw = "🔐" if entry.get('password_hash') else ""
    status = "REVOKED" if entry.get('revoked') else "Active"
    return (
        f"🗂 <b><code>{token}</code></b> {pw}\n"
        f"   • Status: <i>{status}</i>\n"
        f"   • Expires: <b>{exp_txt}</b>\n"
        f"   • Used: <b>{hits}</b> | Last: {la_txt}\n"
    )

def build_links_pages(context: CallbackContext, user_id: int):
    is_admin = user_id in SUPER_ADMINS
    pages, buf = [], ""
    max_chars = 3500

    if is_admin:
        by_owner = {}
        for token, entry in shared_files.items():
            by_owner.setdefault(entry.get('owner_id'), []).append((token, entry))
        if not by_owner: return ["(কোনো লিঙ্ক নেই)"]
        for owner in sorted(by_owner.keys(), key=lambda x: str(x)):
            try:
                user_obj = context.bot.get_chat(owner)
                uname = ("@" + user_obj.username) if user_obj and user_obj.username else str(owner)
            except Exception:
                uname = str(owner)
            block = f"<u>{uname}</u>\n"
            for token, entry in by_owner[owner]:
                block += card_line(token, entry) + "\n"
            if len(buf) + len(block) > max_chars and buf:
                pages.append(buf.strip()); buf = ""
            buf += block + "\n"
        if buf: pages.append(buf.strip())
    else:
        own_items = [(t, e) for t, e in shared_files.items() if e.get('owner_id') == user_id]
        if not own_items: return ["(কোনো লিঙ্ক নেই)"]
        for token, entry in own_items:
            block = card_line(token, entry) + "\n"
            if len(buf) + len(block) > max_chars and buf:
                pages.append(buf.strip()); buf = ""
            buf += block
        if buf: pages.append(buf.strip())
    return pages or ["(কোনো লিঙ্ক নেই)"]

def links_nav_keyboard(total, idx):
    prev_btn = InlineKeyboardButton("⬅️ Prev", callback_data=f"linksnav:prev")
    next_btn = InlineKeyboardButton("Next ➡️", callback_data=f"linksnav:next")
    close_btn = InlineKeyboardButton("✖ Close", callback_data="linksnav:close")
    if total <= 1: return InlineKeyboardMarkup([[close_btn]])
    return InlineKeyboardMarkup([[prev_btn, next_btn], [close_btn]])

def handle_links(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    ensure_user_state(user_id)

    pages = build_links_pages(context, user_id)
    user_state[user_id]['links_pages'] = pages
    user_state[user_id]['links_page_idx'] = 0

    sent = update.message.reply_text(
        pages[0], parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        reply_markup=links_nav_keyboard(len(pages), 0)
    )
    user_state[user_id]['links_msg_id'] = sent.message_id

    buttons, row, shown = [], [], 0
    MAX_BUTTONS = 30
    tokens_for_buttons = []
    if user_id in SUPER_ADMINS:
        tokens_for_buttons = list(shared_files.keys())
    else:
        for token, entry in shared_files.items():
            if entry.get('owner_id') == user_id:
                tokens_for_buttons.append(token)

    for token in tokens_for_buttons:
        if shown >= MAX_BUTTONS: break
        row.append(InlineKeyboardButton(f"Revoke {token}", callback_data=f"revoke:{token}"))
        if len(row) == 2: buttons.append(row); row = []
        shown += 1
    if row: buttons.append(row)
    if buttons:
        update.message.reply_text("রেভোক করতে নিচের বাটন থেকে বেছে নিন:", reply_markup=InlineKeyboardMarkup(buttons))

def on_links_nav(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    ensure_user_state(user_id)

    pages = user_state[user_id].get('links_pages') or []
    if not pages: query.answer(); return
    idx = user_state[user_id].get('links_page_idx', 0)

    action = query.data.split(":")[1]
    if action == "close":
        try: context.bot.edit_message_reply_markup(chat_id=query.message.chat_id, message_id=query.message.message_id, reply_markup=None)
        except Exception: pass
        query.answer("বন্ধ করা হয়েছে"); return

    if action == "next": idx = (idx + 1) % len(pages)
    elif action == "prev": idx = (idx - 1) % len(pages)

    user_state[user_id]['links_page_idx'] = idx
    try:
        context.bot.edit_message_text(
            pages[idx], chat_id=query.message.chat_id, message_id=query.message.message_id,
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            reply_markup=links_nav_keyboard(len(pages), idx)
        )
    except Exception: pass
    query.answer(f"Page {idx+1}/{len(pages)}")

def handle_revoke_cmd(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    is_admin = user_id in SUPER_ADMINS
    if not context.args:
        update.message.reply_text("ব্যবহার: /revoke <token>"); return
    token = context.args[0]
    entry = shared_files.get(token)
    if not entry:
        update.message.reply_text("টোকেন পাওয়া যায়নি।"); return
    if (not is_admin) and entry.get('owner_id') != user_id:
        update.message.reply_text("আপনার অনুমতি নেই।"); return
    entry['revoked'] = True; save_state()
    update.message.reply_text(f"✅ টোকেন {token} রেভোক করা হয়েছে।")

def on_revoke_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    is_admin = user_id in SUPER_ADMINS
    token = query.data.split(":")[1]
    entry = shared_files.get(token)
    if not entry: query.answer("পাওয়া যায়নি", show_alert=True); return
    if (not is_admin) and entry.get('owner_id') != user_id:
        query.answer("অনুমতি নেই", show_alert=True); return
    entry['revoked'] = True; save_state()
    query.answer("রেভোক হয়েছে")
    try: context.bot.edit_message_reply_markup(chat_id=query.message.chat_id, message_id=query.message.message_id, reply_markup=None)
    except Exception: pass
    context.bot.send_message(chat_id=query.message.chat_id, text=f"✅ টোকেন {token} রেভোক করা হয়েছে।")

def cleanup_expired(context: CallbackContext):
    now = time.time()
    to_delete = []
    for token, entry in list(shared_files.items()):
        exp = entry.get('link_expiry')
        revoked = entry.get('revoked')
        if exp is not None and now > exp + 7*DAY:
            to_delete.append(token)
        elif revoked and (now - entry.get('created_at', now) > 30*DAY):
            to_delete.append(token)
    for t in to_delete:
        try: shared_files.pop(t, None)
        except Exception: pass
    if to_delete: save_state()

def autosave_job(context: CallbackContext):
    save_state()

# =========================
# Flask + Webhook wiring
# =========================
TOKEN = os.environ.get("BOT_TOKEN")  # Render → Environment → BOT_TOKEN
if not TOKEN:
    raise RuntimeError("Set BOT_TOKEN env var")

bot = Bot(TOKEN)

# Dispatcher (without Updater)
dispatcher = Dispatcher(bot, update_queue=None, workers=0, use_context=True)
# Register handlers (same set as polling version)
dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(CommandHandler("msg", handle_msg))
dispatcher.add_handler(CommandHandler("user", handle_user_list))
dispatcher.add_handler(CommandHandler("links", handle_links))
dispatcher.add_handler(CommandHandler("revoke", handle_revoke_cmd))
dispatcher.add_handler(MessageHandler(Filters.document | Filters.photo | Filters.video, handle_media))
dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))
dispatcher.add_handler(CallbackQueryHandler(on_link_expiry_selected, pattern=r"^linkexp:"))
dispatcher.add_handler(CallbackQueryHandler(on_delete_after_selected, pattern=r"^delafter:"))
dispatcher.add_handler(CallbackQueryHandler(on_password_choice, pattern=r"^pwdchoice:"))
dispatcher.add_handler(CallbackQueryHandler(on_revoke_callback, pattern=r"^revoke:"))
dispatcher.add_handler(CallbackQueryHandler(on_links_nav, pattern=r"^linksnav:"))

# JobQueue (manually start)
job_queue = JobQueue()
job_queue.set_dispatcher(dispatcher)
job_queue.start()
job_queue.run_repeating(cleanup_expired, interval=3600, first=60)
job_queue.run_repeating(autosave_job,   interval=120,  first=30)

# Load persisted state before serving
load_state()

# Flask app
app = Flask(__name__)

@app.route("/", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "telegram-bot", "version": "webhook-ptb13"}), 200

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dispatcher.process_update(update)
    return "OK", 200

# Optional: helper to set webhook from code (use WEBHOOK_URL env)
@app.before_first_request
def init_webhook():
    url = os.environ.get("WEBHOOK_URL")  # e.g., https://your-service.onrender.com
    if url:
        try:
            bot.set_webhook(f"{url}/{TOKEN}", drop_pending_updates=True)
        except Exception:
            pass

# ---- admin commands reused ----
def handle_msg(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    message = update.message
    if user_id not in SUPER_ADMINS:
        update.message.reply_text("❌ ক্ষমা প্রার্থনা, আপনি অনুমোদিত সুপার এডমিন নন।"); return
    msg_text = ' '.join(context.args)
    if not msg_text and not (message.photo or message.video or message.document):
        update.message.reply_text("❌ দয়া করে /msg এর সাথে কিছু লিখুন অথবা মিডিয়া যোগ করুন।"); return
    send_text = f"এডমিন মেসেজ: {msg_text}" if msg_text else None
    for uid in list(all_users):
        if uid == user_id: continue
        try:
            if message.photo:
                context.bot.send_photo(chat_id=uid, photo=message.photo[-1].file_id, caption=send_text)
            elif message.video:
                context.bot.send_video(chat_id=uid, video=message.video.file_id, caption=send_text)
            elif message.document:
                context.bot.send_document(chat_id=uid, document=message.document.file_id, caption=send_text)
            elif send_text:
                context.bot.send_message(chat_id=uid, text=send_text)
        except Exception:
            continue
    update.message.reply_text("✅ আপনার বার্তা সকল ইউজারকে পাঠানো হয়েছে।")

def handle_user_list(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id not in SUPER_ADMINS:
        update.message.reply_text("❌ ক্ষমা প্রার্থনা, আপনি অনুমোদিত সুপার এডমিন নন।"); return
    total_users = len(all_users)
    user_lines = []
    for uid in all_users:
        try:
            user_obj = context.bot.get_chat(uid)
            uname = user_obj.username
        except Exception:
            uname = None
        display = f"@{uname}" if uname else str(uid)
        user_lines.append(display)
    msg_chunks, chunk = [], ""
    for line in user_lines:
        if len(chunk) + len(line) + 2 > 4000:
            msg_chunks.append(chunk); chunk = ""
        chunk += line + "\n"
    if chunk: msg_chunks.append(chunk)
    for i, m in enumerate(msg_chunks):
        header = f"👥 মোট ইউজার: {total_users}\n" if i == 0 else ""
        context.bot.send_message(chat_id=user_id, text=header + m)

if __name__ == "__main__":
    # Local run support (optional)
    # You can test locally by running: python main.py
    # Then use ngrok to expose and set WEBHOOK_URL to your ngrok URL before first request.
    PORT = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=PORT)