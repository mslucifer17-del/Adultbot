import os
import re
import sys
import asyncio
import logging
import aiohttp
import psycopg2
from urllib.parse import quote
from html import escape as html_escape
from psycopg2 import pool
from flask import Flask, redirect
from threading import Thread
from datetime import datetime, timedelta
from io import BytesIO
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes, ConversationHandler
)

# Force unbuffered output for better logging
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

try:
    import qrcode
    QR_AVAILABLE = True
except ImportError:
    QR_AVAILABLE = False
    print("⚠️ qrcode not available - will use text fallback")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

print("=" * 60)
print("🚀 APPLICATION STARTING...")
print("=" * 60)

# ================= ENVIRONMENT VARIABLES =================
print("\n📋 Loading environment variables...")

MAIN_BOT_TOKEN = os.environ.get("MAIN_BOT_TOKEN")
PROVIDER_BOT_TOKEN = os.environ.get("PROVIDER_BOT_TOKEN")
PROVIDER_BOT_USERNAME = os.environ.get("PROVIDER_BOT_USERNAME", "").replace("@", "")
GPLINKS_API_KEY = os.environ.get("GPLINKS_API_KEY")

# Fix DATABASE_URL for Render (postgres:// -> postgresql://)
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    logger.info("✅ Fixed DATABASE_URL format for psycopg2")

WEB_DOMAIN = os.environ.get("WEB_DOMAIN", "https://my-bot.onrender.com").strip()
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "ownermahi")

FREE_CH = int(os.environ.get("FREE_CHANNEL_ID", "0"))
PAID_CH = int(os.environ.get("PAID_CHANNEL_ID", "0"))
BACKUP_1 = int(os.environ.get("BACKUP_CHANNEL_1", "0"))
BACKUP_2 = int(os.environ.get("BACKUP_CHANNEL_2", "0"))

AUTO_DELETE_TIME = int(os.environ.get("AUTO_DELETE_TIME", "300"))
TEXT_DELETE_TIME = int(os.environ.get("TEXT_DELETE_TIME", "120"))
QR_DELETE_TIME = int(os.environ.get("QR_DELETE_TIME", "600"))
UPI_ID = os.environ.get("UPI_ID", "tumhara@upi")
FREE_CHANNEL_LINK = os.environ.get("FREE_CHANNEL_LINK", "https://t.me/your_free_channel")
SUBSCRIPTION_AMOUNT = os.environ.get("SUBSCRIPTION_AMOUNT", "10")

WAIT_TRIM, WAIT_FULL = range(2)
BULK_WAIT_VIDEO, BULK_CONFIRM = range(2)

print("✅ Environment variables loaded")

# ================= DATABASE SETUP =================
db_pool = None

def init_db_pool():
    global db_pool
    try:
        db_pool = pool.SimpleConnectionPool(1, 20, dsn=DATABASE_URL)
        logger.info("✅ Database pool created successfully")
        print("✅ Database pool initialized")
    except Exception as e:
        logger.error(f"❌ Database pool creation failed: {e}")
        raise

def setup_db():
    conn = None
    try:
        conn = db_pool.getconn()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS adult_videos (
                vid_id SERIAL PRIMARY KEY,
                title TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS video_qualities (
                quality_id SERIAL PRIMARY KEY,
                vid_id INTEGER REFERENCES adult_videos(vid_id) ON DELETE CASCADE,
                quality_label TEXT,
                file_url TEXT,
                file_size BIGINT DEFAULT 0,
                width INTEGER DEFAULT 0,
                height INTEGER DEFAULT 0,
                duration INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                start_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                end_date TIMESTAMP,
                notified BOOLEAN DEFAULT FALSE
            )
        """)

        try:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'video_qualities'
            """)
            existing_cols = [row[0] for row in cur.fetchall()]

            if existing_cols:
                if 'file_id' in existing_cols and 'file_url' not in existing_cols:
                    cur.execute("ALTER TABLE video_qualities RENAME COLUMN file_id TO file_url")
                    logger.info("Migrated: file_id to file_url")
                elif 'file_url' not in existing_cols:
                    cur.execute("ALTER TABLE video_qualities ADD COLUMN file_url TEXT")
                    logger.info("Added file_url column")
        except Exception as e:
            logger.warning(f"Migration note: {e}")

        conn.commit()
        cur.close()
        logger.info("✅ Database tables created/verified")
        print("✅ Database tables ready")
    except Exception as e:
        logger.error(f"❌ Database setup failed: {e}")
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            db_pool.putconn(conn)

def get_db_connection():
    if db_pool is None:
        raise Exception("Database pool not initialized")
    return db_pool.getconn()

# ================= HELPER FUNCTIONS =================

def construct_file_url(channel_id, message_id):
    channel_str = str(channel_id)
    if channel_str.startswith('-100'):
        channel_str = channel_str[4:]
    elif channel_str.startswith('-'):
        channel_str = channel_str[1:]
    return f"https://t.me/c/{channel_str}/{message_id}"

def parse_file_url(file_url):
    if not file_url:
        return None, None
    match = re.match(r'https://t\.me/c/(\d+)/(\d+)', file_url)
    if match:
        channel_part = match.group(1)
        msg_id = int(match.group(2))
        channel_id = int(f"-100{channel_part}")
        return channel_id, msg_id
    return None, None

def clean_title(raw_title):
    if not raw_title:
        return "Exclusive Premium Content"
    title = raw_title.strip()
    title = re.sub(r'@\w+', '', title)
    title = re.sub(r'\d+min\s+from\s+\d+:\d+:\d+\s+of\s+', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\.(mkv|mp4|avi|mov|wmv|flv|webm|m4v)', '', title, flags=re.IGNORECASE)
    unwanted_patterns = [
        r'\b(Seva|HEVC|HDRip|UNRAT|UNRATED|720p|1080p|480p|4K|2160p)\b',
        r'\b(Dzyreplay|DZREPLAY|Replay)\b',
        r'\b(S\d+|Season\s*\d+|E\d+|Episode\s*\d+)\b',
    ]
    for pattern in unwanted_patterns:
        title = re.sub(pattern, '', title, flags=re.IGNORECASE)
    title = re.sub(r'\s+', ' ', title)
    title = re.sub(r'[^\w\s\-\(\)]', '', title)
    title = title.strip()
    if len(title) < 5:
        return "Exclusive Premium Content"
    return title

def generate_display_title(cleaned_title):
    if len(cleaned_title) > 50:
        return cleaned_title[:47] + "..."
    return cleaned_title

def detect_quality_label(video_obj=None, document_obj=None, caption=""):
    width = 0
    height = 0
    file_size = 0
    if video_obj:
        width = video_obj.width or 0
        height = video_obj.height or 0
        file_size = video_obj.file_size or 0
    elif document_obj:
        file_size = document_obj.file_size or 0

    caption_lower = caption.lower() if caption else ""
    if '4k' in caption_lower or '2160' in caption_lower:
        return '4K', width, height, file_size
    if '1080' in caption_lower:
        return '1080p', width, height, file_size
    if '720' in caption_lower:
        return '720p', width, height, file_size
    if '480' in caption_lower:
        return '480p', width, height, file_size
    if '360' in caption_lower:
        return '360p', width, height, file_size

    max_dim = max(width, height)
    if max_dim >= 2160:
        return '4K', width, height, file_size
    elif max_dim >= 1080:
        return '1080p', width, height, file_size
    elif max_dim >= 720:
        return '720p', width, height, file_size
    elif max_dim >= 480:
        return '480p', width, height, file_size
    elif max_dim >= 360:
        return '360p', width, height, file_size
    elif max_dim > 0:
        return f'{max_dim}p', width, height, file_size

    if file_size > 0:
        size_mb = file_size / (1024 * 1024)
        if size_mb > 500:
            return '1080p (est.)', width, height, file_size
        elif size_mb > 200:
            return '720p (est.)', width, height, file_size
        elif size_mb > 50:
            return '480p (est.)', width, height, file_size
        else:
            return '360p (est.)', width, height, file_size

    return 'Unknown', width, height, file_size

def format_file_size(size_bytes):
    if size_bytes <= 0:
        return "Unknown"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

def generate_upi_qr(user_id, user_name, amount):
    safe_name = re.sub(r'[^a-zA-Z0-9 ]', '', user_name)[:30].strip()
    if not safe_name:
        safe_name = "User"
    note = f"TG-{user_id}-{safe_name}"

    upi_url = (
        f"upi://pay"
        f"?pa={quote(UPI_ID)}"
        f"&pn={quote('VIP Subscription')}"
        f"&am={quote(str(amount))}"
        f"&tn={quote(note)}"
        f"&cu=INR"
    )

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(upi_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = BytesIO()
    img.save(bio, format='PNG')
    bio.seek(0)
    bio.name = f"qr_{user_id}.png"
    return bio, note

def build_free_channel_caption(title, qualities_info):
    safe_title = html_escape(title)
    quality_text = " | ".join([q['label'] for q in qualities_info]) if qualities_info else "HD Quality"
    
    return (
        f"🎬 <b>{safe_title}</b>\n\n"
        f"🔞 <b>18+ Exclusive Premium Content</b>\n\n"
        f"📊 <b>Available Qualities:</b> {quality_text}\n\n"
        f"👇 <b>Watch Full Video & Download Below</b> 👇\n"
    )

def build_backup_caption(title, quality_label=""):
    safe_title = html_escape(title)
    if quality_label:
        return f"🔒 {safe_title} [{quality_label}]"
    return f"🔒 {safe_title}"

async def schedule_delete(context, chat_id, message_id, delay=120):
    try:
        await asyncio.sleep(delay)
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"Text msg {message_id} deleted from {chat_id}")
    except Exception as e:
        logger.error(f"Text delete error {message_id}: {e}")

async def auto_delete_with_notification(context, chat_id, message_ids_to_delete, delete_time=AUTO_DELETE_TIME):
    try:
        if isinstance(message_ids_to_delete, int):
            message_ids_to_delete = [message_ids_to_delete]

        wait_time = max(delete_time - 30, 60)
        await asyncio.sleep(wait_time)

        try:
            warning_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "⚠️ IMPORTANT NOTICE\n\n"
                    "🕒 Videos 30 seconds mein auto-delete ho jayengi!\n\n"
                    "💾 Jaldi se Saved Messages mein forward kar lo!\n\n"
                    "🔒 Yeh copyright protection ke liye hai."
                )
            )
            message_ids_to_delete.append(warning_msg.message_id)
        except Exception as e:
            logger.error(f"Warning message error: {e}")

        await asyncio.sleep(30)

        for msg_id in message_ids_to_delete:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                logger.info(f"Message {msg_id} deleted for chat: {chat_id}")
            except Exception as e:
                logger.error(f"Failed to delete message {msg_id}: {e}")

        try:
            final_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "🗑️ Video(s) Auto-Deleted!\n\n"
                    "✅ Agar forward kar liya hai toh saved messages mein check karein.\n"
                    "❌ Nahi kiya toh dobara link se access karein."
                )
            )
            await asyncio.sleep(30)
            try:
                await final_msg.delete()
            except:
                pass
        except Exception as e:
            logger.error(f"Final notice error: {e}")
    except Exception as e:
        logger.error(f"Auto-delete error: {e}")

