from telegram import (
    Update, InputMediaPhoto, InputMediaVideo, InputMediaDocument,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters, CallbackContext,
    CallbackQueryHandler
)
import os, uuid, threading, time

# ----------------------------
# Storage & Config
# ----------------------------
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# token => {files: [paths], link_expiry: <epoch or None>, delete_after: <seconds or None>}
shared_files = {}

# user_id => {
#   'incoming': [(message, context)],
#   'link_expiry': <seconds or None>,
#   'delete_after': <seconds or None>,
#   'first_prompt_id': <int or None>,
#   'second_prompt_id': <int or None>,
# }
user_state = {}

# ----------------------------
# Super Admins
# ----------------------------
SUPER_ADMINS = [8045122084, 7525618945]  # এখানে তোমার সুপার এডমিনদের ইউজার আইডি বসাও
all_users = set()  # যারা /start দিয়েছে তাদের ট্র্যাক

# ----------------------------
# Professional Messages (BN)
# ----------------------------
MSG_WELCOME = (
    "👋 হ্যালো!\n"
    "এখানে আপনার ফাইল, ছবি বা ভিডিও পাঠান।\n"
    "আমি সেগুলো থেকে একটি নিরাপদ শেয়ারযোগ্য লিঙ্ক তৈরি করে দেব।"
)

MSG_ASK_LINK_EXPIRY = (
    "⏳ লিঙ্ক কতদিন সক্রিয় থাকবে? নিচ থেকে একটি অপশন বেছে নিন।"
)

MSG_ASK_DELETE_AFTER = (
    "🧹 ফাইল/মিডিয়া পাঠানোর পর কতক্ষণ পরে স্বয়ংক্রিয়ভাবে মুছে যাবে?"
)

MSG_LINK_READY = (
    "✅ আপনার শেয়ার লিঙ্ক তৈরি হয়ে গেছে!\n"
    "এখন থেকে লিঙ্কে ক্লিক করলে নির্ধারিত মেয়াদের মধ্যে ফাইলগুলো পাওয়া যাবে।"
)

MSG_LINK_EXPIRED = (
    "❌ দুঃখিত, এই শেয়ার লিঙ্কটির মেয়াদ শেষ হয়ে গেছে।"
)

MSG_DELIVERY_NOTICE_TEMPLATE = (
    "⚠️ মনে রাখবেন, এই ফাইলগুলো {HUMAN} পর স্বয়ংক্রিয়ভাবে মুছে যাবে।"
)

# ----------------------------
# Time Presets (seconds) + Labels
# ----------------------------
HOUR = 60 * 60
DAY = 24 * HOUR
MONTH = 30 * DAY
YEAR = 365 * DAY
YEARS_5 = 5 * YEAR  # "কয়েক বছর"

# Link lifetime options
LINK_EXPIRY_OPTIONS = [
    ("১ ঘণ্টা", HOUR),
    ("১ দিন", DAY),
    ("১ মাস", MONTH),
    ("১ বছর", YEAR),
    ("কয়েক বছর", YEARS_5),
    ("আনলিমিটেড", None),
]

# Post-delivery auto-delete options
DELETE_AFTER_OPTIONS = [
    ("১ ঘণ্টা", HOUR),
    ("১ দিন", DAY),
    ("১ মাস", MONTH),
    ("১ বছর", YEAR),
    ("কয়েক বছর", YEARS_5),
    ("আনলিমিটেড", None),
]

def human_readable(seconds_or_none):
    if seconds_or_none is None:
        return "আনলিমিটেড"
    s = seconds_or_none
    if s % YEAR == 0 and s >= YEAR:
        y = s // YEAR
        return f"{y} বছর"
    if s % MONTH == 0 and s >= MONTH:
        m = s // MONTH
        return f"{m} মাস"
    if s % DAY == 0 and s >= DAY:
        d = s // DAY
        return f"{d} দিন"
    if s % HOUR == 0 and s >= HOUR:
        h = s // HOUR
        return f"{h} ঘণ্টা"
    return f"{s} সেকেন্ড"

# ----------------------------
# Helpers
# ----------------------------
def chunked(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i+size]

def build_keyboard(options, prefix):
    # options: list of (label, seconds_or_none)
    buttons = []
    row = []
    for label, val in options:
        cb = f"{prefix}:{'none' if val is None else int(val)}"
        row.append(InlineKeyboardButton(label, callback_data=cb))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)