def check_active_subscription(user_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT end_date FROM subscribers WHERE user_id = %s", (user_id,))
        result = cur.fetchone()
        cur.close()
        if result and result[0]:
            end_date = result[0]
            if end_date > datetime.now():
                return True, end_date
            else:
                return False, end_date
        return False, None
    except Exception as e:
        logger.error(f"Sub check error: {e}")
        return False, None
    finally:
        if conn:
            db_pool.putconn(conn)

# ================= WEB REDIRECTOR =================
app = Flask(__name__)

@app.route('/')
def home():
    return "🤖 Bot Server Running! ✅"

@app.route('/watch/<int:vid_id>')
def watch_video(vid_id):
    bot_username = PROVIDER_BOT_USERNAME or "your_bot"
    return redirect(f"https://t.me/{bot_username}?start=vid_{vid_id}")

def run_flask():
    port = int(os.environ.get('PORT', 8080))
    try:
        app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
    except Exception as e:
        logger.error(f"Flask error: {e}")

# ================================================================
#                    MAIN BOT (ADMIN ONLY)
# ================================================================

async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text(
            "❌ <b>Access Denied!</b>\n\n"
            "Yeh Admin Bot hai. Sirf admin use kar sakta hai.\n\n"
            f"👉 Videos ke liye @{PROVIDER_BOT_USERNAME} use karein.",
            parse_mode='HTML'
        )
        return ConversationHandler.END

    backup_status = "Set" if BACKUP_1 != 0 else "NOT SET (REQUIRED!)"
    await update.message.reply_text(
        "🤖 <b>Admin Bot Ready!</b>\n\n"
        "📌 <b>Commands:</b>\n"
        "  /post - Single video post\n"
        "  /bulk - Bulk upload multiple videos\n"
        "  /cancel - Cancel current operation\n"
        "  /start - Reset everything\n\n"
        f"📦 Backup Channel: {backup_status}\n"
        "⚡ <b>File URL Mode Active</b>\n"
        "📊 <b>Duplicate check: File Size based</b>\n\n"
        "🎬 Shuru karne ke liye /post ya /bulk use karo!",
        parse_mode='HTML'
    )
    return ConversationHandler.END

async def start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Access Denied!")
        return ConversationHandler.END

    if BACKUP_1 == 0:
        await update.message.reply_text(
            "❌ <b>BACKUP_CHANNEL_1 not set!</b>\n\n"
            "File URL mode requires a backup channel.\n"
            "Set BACKUP_CHANNEL_1 env variable first.",
            parse_mode='HTML'
        )
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data['mode'] = 'single'
    context.user_data['full_videos'] = []
    await update.message.reply_text(
        "⚡ <b>Single Post Mode!</b>\n\n"
        "✂️ Sabse pehle <b>TRIM/PREVIEW</b> bhejo:\n"
        "  • 📹 Choti trimmed video\n"
        "  • 🖼️ Ya koi image/photo\n"
        "  • ⏭️ Ya <code>/skip</code>\n\n"
        "❌ Cancel: /cancel",
        parse_mode='HTML'
    )
    return WAIT_TRIM

async def get_trim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return WAIT_TRIM

    if msg.text and msg.text.strip().lower() == '/skip':
        context.user_data['trim_type'] = 'skip'
        await msg.reply_text(
            "⏭️ <b>Skipped!</b>\n\n"
            "🔞 Ab <b>FULL VIDEO(s)</b> bhejo.\n"
            "📊 Multiple qualities? Sab bhejo, phir <code>/done</code>\n\n"
            "⚠️ Duplicate = same file size → rejected\n"
            "❌ Cancel: /cancel",
            parse_mode='HTML'
        )
        return WAIT_FULL

    if msg.text and msg.text.strip().lower() == '/cancel':
        context.user_data.clear()
        await msg.reply_text("❌ Upload cancelled.")
        return ConversationHandler.END

    if msg.text and msg.text.strip().lower() == '/start':
        context.user_data.clear()
        await msg.reply_text("🔄 Reset! Use /post to start again.")
        return ConversationHandler.END

    if msg.photo:
        context.user_data['trim_type'] = 'photo'
        context.user_data['trim_chat_id'] = msg.chat_id
        context.user_data['trim_msg_id'] = msg.message_id
        context.user_data['original_html'] = msg.caption_html
        raw_caption = msg.caption if msg.caption else ""
        cleaned_title = clean_title(raw_caption)
        if cleaned_title != "Exclusive Premium Content":
            context.user_data['title'] = cleaned_title
        await msg.reply_text(
            f"✅ <b>Preview Image Saved!</b>\n\n"
            f"📝 Title: {html_escape(cleaned_title)}\n\n"
            "🔞 Ab <b>FULL VIDEO(s)</b> bhejo.\n"
            "📊 Multiple qualities? Sab bhejo, phir <code>/done</code>\n\n"
            "❌ Cancel: /cancel",
            parse_mode='HTML'
        )
        return WAIT_FULL

    if msg.video or msg.document or msg.animation:
        raw_caption = msg.caption if msg.caption else ""
        cleaned_title = clean_title(raw_caption)
        if cleaned_title != "Exclusive Premium Content":
            context.user_data['title'] = cleaned_title

        context.user_data['trim_chat_id'] = msg.chat_id
        context.user_data['trim_msg_id'] = msg.message_id
        context.user_data['original_html'] = msg.caption_html

        if msg.video:
            context.user_data['trim_type'] = 'video'
        elif msg.animation:
            context.user_data['trim_type'] = 'animation'
        else:
            context.user_data['trim_type'] = 'document'

        await msg.reply_text(
            f"✅ <b>Trim Video Saved!</b>\n\n"
            f"📝 Title: {html_escape(cleaned_title)}\n\n"
            "🔞 Ab <b>FULL VIDEO(s)</b> bhejo, phir <code>/done</code>\n\n"
            "⚠️ <b>Note:</b> Trim video sirf preview ke liye hai.\n"
            "📹 Ab actual FULL quality videos bhejo!\n\n"
            "❌ Cancel: /cancel",
            parse_mode='HTML'
        )
        return WAIT_FULL

    await msg.reply_text(
        "❌ Invalid! Send:\n"
        "  📹 Trim Video / 🖼️ Photo / ⏭️ /skip / ❌ /cancel"
    )
    return WAIT_TRIM

async def get_full_and_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return WAIT_FULL

    if msg.text and msg.text.strip().lower() == '/done':
        full_videos = context.user_data.get('full_videos', [])
        if not full_videos:
            await msg.reply_text("❌ Koi video nahi! Pehle video bhejo, phir /done.")
            return WAIT_FULL
        return await finalize_single_post(update, context)

    if msg.text and msg.text.strip().lower() == '/cancel':
        context.user_data.clear()
        await msg.reply_text("❌ Upload cancelled.")
        return ConversationHandler.END

    if msg.text and msg.text.strip().lower() == '/start':
        context.user_data.clear()
        await msg.reply_text("🔄 Reset! Use /post to start again.")
        return ConversationHandler.END

    if not msg.video and not msg.document:
        await msg.reply_text("❌ Video file bhejo! Ya /done / /cancel likho.")
        return WAIT_FULL

    raw_caption = msg.caption if msg.caption else ""
    title = context.user_data.get('title', '')
    if not title or title == "Exclusive Premium Content":
        title = clean_title(raw_caption)
        context.user_data['title'] = title

    video_obj = msg.video
    doc_obj = msg.document
    duration = 0
    quality_label, width, height, file_size = detect_quality_label(
        video_obj=video_obj, document_obj=doc_obj, caption=raw_caption
    )
    if video_obj:
        duration = video_obj.duration or 0

    # 👇 Thumbnail store karo
    thumb_id = None
    if video_obj and video_obj.thumbnail:
        thumb_id = video_obj.thumbnail.file_id
    elif doc_obj and doc_obj.thumbnail:
        thumb_id = doc_obj.thumbnail.file_id

    full_videos = context.user_data.get('full_videos', [])
    for existing in full_videos:
        if existing['file_size'] == file_size and file_size > 0:
            existing_size_str = format_file_size(existing['file_size'])
            await msg.reply_text(
                f"⚠️ <b>Duplicate detected!</b>\n\n"
                f"📦 File size <b>{format_file_size(file_size)}</b> already exists "
                f"as <b>{existing['quality_label']}</b> ({existing_size_str})\n\n"
                f"📹 Same size = same file. Different quality bhejo ya /done",
                parse_mode='HTML'
            )
            return WAIT_FULL

    video_data = {
        'quality_label': quality_label,
        'width': width,
        'height': height,
        'file_size': file_size,
        'duration': duration,
        'chat_id': msg.chat_id,
        'msg_id': msg.message_id,
        'thumb_id': thumb_id   # 👈 Thumbnail ID yahan store kiya
    }
    full_videos.append(video_data)
    context.user_data['full_videos'] = full_videos

    count = len(full_videos)
    size_str = format_file_size(file_size)
    quality_list = "\n".join(
        [f"  {i + 1}. {v['quality_label']} ({format_file_size(v['file_size'])})"
         for i, v in enumerate(full_videos)]
    )

    await msg.reply_text(
        f"✅ <b>Video #{count} Added!</b>\n\n"
        f"📊 Quality: <b>{quality_label}</b>\n"
        f"💾 Size: {size_str}\n"
        f"⏱️ Duration: {duration}s\n\n"
        f"📋 <b>All Qualities:</b>\n{quality_list}\n\n"
        f"📹 Aur bhejo ya <code>/done</code> likho\n❌ Cancel: /cancel",
        parse_mode='HTML'
    )
    return WAIT_FULL

async def finalize_single_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    full_videos = context.user_data.get('full_videos', [])
    title = context.user_data.get('title', 'Exclusive Premium Content')
    trim_type = context.user_data.get('trim_type', 'skip')
    trim_chat_id = context.user_data.get('trim_chat_id')
    trim_msg_id = context.user_data.get('trim_msg_id')
    
    total = len(full_videos)
    status = await msg.reply_text(f"⏳ Processing {total} quality(ies)...")

    conn = None
    vid_id = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO adult_videos (title) VALUES (%s) RETURNING vid_id", (title,))
        vid_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
    except Exception as e:
        await status.edit_text(f"❌ Database error: {e}")
        context.user_data.clear()
        return ConversationHandler.END
    finally:
        if conn: 
            db_pool.putconn(conn)

    qualities_info = []
    failed_qualities = []

    for idx, vdata in enumerate(full_videos):
        q_label = vdata['quality_label']
        src_chat_id = vdata['chat_id']
        src_msg_id = vdata['msg_id']
        await status.edit_text(f"⏳ Uploading {idx + 1}/{total}: {q_label}...")
        backup_caption = build_backup_caption(title, q_label)

        file_url = None
        try:
            copied_msg = await context.bot.copy_message(
                chat_id=BACKUP_1, from_chat_id=src_chat_id,
                message_id=src_msg_id, caption=backup_caption, parse_mode='HTML'
            )
            file_url = construct_file_url(BACKUP_1, copied_msg.message_id)
        except Exception as e:
            failed_qualities.append(q_label)
            logger.error(f"Backup failed for {q_label}: {e}")
            continue

        for ch_id in [ch for ch in [BACKUP_2, PAID_CH] if ch != 0]:
            try:
                await context.bot.copy_message(
                    chat_id=ch_id, from_chat_id=src_chat_id,
                    message_id=src_msg_id, caption=backup_caption, parse_mode='HTML'
                )
            except: 
                pass

        conn2 = None
        try:
            conn2 = get_db_connection()
            cur2 = conn2.cursor()
            cur2.execute(
                """INSERT INTO video_qualities (vid_id, quality_label, file_url, file_size, width, height, duration)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (vid_id, q_label, file_url, vdata['file_size'], vdata['width'], vdata['height'], vdata['duration'])
            )
            conn2.commit()
            cur2.close()
        except Exception as e:
            logger.error(f"DB save error for {q_label}: {e}")
        finally:
            if conn2: 
                db_pool.putconn(conn2)

        qualities_info.append({'label': q_label, 'size': format_file_size(vdata['file_size']), 'url': file_url})

    if not qualities_info:
        await status.edit_text("❌ All uploads failed!")
        context.user_data.clear()
        return ConversationHandler.END

    bot_username = PROVIDER_BOT_USERNAME if PROVIDER_BOT_USERNAME else "your_bot"
    # 👇 Buy VIP button deep link banaya
    bot_link = f"https://t.me/{bot_username}?start=vid_{vid_id}"
    buy_link = f"https://t.me/{bot_username}?start=buy"
    
    post_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Watch Now / Download 📥", url=bot_link)],
        [InlineKeyboardButton("💎 Buy VIP Subscription", url=buy_link)]   # 👈 Deep link
    ])

    original_html = context.user_data.get('original_html')

    if original_html:
        import re
        caption = re.sub(r'╔═══════════════════╗.*?╚═══════════════════╝', '', original_html, flags=re.DOTALL)
        caption = re.sub(r'[^\n]*ʜᴏᴡ ᴛᴏ ᴏᴘᴇɴ ᴛʜᴇ ʟɪɴᴋ[^\n]*', '', caption, flags=re.IGNORECASE)
        caption = re.sub(r'[^\n]*HOW TO OPEN THE LINK[^\n]*', '', caption, flags=re.IGNORECASE)
        caption = re.sub(r'\n\s*\n\s*\n', '\n\n', caption).strip()
    else:
        caption = build_free_channel_caption(title, qualities_info)

    # 👇 Free channel par post
    if FREE_CH != 0:
        if trim_type != 'skip' and trim_chat_id and trim_msg_id:
            try:
                await context.bot.copy_message(
                    chat_id=FREE_CH, 
                    from_chat_id=trim_chat_id,
                    message_id=trim_msg_id, 
                    caption=caption, 
                    parse_mode='HTML', 
                    reply_markup=post_keyboard
                )
                logger.info(f"Free channel: Posted trim video for {title}")
            except Exception as e:
                logger.error(f"Trim video post failed: {e}")
                # Fallback: first video ka thumbnail bhejo
                first_vid = full_videos[0]
                thumb_id = first_vid.get('thumb_id')
                if thumb_id:
                    try:
                        await context.bot.send_photo(
                            chat_id=FREE_CH,
                            photo=thumb_id,
                            caption=caption,
                            parse_mode='HTML',
                            reply_markup=post_keyboard
                        )
                    except Exception as e2:
                        logger.error(f"Fallback photo failed: {e2}")
                else:
                    # Last resort: text message with button
                    try:
                        await context.bot.send_message(
                            chat_id=FREE_CH,
                            text=caption + "\n\n(Preview unavailable)",
                            parse_mode='HTML',
                            reply_markup=post_keyboard
                        )
                    except Exception as e3:
                        logger.error(f"Fallback text failed: {e3}")
        else:
            first_vid = full_videos[0]
            thumb_id = first_vid.get('thumb_id')
            if thumb_id:
                try:
                    await context.bot.send_photo(
                        chat_id=FREE_CH,
                        photo=thumb_id,
                        caption=caption,
                        parse_mode='HTML',
                        reply_markup=post_keyboard
                    )
                except Exception as e:
                    logger.error(f"Photo post failed: {e}")
                    # fallback to copy first video (but that gives full video)
                    try:
                        await context.bot.copy_message(
                            chat_id=FREE_CH, 
                            from_chat_id=first_vid['chat_id'],
                            message_id=first_vid['msg_id'], 
                            caption=caption, 
                            parse_mode='HTML', 
                            reply_markup=post_keyboard
                        )
                    except Exception as e2:
                        logger.error(f"Fallback copy failed: {e2}")
            else:
                # no thumbnail, send text
                try:
                    await context.bot.send_message(
                        chat_id=FREE_CH,
                        text=caption + "\n\n(Click below to watch)",
                        parse_mode='HTML',
                        reply_markup=post_keyboard
                    )
                except Exception as e:
                    logger.error(f"Text fallback failed: {e}")

    q_str = ", ".join([f"{q['label']}({q['size']})" for q in qualities_info])
    fail_str = f"\n⚠️ Failed: {', '.join(failed_qualities)}" if failed_qualities else ""
    
    await status.edit_text(
        f"✅ <b>SUCCESS!</b>\n\n"
        f"📝 {generate_display_title(title)}\n"
        f"📊 Qualities: {q_str}{fail_str}\n"
        f"🔗 Link: {bot_link}\n\n"
        f"{'📹 Trim video posted in free channel' if trim_type != 'skip' else '📹 First quality posted in free channel'}",
        parse_mode='HTML'
    )
    context.user_data.clear()
    return ConversationHandler.END

# ========== BULK UPLOAD ==========

async def start_bulk_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Access Denied!")
        return ConversationHandler.END

    if BACKUP_1 == 0:
        await update.message.reply_text("❌ BACKUP_CHANNEL_1 not set!")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data['bulk_videos'] = {}
    await update.message.reply_text(
        "📦 <b>BULK UPLOAD MODE</b>\n\n"
        "📹 Videos bhejte jao.\n"
        "📝 Caption = Title (auto-cleaned)\n"
        "📊 Same title = multiple qualities\n\n"
        "✅ Sab bhejo, phir <code>/done</code>\n"
        "❌ Cancel: /cancel",
        parse_mode='HTML'
    )
    return BULK_WAIT_VIDEO

async def process_bulk_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return BULK_WAIT_VIDEO

    if msg.text and msg.text.strip().lower() == '/done':
        return await finalize_bulk_upload(update, context)

    if msg.text and msg.text.strip().lower() == '/cancel':
        context.user_data.clear()
        await msg.reply_text("❌ Bulk cancelled.")
        return ConversationHandler.END

    if msg.text and msg.text.strip().lower() == '/start':
        context.user_data.clear()
        await msg.reply_text("🔄 Reset!")
        return ConversationHandler.END

    if not msg.video and not msg.document:
        await msg.reply_text("❌ Video/Document bhejo! Ya /done")
        return BULK_WAIT_VIDEO

    raw_caption = msg.caption if msg.caption else ""
    title = clean_title(raw_caption)
    video_obj = msg.video
    doc_obj = msg.document
    duration = 0
    quality_label, width, height, file_size = detect_quality_label(
        video_obj=video_obj, document_obj=doc_obj, caption=raw_caption
    )
    if video_obj:
        duration = video_obj.duration or 0

    thumb_id = None
    if video_obj and video_obj.thumbnail:
        thumb_id = video_obj.thumbnail.file_id
    elif doc_obj and doc_obj.thumbnail:
        thumb_id = doc_obj.thumbnail.file_id

    bulk_videos = context.user_data.get('bulk_videos', {})
    if title not in bulk_videos:
        bulk_videos[title] = []

    for existing in bulk_videos[title]:
        if existing['file_size'] == file_size and file_size > 0:
            await msg.reply_text(
                f"⚠️ Duplicate in '<b>{generate_display_title(title)}</b>': {quality_label} = {format_file_size(file_size)}",
                parse_mode='HTML'
            )
            return BULK_WAIT_VIDEO

    video_data = {
        'quality_label': quality_label,
        'width': width,
        'height': height,
        'file_size': file_size,
        'duration': duration,
        'chat_id': msg.chat_id,
        'msg_id': msg.message_id,
        'thumb_id': thumb_id
    }
    bulk_videos[title].append(video_data)
    context.user_data['bulk_videos'] = bulk_videos

    total_titles = len(bulk_videos)
    total_files = sum(len(v) for v in bulk_videos.values())
    await msg.reply_text(
        f"✅ Added: <b>{generate_display_title(title)}</b> [{quality_label} - {format_file_size(file_size)}]\n\n"
        f"📊 Total: {total_titles} titles, {total_files} files\n\n"
        f"📹 Aur bhejo ya <code>/done</code>",
        parse_mode='HTML'
    )
    return BULK_WAIT_VIDEO

async def finalize_bulk_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    bulk_videos = context.user_data.get('bulk_videos', {})
    if not bulk_videos:
        await msg.reply_text("❌ Koi video nahi! Bhejo phir /done.")
        return BULK_WAIT_VIDEO

    total_titles = len(bulk_videos)
    total_files = sum(len(v) for v in bulk_videos.values())
    status = await msg.reply_text(
        f"⏳ Processing {total_titles} videos ({total_files} files)...",
        parse_mode='HTML'
    )

    processed = 0
    results = []

    bot_username = PROVIDER_BOT_USERNAME if PROVIDER_BOT_USERNAME else "your_bot"
    buy_link = f"https://t.me/{bot_username}?start=buy"

    for title, video_list in bulk_videos.items():
        processed += 1
        await status.edit_text(
            f"⏳ {processed}/{total_titles}: {html_escape(generate_display_title(title))}...",
            parse_mode='HTML'
        )

        conn = None
        vid_id = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("INSERT INTO adult_videos (title) VALUES (%s) RETURNING vid_id", (title,))
            vid_id = cur.fetchone()[0]
            conn.commit()
            cur.close()
        except Exception as e:
            logger.error(f"DB error '{title}': {e}")
            results.append(f"❌ {generate_display_title(title)}: DB Error")
            continue
        finally:
            if conn:
                db_pool.putconn(conn)

        qualities_info = []
        for vdata in video_list:
            q_label = vdata['quality_label']
            backup_caption = build_backup_caption(title, q_label)

            file_url = None
            try:
                copied = await context.bot.copy_message(
                    chat_id=BACKUP_1, from_chat_id=vdata['chat_id'],
                    message_id=vdata['msg_id'], caption=backup_caption, parse_mode='HTML'
                )
                file_url = construct_file_url(BACKUP_1, copied.message_id)
                logger.info(f"Bulk backup: {title} {q_label} -> {file_url}")
            except Exception as e:
                logger.error(f"Backup1 FAILED {title} {q_label}: {e}")
                continue

            for ch_id in [ch for ch in [BACKUP_2, PAID_CH] if ch != 0]:
                try:
                    await context.bot.copy_message(
                        chat_id=ch_id, from_chat_id=vdata['chat_id'],
                        message_id=vdata['msg_id'], caption=backup_caption, parse_mode='HTML'
                    )
                except Exception as e:
                    logger.error(f"Channel {ch_id} error: {e}")

            conn2 = None
            try:
                conn2 = get_db_connection()
                cur2 = conn2.cursor()
                cur2.execute(
                    """INSERT INTO video_qualities
                       (vid_id, quality_label, file_url, file_size, width, height, duration)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (vid_id, q_label, file_url, vdata['file_size'],
                     vdata['width'], vdata['height'], vdata['duration'])
                )
                conn2.commit()
                cur2.close()
            except Exception as e:
                logger.error(f"DB quality save error: {e}")
            finally:
                if conn2:
                    db_pool.putconn(conn2)

            qualities_info.append({
                'label': q_label,
                'size': format_file_size(vdata['file_size']),
                'url': file_url
            })

        if not qualities_info:
            results.append(f"❌ {generate_display_title(title)}: All backups failed!")
            continue

        bot_link = f"https://t.me/{bot_username}?start=vid_{vid_id}"
        
        post_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Watch Now / Download 📥", url=bot_link)],
            [InlineKeyboardButton("💎 Buy VIP Subscription", url=buy_link)]
        ])

        caption = build_free_channel_caption(title, qualities_info)

        # 👇 Free channel post with thumbnail
        if FREE_CH != 0:
            first_vid = video_list[0]
            thumb_id = first_vid.get('thumb_id')
            if thumb_id:
                try:
                    await context.bot.send_photo(
                        chat_id=FREE_CH,
                        photo=thumb_id,
                        caption=caption,
                        parse_mode='HTML',
                        reply_markup=post_keyboard
                    )
                except Exception as e:
                    logger.error(f"Bulk photo post failed: {e}")
                    # fallback to text
                    try:
                        await context.bot.send_message(
                            chat_id=FREE_CH,
                            text=caption,
                            parse_mode='HTML',
                            reply_markup=post_keyboard
                        )
                    except Exception as e2:
                        logger.error(f"Bulk text fallback failed: {e2}")
            else:
                try:
                    await context.bot.send_message(
                        chat_id=FREE_CH,
                        text=caption,
                        parse_mode='HTML',
                        reply_markup=post_keyboard
                    )
                except Exception as e:
                    logger.error(f"Bulk text fallback failed: {e}")

        q_str = ", ".join([f"{q['label']}({q['size']})" for q in qualities_info])
        results.append(f"✅ {generate_display_title(title)}: {q_str}")
        await asyncio.sleep(1)

    result_text = "\n".join(results)
    await status.edit_text(
        f"🎉 <b>BULK COMPLETE!</b>\n\n"
        f"📊 {total_titles} videos, {total_files} files\n\n"
        f"<b>Results:</b>\n{result_text}",
        parse_mode='HTML'
    )
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_admin_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ================================================================
#            PROVIDER BOT (USER-FACING)
# ================================================================

async def provider_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('payment_step', None)
    context.user_data.pop('screenshot_id', None)
    context.user_data.pop('qr_expiry', None)
    context.user_data.pop('payment_note', None)

    text = update.message.text
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_name = user.first_name

    logger.info(f"Provider /start from user {user.id} ({user_name}): {text}")

    # 👇 Handle buy parameter
    if text and "buy" in text:
        # Directly trigger buy flow
        await provider_handle_buy(update, context)
        return

    if text and "vid_" in text:
        try:
            vid_id = int(text.split("vid_")[1])
            conn = None
            title = None
            qualities = []
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("SELECT title FROM adult_videos WHERE vid_id = %s", (vid_id,))
                video_result = cur.fetchone()
                if video_result:
                    title = video_result[0]
                    cur.execute(
                        """SELECT quality_id, quality_label, file_url, file_size
                           FROM video_qualities WHERE vid_id = %s
                           ORDER BY file_size DESC""",
                        (vid_id,)
                    )
                    qualities = cur.fetchall()
                cur.close()
            finally:
                if conn:
                    db_pool.putconn(conn)

            if not title:
                err = await update.message.reply_text(
                    "❌ Video Not Found!\n\nYeh video delete ho chuki hai ya invalid link hai."
                )
                asyncio.create_task(schedule_delete(context, chat_id, err.message_id, TEXT_DELETE_TIME))
                asyncio.create_task(schedule_delete(context, chat_id, update.message.message_id, TEXT_DELETE_TIME))
                return
            if not qualities:
                err = await update.message.reply_text(
                    f"❌ No video files found. Contact @{ADMIN_USERNAME}"
                )
                asyncio.create_task(schedule_delete(context, chat_id, err.message_id, TEXT_DELETE_TIME))
                asyncio.create_task(schedule_delete(context, chat_id, update.message.message_id, TEXT_DELETE_TIME))
                return

            asyncio.create_task(schedule_delete(context, chat_id, update.message.message_id, 5))

            if len(qualities) == 1:
                await send_video_to_user(
                    update, context, chat_id, user_name, title, qualities[0]
                )
                return

            keyboard = []
            for q in qualities:
                q_id, q_label, file_url, file_size = q
                size_str = format_file_size(file_size)
                keyboard.append(
                    [InlineKeyboardButton(
                        f"📹 {q_label} ({size_str})",
                        callback_data=f"quality_{vid_id}_{q_id}"
                    )]
                )
            keyboard.append(
                [InlineKeyboardButton(
                    "📦 Download All Qualities",
                    callback_data=f"allquality_{vid_id}"
                )]
            )

            selection_msg = await update.message.reply_text(
                f"👋 Hello <b>{html_escape(user_name)}</b>!\n\n"
                f"🎬 <b>{html_escape(title)}</b>\n\n"
                f"📊 <b>Select Quality:</b>\n\n"
                f"⚠️ Videos auto-delete after 5 minutes!\n"
                f"💾 Forward to Saved Messages immediately!",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            asyncio.create_task(schedule_delete(context, chat_id, selection_msg.message_id, TEXT_DELETE_TIME))

        except ValueError:
            err = await update.message.reply_text("❌ Invalid video ID.")
            asyncio.create_task(schedule_delete(context, chat_id, err.message_id, TEXT_DELETE_TIME))
        except Exception as e:
            logger.error(f"Provider Error: {e}")
            err = await update.message.reply_text("❌ Something went wrong. Try again.")
            asyncio.create_task(schedule_delete(context, chat_id, err.message_id, TEXT_DELETE_TIME))
        return

    sub_status = ""
    is_active, end_date = check_active_subscription(user.id)
    if is_active and end_date:
        remaining = (end_date - datetime.now()).days
        sub_status = (
            f"\n\n✅ <b>Active Subscription!</b>\n"
            f"📅 Expires: {end_date.strftime('%d-%m-%Y')}\n"
            f"⏳ {remaining} days remaining"
        )
    elif end_date:
        sub_status = "\n\n⚠️ <b>Subscription Expired!</b> Renew karo neeche se 👇"

    keyboard = [
        [KeyboardButton("💎 Buy VIP")],
        [KeyboardButton("🆓 Free Channel"), KeyboardButton("👨‍💻 Contact Admin")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)

    welcome = await update.message.reply_text(
        f"🔞 <b>Welcome {html_escape(user_name)}!</b>\n\n"
        f"🎬 Premium Videos sirf {SUBSCRIPTION_AMOUNT}₹/month mein.\n\n"
        f"📌 <b>Features:</b>\n"
        f"  • Direct video files without ads\n"
        f"  • All qualities available\n"
        f"  • Priority support"
        f"{sub_status}\n\n"
        f"👇 Neeche menu se option select karein:",
        parse_mode='HTML',
        reply_markup=reply_markup
    )
    asyncio.create_task(schedule_delete(context, chat_id, welcome.message_id, TEXT_DELETE_TIME))
    asyncio.create_task(schedule_delete(context, chat_id, update.message.message_id, TEXT_DELETE_TIME))

async def provider_handle_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Direct buy flow triggered by /start buy"""
    msg = update.message
    chat_id = msg.chat_id
    user = update.effective_user
    user_name = user.first_name

    # Check if already subscribed
    is_active, end_date = check_active_subscription(user.id)
    if is_active and end_date:
        remaining = (end_date - datetime.now()).days
        active_msg = await msg.reply_text(
            f"✅ <b>Tumhari subscription already active hai!</b>\n\n"
            f"📅 Expires: {end_date.strftime('%d-%m-%Y')}\n"
            f"⏳ {remaining} days remaining",
            parse_mode='HTML'
        )
        asyncio.create_task(schedule_delete(context, chat_id, active_msg.message_id, TEXT_DELETE_TIME))
        return

    amount = SUBSCRIPTION_AMOUNT
    context.user_data['payment_step'] = 'screenshot'

    try:
        if QR_AVAILABLE:
            qr_image, note = generate_upi_qr(user.id, user_name, amount)
            qr_validity_minutes = 10
            expiry_time = datetime.now() + timedelta(minutes=qr_validity_minutes)
            context.user_data['qr_expiry'] = expiry_time
            context.user_data['payment_note'] = note

            qr_msg = await msg.reply_photo(
                photo=qr_image,
                caption=(
                    f"💎 <b>VIP Subscription - {amount}₹ / Month</b>\n\n"
                    f"📱 <b>Scan QR Code</b> from any UPI app:\n"
                    f"  • Google Pay / PhonePe / Paytm\n\n"
                    f"💰 Amount: <b>₹{amount}</b> (pre-filled)\n"
                    f"📝 Note: <code>{note}</code> (auto-filled)\n\n"
                    f"⚠️ <b>Important:</b>\n"
                    f"  • Amount/Note change MAT karna\n"
                    f"  • QR valid for <b>{qr_validity_minutes} min</b>\n"
                    f"  • Expiry: <b>{expiry_time.strftime('%H:%M:%S')}</b>\n\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"✅ Payment ke baad:\n"
                    f"1️⃣ <b>Screenshot</b> bhejo\n"
                    f"2️⃣ Phir <b>UTR Number</b> type karke bhejo\n\n"
                    f"📸 <b>Payment karo aur screenshot bhejo...</b>\n\n"
                    f"❌ Cancel: /cancel"
                ),
                parse_mode='HTML'
            )
            asyncio.create_task(schedule_delete(context, chat_id, qr_msg.message_id, QR_DELETE_TIME))
        else:
            raise Exception("qrcode library not available")

    except Exception as e:
        logger.error(f"QR generation failed: {e}")
        safe_name = re.sub(r'[^a-zA-Z0-9 ]', '', user_name)[:30].strip()
        if not safe_name: 
            safe_name = "User"
        note = f"TG-{user.id}-{safe_name}"
        context.user_data['payment_note'] = note

        fallback_msg = await msg.reply_text(
            f"💎 <b>VIP Subscription - {amount}₹ / Month</b>\n\n"
            f"💳 <b>UPI ID:</b> <code>{UPI_ID}</code>\n"
            f"💰 <b>Amount:</b> ₹{amount}\n"
            f"📝 <b>Note mein likho:</b> <code>{note}</code>\n\n"
            f"⚠️ <b>Steps:</b>\n"
            f"1️⃣ UPI par ₹{amount} pay karo\n"
            f"2️⃣ Note mein <code>{note}</code> likho\n"
            f"3️⃣ Screenshot bhejo\n"
            f"4️⃣ UTR number bhejo\n\n"
            f"❌ Cancel: /cancel",
            parse_mode='HTML'
        )
        asyncio.create_task(schedule_delete(context, chat_id, fallback_msg.message_id, QR_DELETE_TIME))
    
    asyncio.create_task(schedule_delete(context, chat_id, msg.message_id, TEXT_DELETE_TIME))

async def provider_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('payment_step', None)
    context.user_data.pop('screenshot_id', None)
    context.user_data.pop('qr_expiry', None)
    context.user_data.pop('payment_note', None)
    chat_id = update.effective_chat.id
    cancel_msg = await update.message.reply_text(
        "❌ <b>Cancelled!</b>\n\nDobara /start type karein.",
        parse_mode='HTML'
    )
    asyncio.create_task(schedule_delete(context, chat_id, cancel_msg.message_id, TEXT_DELETE_TIME))
    asyncio.create_task(schedule_delete(context, chat_id, update.message.message_id, TEXT_DELETE_TIME))

async def provider_handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id
    user = query.from_user
    user_name = user.first_name

    logger.info(f"Callback from {user.id}: {data}")

    if data.startswith("quality_"):
        parts = data.split("_")
        vid_id = int(parts[1])
        quality_id = int(parts[2])
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT title FROM adult_videos WHERE vid_id = %s", (vid_id,))
            vid_result = cur.fetchone()
            title = vid_result[0] if vid_result else "Unknown"
            cur.execute(
                """SELECT quality_id, quality_label, file_url, file_size
                   FROM video_qualities WHERE quality_id = %s""",
                (quality_id,)
            )
            quality = cur.fetchone()
            cur.close()
        finally:
            if conn:
                db_pool.putconn(conn)

        if not quality:
            await query.edit_message_text("❌ Quality not found!")
            return

        try:
            await query.message.delete()
        except:
            pass

        await send_video_to_user(
            update, context, chat_id, user_name, title, quality, is_callback=True
        )
        return

    if data.startswith("allquality_"):
        vid_id = int(data.split("_")[1])
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT title FROM adult_videos WHERE vid_id = %s", (vid_id,))
            vid_result = cur.fetchone()
            title = vid_result[0] if vid_result else "Unknown"
            cur.execute(
                """SELECT quality_id, quality_label, file_url, file_size
                   FROM video_qualities WHERE vid_id = %s ORDER BY file_size ASC""",
                (vid_id,)
            )
            qualities = cur.fetchall()
            cur.close()
        finally:
            if conn:
                db_pool.putconn(conn)

        if not qualities:
            await query.edit_message_text("❌ No qualities found!")
            return

        try:
            await query.message.delete()
        except:
            pass

        all_sent_ids = []
        for quality in qualities:
            msg_ids = await send_video_to_user(
                update, context, chat_id, user_name, title, quality,
                is_callback=True, return_msg_id=True
            )
            if msg_ids:
                if isinstance(msg_ids, list):
                    all_sent_ids.extend(msg_ids)
                else:
                    all_sent_ids.append(msg_ids)
            await asyncio.sleep(1)

        if all_sent_ids:
            asyncio.create_task(
                auto_delete_with_notification(
                    context=context, chat_id=chat_id,
                    message_ids_to_delete=all_sent_ids, delete_time=AUTO_DELETE_TIME
                )
            )
        return

    if data.startswith("approve_"):
        if user.id != ADMIN_USER_ID:
            await query.answer("❌ Only admin!", show_alert=True)
            return
        parts = data.split("_")
        target_user_id = int(parts[1])
        days = int(parts[2])
        end_date = datetime.now() + timedelta(days=days)
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO subscribers (user_id, end_date, notified)
                VALUES (%s, %s, FALSE)
                ON CONFLICT (user_id)
                DO UPDATE SET end_date = %s, notified = FALSE, start_date = CURRENT_TIMESTAMP
            """, (target_user_id, end_date, end_date))
            conn.commit()
            cur.close()
            logger.info(f"Subscriber {target_user_id} approved for {days} days")
        except Exception as e:
            logger.error(f"DB approve error: {e}")
            await query.edit_message_caption(
                caption=query.message.caption + f"\n\n❌ DB ERROR: {e}",
                parse_mode='HTML'
            )
            return
        finally:
            if conn:
                db_pool.putconn(conn)

        await query.edit_message_caption(
            caption=query.message.caption + (
                f"\n\n✅ <b>APPROVED</b> for {days} days!\n"
                f"📅 Till: {end_date.strftime('%d-%m-%Y')}"
            ),
            parse_mode='HTML'
        )

        try:
            if PAID_CH != 0:
                invite_link = await context.bot.create_chat_invite_link(
                    chat_id=PAID_CH, member_limit=1,
                    expire_date=datetime.now() + timedelta(days=1),
                    name=f"VIP-{target_user_id}"
                )
                await context.bot.send_message(
                    chat_id=target_user_id,
                    text=(
                        f"🎉 <b>Payment Approved!</b>\n\n"
                        f"📅 Plan: {days} Days VIP\n"
                        f"📅 Valid Till: {end_date.strftime('%d-%m-%Y')}\n\n"
                        f"👇 <b>VIP Group Join Link:</b>\n"
                        f"{invite_link.invite_link}\n\n"
                        f"⚠️ Link sirf EK BAAR kaam karega. 24hr mein expire hoga.\n\n"
                        f"🙏 Thank you!"
                    ),
                    parse_mode='HTML'
                )
            else:
                await context.bot.send_message(
                    chat_id=target_user_id,
                    text=(
                        f"🎉 <b>Payment Approved!</b>\n\n"
                        f"📅 Plan: {days} Days VIP\n"
                        f"Admin se link lein: @{ADMIN_USERNAME}"
                    ),
                    parse_mode='HTML'
                )
        except Exception as e:
            logger.error(f"Invite link error: {e}")
        return

    if data.startswith("reject_"):
        if user.id != ADMIN_USER_ID:
            await query.answer("❌ Only admin!", show_alert=True)
            return
        target_user_id = int(data.split("_")[1])
        await query.edit_message_caption(
            caption=query.message.caption + "\n\n❌ <b>REJECTED</b>",
            parse_mode='HTML'
        )
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=(
                    "❌ <b>Payment Rejected!</b>\n\n"
                    "Screenshot ya UTR invalid tha.\n\n"
                    f"🔁 Dobara try: /start\n"
                    f"❓ Help: @{ADMIN_USERNAME}"
                ),
                parse_mode='HTML'
            )
        except:
            pass
        return