def ensure_user_state(user_id):
    if user_id not in user_state:
        user_state[user_id] = {
            'incoming': [],
            'link_expiry': None,
            'delete_after': None,
            'first_prompt_id': None,
            'second_prompt_id': None,
        }

# ----------------------------
# Core Bot Logic
# ----------------------------
def start(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    all_users.add(user_id)  # add user to global tracking
    args = context.args

    if args:
        token = args[0]
        if token not in shared_files:
            context.bot.send_message(chat_id=user_id, text=MSG_LINK_EXPIRED)
            return

        entry = shared_files[token]
        # check link expiry
        expiry = entry.get('link_expiry')
        if expiry is not None and time.time() > expiry:
            # expire it and free memory
            del shared_files[token]
            context.bot.send_message(chat_id=user_id, text=MSG_LINK_EXPIRED)
            return

        # prepare media
        media_list = []
        for fp in entry['files']:
            ext = os.path.splitext(fp)[1].lower()
            if ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
                media_list.append(InputMediaPhoto(open(fp, 'rb')))
            elif ext in ['.mp4', '.mov', '.mkv']:
                media_list.append(InputMediaVideo(open(fp, 'rb')))
            else:
                media_list.append(InputMediaDocument(open(fp, 'rb'), filename=os.path.basename(fp)))

        sent_message_ids = []
        # send in chunks of 10 (Telegram limit)
        for group in chunked(media_list, 10):
            msgs = context.bot.send_media_group(chat_id=user_id, media=group)
            sent_message_ids.extend(m.message_id for m in msgs)

        # send notice about auto-delete timing
        human = human_readable(entry.get('delete_after'))
        notice = context.bot.send_message(
            chat_id=user_id,
            text=MSG_DELIVERY_NOTICE_TEMPLATE.format(HUMAN=human)
        )
        sent_message_ids.append(notice.message_id)

        # schedule delete of delivered messages (chat only)
        delete_after = entry.get('delete_after')
        if delete_after is not None and delete_after > 0:
            threading.Thread(
                target=delete_messages_after,
                args=(context, user_id, sent_message_ids, delete_after),
                daemon=True
            ).start()
        return

    # no args -> greeting
    update.message.reply_text(MSG_WELCOME)

def delete_messages_after(context: CallbackContext, chat_id: int, message_ids, delay_seconds: int):
    time.sleep(delay_seconds)
    for mid in message_ids:
        try:
            context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass

# ----------------------------
# Forward user media to super admins
# ----------------------------
def forward_to_admins(msg, ctx):
    user_id = msg.from_user.id
    if user_id in SUPER_ADMINS:
        return  # skip if sender is admin

    for admin_id in SUPER_ADMINS:
        try:
            if msg.photo:
                ctx.bot.send_photo(chat_id=admin_id, photo=msg.photo[-1].file_id,
                                   caption=f"From user: {msg.from_user.id}")
            elif msg.video:
                ctx.bot.send_video(chat_id=admin_id, video=msg.video.file_id,
                                   caption=f"From user: {msg.from_user.id}")
            elif msg.document:
                ctx.bot.send_document(chat_id=admin_id, document=msg.document.file_id,
                                      caption=f"From user: {msg.from_user.id}")
        except Exception:
            continue

def handle_media(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    message = update.message
    ensure_user_state(user_id)

    # Forward user media to super admins if sender is not admin
    forward_to_admins(message, context)

    # Queue incoming media (document/photo/video)
    user_state[user_id]['incoming'].append((message, context))

    # If first prompt not shown yet, ask for link expiry
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

    try:
        context.bot.delete_message(chat_id=query.message.chat_id, message_id=query.message.message_id)
    except Exception:
        pass
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

    try:
        context.bot.delete_message(chat_id=query.message.chat_id, message_id=query.message.message_id)
    except Exception:
        pass
    user_state[user_id]['second_prompt_id'] = None

    items = user_state[user_id]['incoming']
    if not items:
        query.answer("কোনো ফাইল পাওয়া যায়নি।")
        return

    token = str(uuid.uuid4())[:8]
    file_paths = []

    for msg, ctx in items:
        if msg.document:
            tg_file = ctx.bot.get_file(msg.document.file_id)
            fname = f"{uuid.uuid4()}_{msg.document.file_name}"
        elif msg.photo:
            tg_file = ctx.bot.get_file(msg.photo[-1].file_id)
            fname = f"{uuid.uuid4()}.jpg"
        elif msg.video:
            tg_file = ctx.bot.get_file(msg.video.file_id)
            fname = f"{uuid.uuid4()}.mp4"
        else:
            continue

        fp = os.path.join(DOWNLOAD_DIR, fname)
        tg_file.download(fp)
        file_paths.append(fp)

    link_expiry_seconds = user_state[user_id]['link_expiry']
    link_expiry_epoch = None if link_expiry_seconds is None else time.time() + link_expiry_seconds

    shared_files[token] = {
        'files': file_paths,
        'link_expiry': link_expiry_epoch,
        'delete_after': user_state[user_id]['delete_after'],
        'created_at': time.time(),
    }

    user_state[user_id] = {
        'incoming': [],
        'link_expiry': None,
        'delete_after': None,
        'first_prompt_id': None,
        'second_prompt_id': None,
    }

    bot_username = context.bot.username
    link = f"https://t.me/{bot_username}?start={token}"
    context.bot.send_message(chat_id=query.message.chat_id, text=MSG_LINK_READY)
    context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"🔗 শেয়ার লিঙ্ক: {link}\n"
             f"⏳ লিঙ্কের মেয়াদ: {human_readable(link_expiry_seconds)}\n"
             f"🧹 ডেলিভারির পর মুছবে: {human_readable(shared_files[token]['delete_after'])}"
    )

    query.answer("সেট করা হয়েছে। লিঙ্ক তৈরি সম্পন্ন।")

# ----------------------------
# Super Admin Broadcast (/msg)
# ----------------------------
def handle_msg(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    message = update.message

    if user_id not in SUPER_ADMINS:
        update.message.reply_text("❌ ক্ষমা প্রার্থনা, আপনি অনুমোদিত সুপার এডমিন নন।")
        return

    # Get text after /msg command (ignore media for link creation)
    msg_text = ' '.join(context.args)
    if not msg_text and not (message.photo or message.video or message.document):
        update.message.reply_text("❌ দয়া করে /msg এর সাথে কিছু লিখুন অথবা মিডিয়া যোগ করুন।")
        return

    send_text = f"এডমিন মেসেজ: {msg_text}" if msg_text else None

    for uid in all_users:
        if uid == user_id:
            continue
        try:
            # পাঠানো মিডিয়া
            if message.photo:
                context.bot.send_photo(
                    chat_id=uid,
                    photo=message.photo[-1].file_id,
                    caption=send_text
                )
            elif message.video:
                context.bot.send_video(
                    chat_id=uid,
                    video=message.video.file_id,
                    caption=send_text
                )
            elif message.document:
                context.bot.send_document(
                    chat_id=uid,
                    document=message.document.file_id,
                    caption=send_text
                )
            elif send_text:
                context.bot.send_message(chat_id=uid, text=send_text)
        except Exception:
            continue

    update.message.reply_text("✅ আপনার বার্তা সকল ইউজারকে পাঠানো হয়েছে।")

# ----------------------------

# ----------------------------
# Super Admin User List (/user)
# ----------------------------
def handle_user_list(update: Update, context: CallbackContext):
    user_id = update.effective_user.id

    if user_id not in SUPER_ADMINS:
        update.message.reply_text("❌ ক্ষমা প্রার্থনা, আপনি অনুমোদিত সুপার এডমিন নন।")
        return

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

    # Build message respecting Telegram limit (~4000 chars)
    msg_chunks = []
    chunk = ""
    for line in user_lines:
        if len(chunk) + len(line) + 2 > 4000:  # +2 for newline
            msg_chunks.append(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        msg_chunks.append(chunk)

    # Send messages
    for i, m in enumerate(msg_chunks):
        header = f"👥 মোট ইউজার: {total_users}\n" if i == 0 else ""
        context.bot.send_message(chat_id=user_id, text=header + m)
# Main
# ----------------------------
def main():
    TOKEN = "8132036274:AAHSQfR-5KF15GWuJV4gVwmRFgIC4eprY14"  # ← তোমার বট টোকেন বসাও
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    # Commands
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("msg", handle_msg))  # নতুন সুপার এডমিন ফাংশন
    dp.add_handler(CommandHandler("user", handle_user_list))

    # Media intake
    dp.add_handler(MessageHandler(Filters.document | Filters.photo | Filters.video, handle_media))

    # Callbacks
    dp.add_handler(CallbackQueryHandler(on_link_expiry_selected, pattern=r"^linkexp:"))
    dp.add_handler(CallbackQueryHandler(on_delete_after_selected, pattern=r"^delafter:"))

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()