async def provider_handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = msg.chat_id
    payment_step = context.user_data.get('payment_step')

    if payment_step == 'screenshot':
        qr_expiry = context.user_data.get('qr_expiry')
        if qr_expiry and datetime.now() > qr_expiry:
            context.user_data.pop('payment_step', None)
            context.user_data.pop('qr_expiry', None)
            context.user_data.pop('payment_note', None)
            expired_msg = await msg.reply_text(
                "❌ <b>QR Code Expired!</b>\n\n"
                "⏰ Time khatam ho gaya.\n"
                "🔁 Naya QR lene ke liye /start → Buy VIP dabao.\n\n"
                f"⚠️ Agar payment ho gaya hai toh contact karo: @{ADMIN_USERNAME}",
                parse_mode='HTML'
            )
            asyncio.create_task(schedule_delete(context, chat_id, expired_msg.message_id, TEXT_DELETE_TIME))
            asyncio.create_task(schedule_delete(context, chat_id, msg.message_id, TEXT_DELETE_TIME))
            return

        context.user_data['screenshot_id'] = msg.photo[-1].file_id
        context.user_data['payment_step'] = 'utr'

        receipt = await msg.reply_text(
            "✅ <b>Screenshot Received!</b>\n\n"
            "🔢 Ab <b>UTR ya Reference Number</b> type karke bhejein.\n\n"
            "💡 UTR number payment SMS ya app mein milta hai.\n\n"
            "❌ Cancel: /cancel",
            parse_mode='HTML'
        )
        asyncio.create_task(schedule_delete(context, chat_id, receipt.message_id, TEXT_DELETE_TIME))
        asyncio.create_task(schedule_delete(context, chat_id, msg.message_id, TEXT_DELETE_TIME))
        return

    err = await msg.reply_text(
        "📸 Photo received, lekin koi active process nahi hai.\n\n"
        "👉 /start type karein menu dekhne ke liye."
    )
    asyncio.create_task(schedule_delete(context, chat_id, err.message_id, TEXT_DELETE_TIME))
    asyncio.create_task(schedule_delete(context, chat_id, msg.message_id, TEXT_DELETE_TIME))

async def provider_handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = msg.chat_id
    text = msg.text.strip()
    payment_step = context.user_data.get('payment_step')
    user = update.effective_user
    user_name = user.first_name

    if text == "💎 Buy VIP":
        await provider_handle_buy(update, context)
        return

    elif text == "🆓 Free Channel":
        info_msg = await msg.reply_text(f"🆓 Join our Free Channel here:\n👉 {FREE_CHANNEL_LINK}")
        asyncio.create_task(schedule_delete(context, chat_id, info_msg.message_id, TEXT_DELETE_TIME))
        asyncio.create_task(schedule_delete(context, chat_id, msg.message_id, TEXT_DELETE_TIME))
        return

    elif text == "👨‍💻 Contact Admin":
        info_msg = await msg.reply_text(f"👨‍💻 Admin se yahan baat karein:\n👉 @{ADMIN_USERNAME}")
        asyncio.create_task(schedule_delete(context, chat_id, info_msg.message_id, TEXT_DELETE_TIME))
        asyncio.create_task(schedule_delete(context, chat_id, msg.message_id, TEXT_DELETE_TIME))
        return

    if payment_step == 'utr':
        utr_number = msg.text.strip()
        if len(utr_number) < 4:
            short_msg = await msg.reply_text(
                "❌ UTR number bahut chota hai. Sahi UTR bhejein.\n❌ Cancel: /cancel"
            )
            asyncio.create_task(schedule_delete(context, chat_id, short_msg.message_id, TEXT_DELETE_TIME))
            asyncio.create_task(schedule_delete(context, chat_id, msg.message_id, TEXT_DELETE_TIME))
            return

        screenshot_id = context.user_data.get('screenshot_id')
        payment_note = context.user_data.get('payment_note', 'N/A')
        username_text = f"@{user.username}" if user.username else "N/A"

        admin_keyboard = [
            [
                InlineKeyboardButton("✅ Approve (30 Days)", callback_data=f"approve_{user.id}_30"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user.id}")
            ],
            [InlineKeyboardButton("✅ Approve (7 Days Trial)", callback_data=f"approve_{user.id}_7")]
        ]

        try:
            await context.bot.send_photo(
                chat_id=ADMIN_USER_ID,
                photo=screenshot_id,
                caption=(
                    f"🔔 <b>NEW PAYMENT PENDING</b>\n\n"
                    f"👤 Name: {html_escape(user.first_name)}\n"
                    f"🆔 ID: <code>{user.id}</code>\n"
                    f"📱 Username: {username_text}\n"
                    f"🔢 UTR: <code>{utr_number}</code>\n"
                    f"📝 UPI Note: <code>{payment_note}</code>\n"
                    f"💰 Amount: {SUBSCRIPTION_AMOUNT}₹\n"
                    f"📅 Date: {datetime.now().strftime('%d-%m-%Y %H:%M')}\n\n"
                    f"💡 UPI mein <code>{payment_note}</code> search karo\n\n"
                    f"👇 Verify karke approve/reject karein:"
                ),
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(admin_keyboard)
            )
        except Exception as e:
            logger.error(f"Failed to send payment to admin: {e}")
            err = await msg.reply_text(f"❌ Error! Please try again or contact @{ADMIN_USERNAME}")
            context.user_data.pop('payment_step', None)
            context.user_data.pop('screenshot_id', None)
            context.user_data.pop('qr_expiry', None)
            context.user_data.pop('payment_note', None)
            asyncio.create_task(schedule_delete(context, chat_id, err.message_id, TEXT_DELETE_TIME))
            asyncio.create_task(schedule_delete(context, chat_id, msg.message_id, TEXT_DELETE_TIME))
            return

        pending_msg = await msg.reply_text(
            "⏳ <b>Verification Pending!</b>\n\n"
            "✅ Payment details admin ko bhej di gayi.\n"
            "🕒 Admin verify karte hi VIP link mil jayega.\n\n"
            "⏱️ Usually 5-30 minutes lagta hai.\n\n"
            f"❓ Problem? @{ADMIN_USERNAME}",
            parse_mode='HTML'
        )
        asyncio.create_task(schedule_delete(context, chat_id, pending_msg.message_id, TEXT_DELETE_TIME))
        asyncio.create_task(schedule_delete(context, chat_id, msg.message_id, TEXT_DELETE_TIME))

        context.user_data.pop('payment_step', None)
        context.user_data.pop('screenshot_id', None)
        context.user_data.pop('qr_expiry', None)
        context.user_data.pop('payment_note', None)
        return

    if payment_step == 'screenshot':
        photo_err = await msg.reply_text(
            "❌ Photo chahiye! Payment ka <b>Screenshot (photo)</b> bhejein.\n\n"
            "❌ Cancel: /cancel",
            parse_mode='HTML'
        )
        asyncio.create_task(schedule_delete(context, chat_id, photo_err.message_id, TEXT_DELETE_TIME))
        asyncio.create_task(schedule_delete(context, chat_id, msg.message_id, TEXT_DELETE_TIME))
        return

    normal_err = await msg.reply_text(
        "🤔 Samajh nahi aaya.\n\n"
        "👉 Niche diye gaye menu buttons ka istemal karein.\n"
        "👉 Agar video link chahiye toh Free channel se link copy karein."
    )
    asyncio.create_task(schedule_delete(context, chat_id, normal_err.message_id, TEXT_DELETE_TIME))
    asyncio.create_task(schedule_delete(context, chat_id, msg.message_id, TEXT_DELETE_TIME))

# ================================================================
#  SEND VIDEO TO USER
# ================================================================

async def send_video_to_user(update, context, chat_id, user_name, title,
                              quality_data, is_callback=False, return_msg_id=False):
    q_id, q_label, file_url, file_size = quality_data
    size_str = format_file_size(file_size)

    is_old_file_id = not file_url.startswith("https://t.me/c/") if file_url else False

    try:
        warning_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=(f"👋 <b>Hello {html_escape(user_name)}!</b>\n\n"
                  f"⏳ Tumhari file (<b>{q_label}</b>) bheji ja rahi hai..."),
            parse_mode='HTML'
        )
        asyncio.create_task(schedule_delete(context, chat_id, warning_msg.message_id, 10))
    except: 
        pass

    caption_text = (
        f"🎬 <b>{html_escape(title)}</b>\n\n"
        f"📊 <b>Quality:</b> {q_label} ({size_str})\n"
        f"⚡ <b>Fast Direct Download Server</b>\n\n"
        f"⚠️ <b>IMPORTANT NOTICE:</b>\n"
        f"Yeh file automatically <b>5 minutes</b> mein delete ho jayegi.\n"
        f"💾 <b>Jaldi se 'Saved Messages' mein forward kar lo!</b>\n\n"
        f"💎 Ads-free experience ke liye VIP lein.\n"
        f"👨‍💻 <b>Owner:</b> @{ADMIN_USERNAME}"
    )

    join_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Buy VIP Access", url=f"https://t.me/{PROVIDER_BOT_USERNAME}?start=buy")],
        [InlineKeyboardButton("🆓 Join Free Channel", url=FREE_CHANNEL_LINK)]
    ])

    sent_msg_id = None

    if is_old_file_id:
        try:
            fallback = await context.bot.send_video(
                chat_id=chat_id, video=file_url, caption=caption_text, 
                parse_mode='HTML', reply_markup=join_keyboard, supports_streaming=True
            )
            sent_msg_id = fallback.message_id
        except: 
            pass
    else:
        backup_channel_id, backup_msg_id = parse_file_url(file_url)
        if backup_channel_id and backup_msg_id:
            try:
                copied = await context.bot.copy_message(
                    chat_id=chat_id, from_chat_id=backup_channel_id, message_id=backup_msg_id,
                    caption=caption_text, parse_mode='HTML', reply_markup=join_keyboard
                )
                sent_msg_id = copied.message_id
            except: 
                pass

    if sent_msg_id and not return_msg_id:
        asyncio.create_task(
            auto_delete_with_notification(
                context=context, chat_id=chat_id,
                message_ids_to_delete=[sent_msg_id], delete_time=AUTO_DELETE_TIME
            )
        )

    return sent_msg_id

# ================= BACKGROUND TASKS =================

async def periodic_cleanup(context):
    while True:
        await asyncio.sleep(3600)
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("DELETE FROM adult_videos WHERE created_at < NOW() - INTERVAL '7 days'")
            deleted = cur.rowcount
            conn.commit()
            cur.close()
            if deleted > 0:
                logger.info(f"Cleaned {deleted} old records")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        finally:
            if conn:
                db_pool.putconn(conn)

async def notify_expired_subs(provider_app_instance: Application):
    await asyncio.sleep(60)
    logger.info("Subscription expiry checker started")
    while True:
        users_to_notify = []
        conn = None
        
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                SELECT user_id, end_date FROM subscribers 
                WHERE end_date < NOW() + INTERVAL '2 days' 
                AND end_date > NOW() - INTERVAL '7 days' 
                AND notified = FALSE
            """)
            users_to_notify = cur.fetchall()
            cur.close()
        except Exception as e:
            logger.error(f"Expiry check DB error: {e}")
        finally:
            if conn:
                db_pool.putconn(conn)

        for (user_id, end_date) in users_to_notify:
            is_expired = end_date < datetime.now()
            if is_expired:
                msg_text = (
                    "⚠️ <b>Subscription Expired!</b>\n\n"
                    f"📅 Expired on: {end_date.strftime('%d-%m-%Y')}\n\n"
                    "🔁 Renew: /start → Buy Subscription\n"
                    f"❓ Help: @{ADMIN_USERNAME}"
                )
            else:
                remaining = (end_date - datetime.now()).days
                msg_text = (
                    "⚠️ <b>Subscription Expiry Alert!</b>\n\n"
                    f"📅 Expires in <b>{remaining} days</b> ({end_date.strftime('%d-%m-%Y')})\n\n"
                    "🔁 Renew now: /start → Buy Subscription\n"
                    f"❓ Help: @{ADMIN_USERNAME}"
                )
                
            try:
                await provider_app_instance.bot.send_message(
                    chat_id=user_id, text=msg_text, parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Notify user {user_id} error: {e}")

            conn2 = None
            try:
                conn2 = get_db_connection()
                cur2 = conn2.cursor()
                cur2.execute("UPDATE subscribers SET notified = TRUE WHERE user_id = %s", (user_id,))
                conn2.commit()
                cur2.close()
            except Exception as e:
                logger.error(f"Update notified status error: {e}")
            finally:
                if conn2:
                    db_pool.putconn(conn2)

            await asyncio.sleep(2)

        await asyncio.sleep(43200)

# ================================================================
#                    RUN BOTH BOTS
# ================================================================

async def run_bots():
    print("\n🤖 Initializing bots...")
    
    if not MAIN_BOT_TOKEN:
        logger.error("MAIN_BOT_TOKEN not found!")
        return
    if not PROVIDER_BOT_TOKEN:
        logger.error("PROVIDER_BOT_TOKEN not found!")
        return

    if BACKUP_1 == 0:
        logger.warning("BACKUP_CHANNEL_1 not set! File URL mode won't work!")

    print("✅ Tokens verified")

    # ============ MAIN BOT ============
    print("\n⚙️  Configuring Main Bot (Admin)...")
    main_app = Application.builder().token(MAIN_BOT_TOKEN).build()

    upload_conv = ConversationHandler(
        entry_points=[CommandHandler('post', start_upload)],
        states={
            WAIT_TRIM: [
                CommandHandler('skip', get_trim),
                CommandHandler('cancel', cancel_admin_flow),
                CommandHandler('start', admin_start),
                MessageHandler(filters.ALL & ~filters.COMMAND, get_trim),
            ],
            WAIT_FULL: [
                CommandHandler('done', get_full_and_process),
                CommandHandler('cancel', cancel_admin_flow),
                CommandHandler('start', admin_start),
                MessageHandler(filters.ALL & ~filters.COMMAND, get_full_and_process),
            ]
        },
        fallbacks=[
            CommandHandler('cancel', cancel_admin_flow),
            CommandHandler('start', admin_start),
        ],
        allow_reentry=True
    )
    main_app.add_handler(upload_conv)

    bulk_conv = ConversationHandler(
        entry_points=[CommandHandler('bulk', start_bulk_upload)],
        states={
            BULK_WAIT_VIDEO: [
                CommandHandler('done', process_bulk_video),
                CommandHandler('cancel', cancel_admin_flow),
                CommandHandler('start', admin_start),
                MessageHandler(filters.ALL & ~filters.COMMAND, process_bulk_video),
            ]
        },
        fallbacks=[
            CommandHandler('cancel', cancel_admin_flow),
            CommandHandler('start', admin_start),
        ],
        allow_reentry=True
    )
    main_app.add_handler(bulk_conv)
    main_app.add_handler(CommandHandler('start', admin_start))
    print("✅ Main Bot handlers configured")

    # ============ PROVIDER BOT ============
    print("\n⚙️  Configuring Provider Bot (User)...")
    provider_app = Application.builder().token(PROVIDER_BOT_TOKEN).build()
    provider_app.add_handler(CommandHandler('start', provider_start))
    provider_app.add_handler(CommandHandler('cancel', provider_cancel))
    provider_app.add_handler(CallbackQueryHandler(provider_handle_callback))
    provider_app.add_handler(MessageHandler(filters.PHOTO, provider_handle_photo))
    provider_app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, provider_handle_text
    ))
    print("✅ Provider Bot handlers configured")

    # ============ START BOTH ============
    try:
        print("\n🚀 Starting Main Bot...")
        await main_app.initialize()
        await main_app.start()
        await main_app.updater.start_polling()
        print("✅ Main Bot RUNNING!")

        print("\n🚀 Starting Provider Bot...")
        await provider_app.initialize()
        await provider_app.start()
        await provider_app.updater.start_polling()
        print("✅ Provider Bot RUNNING!")

        print("\n" + "=" * 60)
        print("🎉 BOTH BOTS RUNNING SUCCESSFULLY!")
        print("=" * 60)
        print(f"👤 Admin User ID: {ADMIN_USER_ID}")
        print(f"👤 Admin Username: @{ADMIN_USERNAME}")
        print(f"🤖 Provider Bot: @{PROVIDER_BOT_USERNAME}")
        print(f"📦 Backup Channel: {BACKUP_1}")
        print(f"🆓 Free Channel: {FREE_CH if FREE_CH != 0 else 'Not Set'}")
        print(f"💎 Paid Channel: {PAID_CH if PAID_CH != 0 else 'Not Set'}")
        print(f"🕒 Video Auto-Delete: {AUTO_DELETE_TIME}s")
        print(f"🕒 Text Auto-Delete: {TEXT_DELETE_TIME}s")
        print(f"🕒 QR Auto-Delete: {QR_DELETE_TIME}s")
        print(f"📱 QR Code: {'Available' if QR_AVAILABLE else 'Text Fallback'}")
        print(f"💰 Subscription: ₹{SUBSCRIPTION_AMOUNT}/month")
        print("=" * 60)

        print("\n🔄 Starting background tasks...")
        asyncio.create_task(periodic_cleanup(None))
        asyncio.create_task(notify_expired_subs(provider_app))
        print("✅ Background tasks started")

        print("\n✨ System ready! Waiting for commands...\n")

        while True:
            await asyncio.sleep(3600)

    except Exception as e:
        logger.error(f"❌ Runtime error: {e}", exc_info=True)
        raise
    finally:
        print("\n🛑 Shutting down bots...")
        try:
            await main_app.updater.stop()
            await main_app.stop()
            await main_app.shutdown()
        except:
            pass
        try:
            await provider_app.updater.stop()
            await provider_app.stop()
            await provider_app.shutdown()
        except:
            pass
        print("✅ Shutdown complete")

# ================================================================
#                    MAIN ENTRY POINT
# ================================================================

if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("🔍 PRE-FLIGHT CHECKS")
    print("=" * 60)
    
    # Check required env vars
    required = ['MAIN_BOT_TOKEN', 'PROVIDER_BOT_TOKEN', 'DATABASE_URL']
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"❌ ERROR: Missing environment variables: {', '.join(missing)}")
        print("\n📝 Set these in Render dashboard → Environment tab")
        exit(1)
    print("✅ Required environment variables present")

    # Check backup channel
    if BACKUP_1 == 0:
        print("❌ ERROR: BACKUP_CHANNEL_1 is REQUIRED!")
        print("\n📝 Set BACKUP_CHANNEL_1 env variable (e.g. -1002683355160)")
        print("⚠️  Both bots must be admin of this channel!")
        exit(1)
    print(f"✅ Backup channel configured: {BACKUP_1}")

    # Initialize database
    print("\n📊 Initializing database...")
    try:
        init_db_pool()
        setup_db()
        print("✅ Database ready")
    except Exception as e:
        print(f"❌ DATABASE ERROR: {e}")
        logger.error(f"DB init failed: {e}", exc_info=True)
        exit(1)

    # Start Flask
    print("\n🌐 Starting web server...")
    try:
        flask_thread = Thread(target=run_flask, daemon=True)
        flask_thread.start()
        print(f"✅ Web server started on port {os.environ.get('PORT', 8080)}")
    except Exception as e:
        print(f"❌ FLASK ERROR: {e}")
        exit(1)

    # Run bots
    print("\n" + "=" * 60)
    print("🚀 LAUNCHING TELEGRAM BOTS")
    print("=" * 60)
    
    try:
        asyncio.run(run_bots())
    except KeyboardInterrupt:
        print("\n\n🛑 Interrupted by user")
    except Exception as e:
        print(f"\n\n❌ FATAL ERROR: {e}")
        logger.error(f"Fatal error: {e}", exc_info=True)
        import traceback
        traceback.print_exc()
        exit(1)
