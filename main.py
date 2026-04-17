import os
import re
import sys
import asyncio
import logging
import aiohttp
import psycopg2
from urllib.parse import quote
from telegram import InputMediaPhoto
from html import escape as html_escape
from psycopg2 import pool
from flask import Flask, redirect
from threading import Thread
from datetime import datetime, timedelta, time as dt_time
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

# PIL import with fallback
try:
    from PIL import Image, ImageEnhance, ImageFilter, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    print("⚠️ PIL/Pillow not available - thumbnail enhancement disabled")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

print("=" * 60)
print("🚀 APPLICATION STARTING - ENHANCED v3.0...")
print("=" * 60)

# ================= ENVIRONMENT VARIABLES =================
print("\n📋 Loading environment variables...")

MAIN_BOT_TOKEN = os.environ.get("MAIN_BOT_TOKEN")
PROVIDER_BOT_TOKEN = os.environ.get("PROVIDER_BOT_TOKEN")
PROVIDER_BOT_USERNAME = os.environ.get("PROVIDER_BOT_USERNAME", "").replace("@", "")
GPLINKS_API_KEY = os.environ.get("GPLINKS_API_KEY")

DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    logger.info("✅ Fixed DATABASE_URL format for psycopg2")

WEB_DOMAIN = os.environ.get("WEB_DOMAIN", "https://my-bot.onrender.com").strip()
ADMIN_USER_IDS = [int(uid.strip()) for uid in os.environ.get("ADMIN_USER_IDS", "").split(",") if uid.strip()]
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "ownermahi")

FREE_CH = int(os.environ.get("FREE_CHANNEL_ID", "0"))
PAID_CH = int(os.environ.get("PAID_CHANNEL_ID", "0"))
BACKUP_1 = int(os.environ.get("BACKUP_CHANNEL_1", "0"))
BACKUP_2 = int(os.environ.get("BACKUP_CHANNEL_2", "0"))
CP_CHANNEL_ID = int(os.environ.get("CP_CHANNEL_ID", "0"))

AUTO_DELETE_TIME = int(os.environ.get("AUTO_DELETE_TIME", "300"))
TEXT_DELETE_TIME = int(os.environ.get("TEXT_DELETE_TIME", "120"))
QR_DELETE_TIME = int(os.environ.get("QR_DELETE_TIME", "600"))
UPI_ID = os.environ.get("UPI_ID", "tumhara@upi")
FREE_CHANNEL_LINK = os.environ.get("FREE_CHANNEL_LINK", "https://t.me/+wcYoTQhIz-ZmOTY1")
SUBSCRIPTION_AMOUNT = os.environ.get("SUBSCRIPTION_AMOUNT", "10")

# Sticker settings (env se bhi le sakte ho)
STICKER_CHAT_ID = int(os.environ.get("STICKER_CHAT_ID", "-1003576065127"))
STICKER_MSG_ID = int(os.environ.get("STICKER_MSG_ID", "344"))

# Telegram caption limits
PHOTO_CAPTION_LIMIT = 1024
MESSAGE_TEXT_LIMIT = 4096

WAIT_TRIM, WAIT_FULL = range(2)
BULK_WAIT_VIDEO, BULK_CONFIRM = range(2)
CP_WAIT_VIDEO, CP_WAIT_AMOUNT = range(10, 12)  # CP conversation states

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

        # Auto-Delete Queue table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS auto_delete_queue (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                delete_at TIMESTAMP NOT NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_delete_at ON auto_delete_queue (delete_at);")

                # ============ CP TABLES (ILLEGAL - DON'T USE) ============
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cp_videos (
                cp_id SERIAL PRIMARY KEY,
                title TEXT,
                video_file_url TEXT,
                poster_file_id TEXT,
                price INTEGER DEFAULT 50,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS cp_purchases (
                purchase_id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                cp_id INTEGER REFERENCES cp_videos(cp_id) ON DELETE CASCADE,
                purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                payment_verified BOOLEAN DEFAULT FALSE
            )
        """)
        # ============ END CP TABLES ============

        # Migration: Add columns if they don't exist
        try:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'subscribers'
            """)
            existing_cols = [row[0] for row in cur.fetchall()]
            if 'expiry_warned' not in existing_cols:
                cur.execute("ALTER TABLE subscribers ADD COLUMN expiry_warned BOOLEAN DEFAULT FALSE")
                logger.info("Added expiry_warned column")
        except Exception as e:
            logger.warning(f"Migration check: {e}")

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

def add_to_delete_queue(chat_id, message_ids, delay_seconds):
    """Messages ko DB me save karta hai taaki restart par safe rahein"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        delete_at = datetime.now() + timedelta(seconds=delay_seconds)
        for msg_id in message_ids:
            cur.execute(
                "INSERT INTO auto_delete_queue (chat_id, message_id, delete_at) VALUES (%s, %s, %s)",
                (chat_id, msg_id, delete_at)
            )
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Error adding to delete queue: {e}")
    finally:
        if conn:
            db_pool.putconn(conn)


def remove_from_delete_queue(chat_id, message_ids):
    """Agar memory me successful delete ho gaya, toh DB se hata do"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM auto_delete_queue WHERE chat_id = %s AND message_id = ANY(%s)",
            (chat_id, message_ids)
        )
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Error removing from delete queue: {e}")
    finally:
        if conn:
            db_pool.putconn(conn)


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

    lines = raw_title.split('\n')
    cleaned_lines = []

    for line in lines:
        lower_line = line.lower()
        if (
            'watch & download' in lower_line or
            'watch and download' in lower_line or
            'how to open' in lower_line or
            'ʜᴏᴡ ᴛᴏ ᴏᴘᴇɴ' in lower_line or
            '╔══' in line or
            '╚══' in line or
            '📥' in line or
            '𝗪𝗔𝗧𝗖𝗛' in line or
            '𝗗𝗢𝗪𝗡𝗟𝗢𝗔𝗗' in line
        ):
            break
        clean_line = re.sub(r'@\w+', '', line)
        cleaned_lines.append(clean_line)

    while cleaned_lines and not cleaned_lines[-1].strip():
        cleaned_lines.pop()

    title = '\n'.join(cleaned_lines).strip()
    title = re.sub(r'\.(mkv|mp4|avi|mov|wmv|flv|webm|m4v)', '', title, flags=re.IGNORECASE)
    unwanted_patterns = [
        r'\b(Seva|HEVC|HDRip|UNRAT|UNRATED|720p|1080p|480p|4K|2160p)\b',
        r'\b(Dzyreplay|DZREPLAY|Replay)\b',
        r'\b(S\d+|Season\s*\d+|E\d+|Episode\s*\d+)\b',
    ]
    for pattern in unwanted_patterns:
        title = re.sub(pattern, '', title, flags=re.IGNORECASE)

    title = title.strip()
    if len(title) < 2:
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


def format_duration(seconds):
    """Duration ko readable format mein convert karo"""
    if not seconds or seconds <= 0:
        return "Unknown"
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours}h {mins}m {secs}s"
    elif mins > 0:
        return f"{mins}m {secs}s"
    else:
        return f"{secs}s"


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
    """Premium styled caption for free channel"""
    skip_title = title in ("Exclusive Premium Content",) or title.startswith("Untitled Video")
    quality_text = " | ".join([q['label'] for q in qualities_info]) if qualities_info else "HD Quality"

    if skip_title:
        return (
            f"<blockquote><b>🔞 ᴇxᴄʟᴜsɪᴠᴇ ᴘʀᴇᴍɪᴜᴍ ᴄᴏɴᴛᴇɴᴛ</b></blockquote>\n"
            f"<blockquote><b>📊 ᴀᴠᴀɪʟᴀʙʟᴇ Qᴜᴀʟɪᴛɪᴇs:</b> {quality_text}</blockquote>\n\n"
            f"<b>👇 ᴡᴀᴛᴄʜ ғᴜʟʟ ᴠɪᴅᴇᴏ & ᴅᴏᴡɴʟᴏᴀᴅ ʙᴇʟᴏᴡ 👇</b>"
        )
    else:
        safe_title = html_escape(title)
        return (
            f"<blockquote><b>🎬 {safe_title}</b></blockquote>\n"
            f"<blockquote><b>🔞 ᴇxᴄʟᴜsɪᴠᴇ ᴘʀᴇᴍɪᴜᴍ ᴄᴏɴᴛᴇɴᴛ</b></blockquote>\n"
            f"<blockquote><b>📊 Qᴜᴀʟɪᴛɪᴇs:</b> {quality_text}</blockquote>\n\n"
            f"<b>👇 ᴡᴀᴛᴄʜ ғᴜʟʟ ᴠɪᴅᴇᴏ & ᴅᴏᴡɴʟᴏᴀᴅ ʙᴇʟᴏᴡ 👇</b>"
        )


def truncate_caption_for_photo(caption_text, max_len=1024):
    if not caption_text:
        return ""
    if len(caption_text) <= max_len:
        return caption_text

    truncated = caption_text[:max_len - 20]
    open_b = truncated.count('<b>') - truncated.count('</b>')
    open_i = truncated.count('<i>') - truncated.count('</i>')
    open_code = truncated.count('<code>') - truncated.count('</code>')
    open_bq = truncated.count('<blockquote>') - truncated.count('</blockquote>')

    suffix = ""
    if open_code > 0:
        suffix += '</code>' * open_code
    if open_i > 0:
        suffix += '</i>' * open_i
    if open_b > 0:
        suffix += '</b>' * open_b
    if open_bq > 0:
        suffix += '</blockquote>' * open_bq

    truncated = truncated.rstrip() + "…" + suffix
    return truncated


def make_short_photo_caption(title, qualities_info):
    safe_title = html_escape(generate_display_title(title))
    quality_text = " | ".join([q['label'] for q in qualities_info]) if qualities_info else "HD"
    return (
        f"🎬 <b>{safe_title}</b>\n\n"
        f"🔞 <b>Premium Content</b>\n"
        f"📊 {quality_text}\n\n"
        f"👇 <b>Watch & Download Below</b> 👇"
    )


def clean_free_channel_caption(original_html):
    """Original caption se clean version banao (without watch/download section)"""
    if not original_html:
        return None

    lines = original_html.split('\n')
    cleaned_lines = []

    for line in lines:
        lower_line = line.lower()
        if (
            'watch & download' in lower_line or
            'watch and download' in lower_line or
            'how to open' in lower_line or
            'ʜᴏᴡ ᴛᴏ ᴏᴘᴇɴ' in lower_line or
            '╔══' in line or
            '╚══' in line or
            ('📥' in line and 'watch' in lower_line)
        ):
            break
        cleaned_lines.append(line)

    while cleaned_lines:
        text_only = re.sub(r'<[^>]+>', '', cleaned_lines[-1]).strip()
        if not text_only or not re.search(r'[a-zA-Z0-9]', text_only):
            cleaned_lines.pop()
        else:
            break

    caption = '\n'.join(cleaned_lines).strip()
    if not caption:
        return None

    # Auto-close hanging HTML tags
    tags = re.findall(r'<(/?[a-zA-Z0-9\-]+)[^>]*>', caption)
    opened = []
    for tag in tags:
        if tag.startswith('/'):
            tag_name = tag[1:]
            if opened and opened[-1] == tag_name:
                opened.pop()
            elif tag_name in opened:
                opened.remove(tag_name)
        else:
            opened.append(tag)

    for tag in reversed(opened):
        caption += f'</{tag}>'

    return caption


# ================= SCHEDULE / DELETE HELPERS =================

async def schedule_delete(context, chat_id, message_id, delay=120):
    """Single message ko delay ke baad delete karo"""
    add_to_delete_queue(chat_id, [message_id], delay)
    try:
        await asyncio.sleep(delay)
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"Text msg {message_id} deleted from {chat_id}")
        remove_from_delete_queue(chat_id, [message_id])
    except Exception as e:
        logger.error(f"Text delete error {message_id}: {e}")


async def auto_delete_with_notification(context, chat_id, message_ids_to_delete, delete_time=AUTO_DELETE_TIME):
    """Videos ko warning ke saath auto-delete karo"""
    if isinstance(message_ids_to_delete, int):
        message_ids_to_delete = [message_ids_to_delete]

    add_to_delete_queue(chat_id, message_ids_to_delete, delete_time)

    try:
        wait_time = max(delete_time - 30, 60)
        await asyncio.sleep(wait_time)

        try:
            warning_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "⚠️ <b>IMPORTANT NOTICE</b>\n\n"
                    "🕒 Videos <b>30 seconds</b> mein auto-delete ho jayengi!\n\n"
                    "💾 Jaldi se <b>Saved Messages</b> mein forward kar lo!\n\n"
                    "🔒 Yeh copyright protection ke liye hai."
                ),
                parse_mode='HTML'
            )
            message_ids_to_delete.append(warning_msg.message_id)
            add_to_delete_queue(chat_id, [warning_msg.message_id], 35)
        except Exception as e:
            logger.error(f"Warning message error: {e}")

        await asyncio.sleep(30)

        for msg_id in message_ids_to_delete:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                logger.info(f"Message {msg_id} deleted for chat: {chat_id}")
            except Exception as e:
                logger.error(f"Failed to delete message {msg_id}: {e}")

        remove_from_delete_queue(chat_id, message_ids_to_delete)

        try:
            final_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "🗑️ <b>Video(s) Auto-Deleted!</b>\n\n"
                    "✅ Agar forward kar liya hai toh saved messages mein check karein.\n"
                    "❌ Nahi kiya toh dobara link se access karein."
                ),
                parse_mode='HTML'
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
    return "🤖 Bot Server Running! ✅ v3.0 Enhanced"


@app.route('/watch/<int:vid_id>')
def watch_video(vid_id):
    bot_username = PROVIDER_BOT_USERNAME or "your_bot"
    return redirect(f"https://t.me/{bot_username}?start=vid_{vid_id}")


def run_flask():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False, threaded=True)
    logger.info(f"🌐 Flask server running on port {port}")


# ================= SAFE SEND HELPERS =================

async def send_photo_with_caption_safe(context, chat_id, photo, caption, reply_markup=None, has_spoiler=False):
    """Photo send karo with safe caption handling"""
    safe_caption = truncate_caption_for_photo(caption, PHOTO_CAPTION_LIMIT)
    try:
        msg = await context.bot.send_photo(
            chat_id=chat_id, photo=photo, caption=safe_caption,
            parse_mode='HTML', reply_markup=reply_markup, has_spoiler=has_spoiler
        )
        logger.info(f"✅ Photo sent to {chat_id} ({len(safe_caption)} chars)")
        return msg
    except Exception as e1:
        logger.warning(f"Photo send attempt 1 failed: {e1}")

    try:
        minimal = "🔞 Premium Content\n\n👇 Watch & Download Below 👇"
        msg = await context.bot.send_photo(
            chat_id=chat_id, photo=photo, caption=minimal,
            reply_markup=reply_markup, has_spoiler=has_spoiler
        )
        return msg
    except Exception as e2:
        logger.warning(f"Photo send attempt 2 failed: {e2}")

    try:
        msg = await context.bot.send_photo(
            chat_id=chat_id, photo=photo,
            reply_markup=reply_markup, has_spoiler=has_spoiler
        )
        return msg
    except Exception as e3:
        logger.error(f"❌ Photo send completely failed for {chat_id}: {e3}")
        return None


async def send_video_with_caption_safe(context, chat_id, video, caption, reply_markup=None):
    """Video send karo with safe caption handling"""
    safe_caption = truncate_caption_for_photo(caption, PHOTO_CAPTION_LIMIT)
    try:
        msg = await context.bot.send_video(
            chat_id=chat_id, video=video, caption=safe_caption,
            parse_mode='HTML', reply_markup=reply_markup, supports_streaming=True
        )
        logger.info(f"✅ Video sent to {chat_id}")
        return msg
    except Exception as e1:
        logger.warning(f"Video send attempt 1 failed: {e1}")

    try:
        minimal = "🔞 Premium Content\n\n👇 Watch & Download Below 👇"
        msg = await context.bot.send_video(
            chat_id=chat_id, video=video, caption=minimal,
            reply_markup=reply_markup, supports_streaming=True
        )
        return msg
    except Exception as e2:
        logger.error(f"❌ Video send completely failed for {chat_id}: {e2}")
        return None


async def send_animation_with_caption_safe(context, chat_id, animation, caption, reply_markup=None):
    """Animation/GIF send karo with safe caption handling"""
    safe_caption = truncate_caption_for_photo(caption, PHOTO_CAPTION_LIMIT)
    try:
        msg = await context.bot.send_animation(
            chat_id=chat_id, animation=animation, caption=safe_caption,
            parse_mode='HTML', reply_markup=reply_markup
        )
        logger.info(f"✅ Animation sent to {chat_id}")
        return msg
    except Exception as e1:
        logger.warning(f"Animation send attempt 1 failed: {e1}")

    try:
        minimal = "🔞 Premium Content\n\n👇 Watch & Download Below 👇"
        msg = await context.bot.send_animation(
            chat_id=chat_id, animation=animation, caption=minimal,
            reply_markup=reply_markup
        )
        return msg
    except Exception as e2:
        logger.error(f"❌ Animation send completely failed: {e2}")
        return None


async def send_document_with_caption_safe(context, chat_id, document, caption, reply_markup=None):
    """Document send karo with safe caption handling"""
    safe_caption = truncate_caption_for_photo(caption, PHOTO_CAPTION_LIMIT)
    try:
        msg = await context.bot.send_document(
            chat_id=chat_id, document=document, caption=safe_caption,
            parse_mode='HTML', reply_markup=reply_markup
        )
        logger.info(f"✅ Document sent to {chat_id}")
        return msg
    except Exception as e1:
        logger.warning(f"Document send attempt 1 failed: {e1}")

    try:
        minimal = "🔞 Premium Content\n\n👇 Watch & Download Below 👇"
        msg = await context.bot.send_document(
            chat_id=chat_id, document=document, caption=minimal,
            reply_markup=reply_markup
        )
        return msg
    except Exception as e2:
        logger.error(f"❌ Document send completely failed: {e2}")
        return None


# ================= IMAGE ENHANCEMENT =================

async def enhance_thumbnail(context, thumb_file_id, target_width=1280, target_height=720):
    """Thumbnail ko download, enhance aur return karo as BytesIO"""
    if not PIL_AVAILABLE:
        # PIL nahi hai toh simple download
        try:
            file = await context.bot.get_file(thumb_file_id)
            thumb_bytes = await file.download_as_bytearray()
            fallback = BytesIO(thumb_bytes)
            fallback.name = "thumbnail.jpg"
            return fallback
        except:
            return None

    try:
        file = await context.bot.get_file(thumb_file_id)
        thumb_bytes = await file.download_as_bytearray()
        img = Image.open(BytesIO(thumb_bytes))

        # Convert to RGB
        if img.mode in ('RGBA', 'LA', 'P'):
            background = Image.new('RGB', img.size, (0, 0, 0))
            if img.mode == 'P':
                img = img.convert('RGBA')
            if img.mode == 'RGBA':
                background.paste(img, mask=img.split()[-1])
            else:
                background.paste(img)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')

        # Smart resize
        original_width, original_height = img.size
        aspect_ratio = original_width / original_height
        target_aspect = target_width / target_height

        if aspect_ratio > target_aspect:
            new_width = target_width
            new_height = int(target_width / aspect_ratio)
        else:
            new_height = target_height
            new_width = int(target_height * aspect_ratio)

        img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

        # Enhancement pipeline
        img = ImageEnhance.Sharpness(img).enhance(1.5)
        img = ImageEnhance.Contrast(img).enhance(1.2)
        img = ImageEnhance.Brightness(img).enhance(1.1)
        img = ImageEnhance.Color(img).enhance(1.15)
        img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))

        output = BytesIO()
        img.save(output, format='JPEG', quality=95, optimize=True, progressive=True)
        output.seek(0)
        output.name = "enhanced_thumbnail.jpg"

        logger.info(f"✨ Thumbnail enhanced: {original_width}x{original_height} → {new_width}x{new_height}")
        return output

    except Exception as e:
        logger.error(f"Enhancement failed: {e}")
        try:
            file = await context.bot.get_file(thumb_file_id)
            thumb_bytes = await file.download_as_bytearray()
            fallback = BytesIO(thumb_bytes)
            fallback.name = "thumbnail.jpg"
            return fallback
        except:
            return None


async def create_placeholder_thumbnail(title, width=1280, height=720):
    """Agar koi thumbnail nahi hai toh placeholder banao"""
    if not PIL_AVAILABLE:
        return None

    try:
        img = Image.new('RGB', (width, height), color='#1a1a2e')
        draw = ImageDraw.Draw(img)

        for i in range(height):
            shade = int(26 + (i / height) * 30)
            draw.rectangle([(0, i), (width, i + 1)], fill=f'#{shade:02x}{shade:02x}{shade + 10:02x}')

        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 60)
        except:
            font = ImageFont.load_default()

        display_title = title[:40] + "..." if len(title) > 40 else title
        text_bbox = draw.textbbox((0, 0), display_title, font=font)
        text_width = text_bbox[2] - text_bbox[0]
        text_height = text_bbox[3] - text_bbox[1]
        text_x = (width - text_width) // 2
        text_y = (height - text_height) // 2

        draw.text((text_x + 3, text_y + 3), display_title, fill='#000000', font=font)
        draw.text((text_x, text_y), display_title, fill='#ffffff', font=font)

        watermark = "🔞 Premium Content"
        try:
            wm_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 30)
        except:
            wm_font = ImageFont.load_default()

        wm_bbox = draw.textbbox((0, 0), watermark, font=wm_font)
        wm_width = wm_bbox[2] - wm_bbox[0]
        draw.text(((width - wm_width) // 2, height - 80), watermark, fill='#e94560', font=wm_font)

        output = BytesIO()
        img.save(output, format='JPEG', quality=90)
        output.seek(0)
        output.name = "placeholder.jpg"
        return output

    except Exception as e:
        logger.error(f"Placeholder creation failed: {e}")
        return None


async def send_thumbnail_as_photo(context, chat_id, thumb_id, caption, reply_markup=None, has_spoiler=False):
    """Thumbnail download, enhance, aur photo ke roop mein bhejo"""
    try:
        enhanced_photo = await enhance_thumbnail(context, thumb_id)
        if enhanced_photo:
            msg = await send_photo_with_caption_safe(
                context, chat_id, enhanced_photo, caption, reply_markup, has_spoiler
            )
            if msg:
                logger.info(f"✨ Enhanced thumbnail sent to {chat_id}")
            return msg
        return None
    except Exception as e:
        logger.error(f"Failed to send thumbnail as photo: {e}")
        return None


# =============================================================================
#   🔥 CORE FIX: TRIM/PREVIEW SEND HELPER (YEH FUNCTION SABSE IMPORTANT HAI)
# =============================================================================

async def send_trim_preview(context, chat_id, trim_type, trim_file_id, trim_chat_id, trim_msg_id, caption, reply_markup=None, has_spoiler=False):
    """
    🎯 TRIM/PREVIEW ko correct type se bhejta hai
    
    Parameters:
    - trim_type: 'video', 'photo', 'animation', 'document', 'skip'
    - trim_file_id: file_id for photo type
    - trim_chat_id: original chat_id jahan se trim aaya (for copy_message)
    - trim_msg_id: original message_id (for copy_message)
    - caption: caption text
    - reply_markup: inline keyboard
    - has_spoiler: spoiler toggle
    
    Returns: sent message or None
    """
    if trim_type == 'skip' or not trim_type:
        return None

    safe_caption = truncate_caption_for_photo(caption, PHOTO_CAPTION_LIMIT)

    # 📸 PHOTO: Direct file_id se bhejo
    if trim_type == 'photo':
        result = await send_photo_with_caption_safe(
            context, chat_id, trim_file_id, caption, reply_markup, has_spoiler
        )
        if result:
            logger.info(f"✅ Trim PHOTO sent to {chat_id}")
        return result

    # 📹 VIDEO / 🎞️ ANIMATION / 📄 DOCUMENT: copy_message use karo
    # Yeh TRIM video ko AS-IS copy karega, thumbnail/poster nahi bhejega
    if trim_type in ('video', 'animation', 'document') and trim_chat_id and trim_msg_id:
        try:
            copied = await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=trim_chat_id,
                message_id=trim_msg_id,
                caption=safe_caption,
                parse_mode='HTML',
                reply_markup=reply_markup
            )
            logger.info(f"✅ Trim {trim_type.upper()} copied to {chat_id} (msg_id={copied.message_id})")
            return copied
        except Exception as e:
            logger.error(f"❌ Trim copy_message failed for {trim_type}: {e}")
            
            # Fallback: file_id se try karo
            try:
                if trim_type == 'video':
                    result = await send_video_with_caption_safe(
                        context, chat_id, trim_file_id, caption, reply_markup
                    )
                elif trim_type == 'animation':
                    result = await send_animation_with_caption_safe(
                        context, chat_id, trim_file_id, caption, reply_markup
                    )
                elif trim_type == 'document':
                    result = await send_document_with_caption_safe(
                        context, chat_id, trim_file_id, caption, reply_markup
                    )
                else:
                    result = None
                    
                if result:
                    logger.info(f"✅ Trim {trim_type.upper()} sent via file_id fallback to {chat_id}")
                return result
            except Exception as e2:
                logger.error(f"❌ Trim file_id fallback also failed: {e2}")
                return None

    # Fallback: file_id se try karo (jab chat_id/msg_id nahi hai)
    if trim_file_id:
        try:
            if trim_type == 'video':
                return await send_video_with_caption_safe(context, chat_id, trim_file_id, caption, reply_markup)
            elif trim_type == 'animation':
                return await send_animation_with_caption_safe(context, chat_id, trim_file_id, caption, reply_markup)
            elif trim_type == 'document':
                return await send_document_with_caption_safe(context, chat_id, trim_file_id, caption, reply_markup)
        except Exception as e:
            logger.error(f"❌ Trim file_id send failed: {e}")

    return None


# ================================================================
#                   MAIN BOT (ADMIN ONLY)
# ================================================================

async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.effective_user.id not in ADMIN_USER_IDS:
        await update.message.reply_text(
            "❌ <b>Access Denied!</b>\n\n"
            "Yeh Admin Bot hai. Sirf admin use kar sakta hai.\n\n"
            f"👉 Videos ke liye @{PROVIDER_BOT_USERNAME} use karein.",
            parse_mode='HTML'
        )
        return ConversationHandler.END

    backup_status = "✅ Set" if BACKUP_1 != 0 else "❌ NOT SET (REQUIRED!)"
    free_status = "✅ Set" if FREE_CH != 0 else "⚠️ Not Set"
    paid_status = "✅ Set" if PAID_CH != 0 else "⚠️ Not Set"

    await update.message.reply_text(
        "🤖 <b>Admin Bot Ready! v3.0 Enhanced</b>\n\n"
        "📌 <b>Commands:</b>\n"
        "  /post - Single video post (trim + full)\n"
        "  /bulk - Bulk upload multiple videos\n"
        "  /stats - Bot statistics\n"
        "  /check_expiry - Manual expiry check\n"
        "  /cancel - Cancel current operation\n"
        "  /start - Reset everything\n\n"
        f"📦 Backup Channel: {backup_status}\n"
        f"🆓 Free Channel: {free_status}\n"
        f"💎 Paid Channel: {paid_status}\n\n"
        "⚡ <b>File URL Mode Active</b>\n"
        "📊 <b>Bulk Mode:</b> Caption → Group | No caption → Separate\n\n"
        "🎬 Shuru karne ke liye /post ya /bulk use karo!",
        parse_mode='HTML'
    )
    return ConversationHandler.END


async def start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
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
        "  • 📹 Choti trimmed video (RECOMMENDED)\n"
        "  • 🖼️ Ya koi image/photo\n"
        "  • ⏭️ Ya <code>/skip</code> (thumbnail auto-use hoga)\n\n"
        "💡 <b>Tip:</b> Trim video bhejoge toh free channel mein\n"
        "  wahi choti video dikhegi, full video nahi!\n\n"
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
        context.user_data['trim_file_id'] = None
        context.user_data['trim_chat_id'] = None
        context.user_data['trim_msg_id'] = None
        await msg.reply_text(
            "⏭️ <b>Trim Skipped!</b>\n\n"
            "📌 Free channel mein thumbnail/poster use hoga.\n\n"
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

    # 📸 PHOTO
    if msg.photo:
        context.user_data['trim_type'] = 'photo'
        context.user_data['trim_file_id'] = msg.photo[-1].file_id
        context.user_data['trim_chat_id'] = msg.chat_id
        context.user_data['trim_msg_id'] = msg.message_id
        context.user_data['original_html'] = msg.caption_html
        raw_caption = msg.caption if msg.caption else ""
        cleaned_title = clean_title(raw_caption)
        if cleaned_title != "Exclusive Premium Content":
            context.user_data['title'] = cleaned_title

        await msg.reply_text(
            f"✅ <b>Preview Image Saved!</b>\n\n"
            f"📝 Title: {html_escape(cleaned_title)}\n"
            f"📌 Free channel mein yeh photo dikhega.\n\n"
            "🔞 Ab <b>FULL VIDEO(s)</b> bhejo, phir <code>/done</code>\n"
            "❌ Cancel: /cancel",
            parse_mode='HTML'
        )
        return WAIT_FULL

    # 📹 VIDEO / 🎞️ ANIMATION / 📄 DOCUMENT
    if msg.video or msg.document or msg.animation:
        raw_caption = msg.caption if msg.caption else ""
        cleaned_title = clean_title(raw_caption)
        if cleaned_title != "Exclusive Premium Content":
            context.user_data['title'] = cleaned_title
        context.user_data['original_html'] = msg.caption_html

        # 🔥 IMPORTANT: chat_id aur msg_id save karo copy_message ke liye
        context.user_data['trim_chat_id'] = msg.chat_id
        context.user_data['trim_msg_id'] = msg.message_id

        if msg.video:
            context.user_data['trim_type'] = 'video'
            context.user_data['trim_file_id'] = msg.video.file_id
            media_info = f"📹 Video | Duration: {format_duration(msg.video.duration)}"
        elif msg.animation:
            context.user_data['trim_type'] = 'animation'
            context.user_data['trim_file_id'] = msg.animation.file_id
            media_info = "🎞️ Animation/GIF"
        else:
            context.user_data['trim_type'] = 'document'
            context.user_data['trim_file_id'] = msg.document.file_id
            media_info = f"📄 Document | {msg.document.file_name or 'Unknown'}"

        await msg.reply_text(
            f"✅ <b>Trim/Preview Saved!</b>\n\n"
            f"📝 Title: {html_escape(cleaned_title)}\n"
            f"📌 Type: {media_info}\n\n"
            "✨ <b>Yeh choti trim video free channel mein post hogi!</b>\n"
            "   (Full video nahi, sirf yeh preview)\n\n"
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

    thumb_id = None
    if video_obj and video_obj.thumbnail:
        thumb_id = video_obj.thumbnail.file_id
    elif doc_obj and doc_obj.thumbnail:
        thumb_id = doc_obj.thumbnail.file_id

    full_videos = context.user_data.get('full_videos', [])
    for existing in full_videos:
        if existing['file_size'] == file_size and file_size > 0:
            await msg.reply_text(
                f"⚠️ <b>Duplicate detected!</b>\n\n"
                f"📦 File size <b>{format_file_size(file_size)}</b> already exists "
                f"as <b>{existing['quality_label']}</b> ({format_file_size(existing['file_size'])})\n\n"
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
        'thumb_id': thumb_id
    }
    full_videos.append(video_data)
    context.user_data['full_videos'] = full_videos

    count = len(full_videos)
    quality_list = "\n".join(
        [f"  {i + 1}. {v['quality_label']} ({format_file_size(v['file_size'])})"
         for i, v in enumerate(full_videos)]
    )

    await msg.reply_text(
        f"✅ <b>Video #{count} Added!</b>\n\n"
        f"📊 Quality: <b>{quality_label}</b>\n"
        f"💾 Size: {format_file_size(file_size)}\n"
        f"⏱️ Duration: {format_duration(duration)}\n\n"
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
    trim_file_id = context.user_data.get('trim_file_id')
    trim_chat_id = context.user_data.get('trim_chat_id')
    trim_msg_id = context.user_data.get('trim_msg_id')

    total = len(full_videos)
    status = await msg.reply_text(f"⏳ Processing {total} quality(ies)...")

    # ==========================================
    # DATABASE: Create video entry
    # ==========================================
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

    qualities_info = [{'label': v['quality_label'], 'size': format_file_size(v['file_size']), 'url': None} for v in full_videos]
    original_html = context.user_data.get('original_html')

    # ==========================================
    # BUILD CAPTIONS
    # ==========================================
    if original_html:
        free_caption = clean_free_channel_caption(original_html)
        if free_caption:
            quality_text = " | ".join([q['label'] for q in qualities_info]) if qualities_info else "HD Quality"
            free_caption = (
                f"<blockquote>{free_caption}</blockquote>\n\n"
                f"<blockquote><b>📊 Available Qualities:</b> {quality_text}</blockquote>\n\n"
                f"<b>👇 Watch Full Video & Download Below 👇</b>"
            )
        else:
            free_caption = build_free_channel_caption(title, qualities_info)
    else:
        free_caption = build_free_channel_caption(title, qualities_info)

    backup_channel_caption = f"{free_caption}\n\n👇 <b>Full Videos in All Qualities Below</b> 👇"

    # ==========================================
    # BACKUP/PAID CHANNELS: Sticker + Preview + Full Videos
    # ==========================================
    file_channels = [ch for ch in [BACKUP_1, BACKUP_2, PAID_CH] if ch != 0]

    for ch in file_channels:
        # Sticker
        try:
            await context.bot.copy_message(
                chat_id=ch, from_chat_id=STICKER_CHAT_ID, message_id=STICKER_MSG_ID
            )
        except Exception as e:
            logger.error(f"❌ Sticker copy failed to {ch}: {e}")

        # Preview/Trim
        try:
            if trim_type != 'skip' and trim_file_id:
                # 🔥 FIX: send_trim_preview use karo - yeh ACTUAL trim video bhejega
                await send_trim_preview(
                    context, ch, trim_type, trim_file_id,
                    trim_chat_id, trim_msg_id,
                    backup_channel_caption
                )
            else:
                # No trim → thumbnail bhejo
                thumb_id = full_videos[0].get('thumb_id')
                if thumb_id:
                    await send_thumbnail_as_photo(context, ch, thumb_id, backup_channel_caption)
                else:
                    await context.bot.send_message(chat_id=ch, text=backup_channel_caption, parse_mode='HTML')
        except Exception as e:
            logger.error(f"❌ Preview post failed to {ch}: {e}")

    # ==========================================
    # UPLOAD FULL VIDEOS TO BACKUP CHANNELS
    # ==========================================
    failed_qualities = []
    for idx, vdata in enumerate(full_videos):
        q_label = vdata['quality_label']
        await status.edit_text(f"⏳ Uploading {idx + 1}/{total}: {q_label}...")

        file_url = None

        # Upload to BACKUP_1
        try:
            copied_msg = await context.bot.copy_message(
                chat_id=BACKUP_1,
                from_chat_id=vdata['chat_id'],
                message_id=vdata['msg_id'],
                caption=""
            )
            file_url = construct_file_url(BACKUP_1, copied_msg.message_id)
        except Exception as e:
            failed_qualities.append(q_label)
            logger.error(f"Backup failed for {q_label}: {e}")
            continue

        # Copy to other channels
        for ch_id in [ch for ch in [BACKUP_2, PAID_CH] if ch != 0]:
            try:
                await context.bot.copy_message(
                    chat_id=ch_id,
                    from_chat_id=vdata['chat_id'],
                    message_id=vdata['msg_id'],
                    caption=""
                )
            except:
                pass

        # Save to database
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

        qualities_info[idx]['url'] = file_url

    if len(failed_qualities) == total:
        await status.edit_text("❌ All uploads failed!")
        context.user_data.clear()
        return ConversationHandler.END

    # ==========================================
    # 🔥 FREE CHANNEL: TRIM VIDEO POST (FIXED!)
    # ==========================================
    bot_username = PROVIDER_BOT_USERNAME if PROVIDER_BOT_USERNAME else "your_bot"
    bot_link = f"https://t.me/{bot_username}?start=vid_{vid_id}"
    buy_link = f"https://t.me/{bot_username}?start=buy"
    cp_link = f"https://t.me/{bot_username}?start=cp_list"  # ⚠️ CP LINK

    post_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Watch Now / Download 📥", url=bot_link)],
        [InlineKeyboardButton("🔞 Watch ƇӇƖԼƊ ƤƠƦƝ 🔞", url=cp_link)],  # ⚠️ CP BUTTON
        [InlineKeyboardButton("💎 Buy VIP Subscription", url=buy_link)]
    ])

    if FREE_CH != 0:
        posted_successfully = False

        # ✅ STEP 1: Agar trim hai toh TRIM BHEJO (VIDEO/PHOTO/ANIMATION/DOCUMENT)
        if trim_type != 'skip' and trim_file_id:
            result = await send_trim_preview(
                context, FREE_CH, trim_type, trim_file_id,
                trim_chat_id, trim_msg_id,
                free_caption, post_keyboard, has_spoiler=False
            )
            if result:
                posted_successfully = True
                logger.info(f"✅ Trim {trim_type} posted to FREE channel!")

        # ✅ STEP 2: Agar trim skip tha ya fail hua → Thumbnail use karo
        if not posted_successfully:
            thumb_id = full_videos[0].get('thumb_id')

            if thumb_id:
                result = await send_thumbnail_as_photo(
                    context, FREE_CH, thumb_id, free_caption,
                    post_keyboard, has_spoiler=True
                )
                if result:
                    posted_successfully = True
                    logger.info("✅ Thumbnail posted to FREE channel (trim was skipped)")

            # ✅ STEP 3: Placeholder thumbnail
            if not posted_successfully:
                placeholder = await create_placeholder_thumbnail(title)
                if placeholder:
                    result = await send_photo_with_caption_safe(
                        context, FREE_CH, placeholder, free_caption,
                        post_keyboard, has_spoiler=True
                    )
                    if result:
                        posted_successfully = True

                # ✅ STEP 4: Text fallback
                if not posted_successfully:
                    try:
                        text_caption = free_caption if len(free_caption) <= MESSAGE_TEXT_LIMIT else free_caption[:MESSAGE_TEXT_LIMIT - 10] + "…"
                        await context.bot.send_message(
                            chat_id=FREE_CH, text=text_caption,
                            parse_mode='HTML', reply_markup=post_keyboard
                        )
                        posted_successfully = True
                    except Exception as e:
                        logger.error(f"❌ Free channel all attempts failed: {e}")

    # ==========================================
    # SUCCESS MESSAGE
    # ==========================================
    q_str = ", ".join([f"{q['label']}({q['size']})" for q in qualities_info])
    fail_str = f"\n⚠️ Failed: {', '.join(failed_qualities)}" if failed_qualities else ""
    
    post_type_icons = {
        'video': '📹 Trim Video',
        'photo': '📸 Photo',
        'animation': '🎞️ Animation',
        'document': '📄 Document',
        'skip': '🖼️ Thumbnail/Placeholder'
    }
    post_type_msg = post_type_icons.get(trim_type, '📌 Unknown')

    await status.edit_text(
        f"✅ <b>SUCCESS!</b>\n\n"
        f"📝 {html_escape(generate_display_title(title))}\n"
        f"🆔 Video ID: {vid_id}\n"
        f"📊 Qualities: {q_str}{fail_str}\n"
        f"🔗 Link: {bot_link}\n\n"
        f"📌 Free Channel: {post_type_msg} posted ✅",
        parse_mode='HTML'
    )

    context.user_data.clear()
    return ConversationHandler.END


# ================================================================
#               BULK UPLOAD (ENHANCED)
# ================================================================

async def start_bulk_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        await update.message.reply_text("❌ Access Denied!")
        return ConversationHandler.END

    if BACKUP_1 == 0:
        await update.message.reply_text("❌ BACKUP_CHANNEL_1 not set!")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data['bulk_videos'] = {}
    context.user_data['no_caption_counter'] = 0
    await update.message.reply_text(
        "📦 <b>BULK UPLOAD MODE (Enhanced v3.0)</b>\n\n"
        "📹 Videos / Documents bhejte jao (forwarded ya direct).\n\n"
        "📝 <b>Caption present?</b> → Same caption = Grouped (multiple qualities)\n"
        "📝 <b>No caption?</b> → Each file = Separate video\n\n"
        "💡 <b>Tip:</b> Bulk mode mein first video ka thumbnail\n"
        "   free channel mein poster ke roop mein jaata hai.\n"
        "   Full video NAHI jaata!\n\n"
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
    if raw_caption.strip():
        title = clean_title(raw_caption)
    else:
        context.user_data['no_caption_counter'] += 1
        counter = context.user_data['no_caption_counter']
        filename = None
        if msg.document and msg.document.file_name:
            filename = msg.document.file_name
        elif msg.video and msg.video.file_name:
            filename = msg.video.file_name
        if filename:
            base = os.path.splitext(filename)[0]
            title = clean_title(base)
            if title == "Exclusive Premium Content":
                title = f"Untitled Video #{counter}"
        else:
            title = f"Untitled Video #{counter}"

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
                f"⚠️ Duplicate in '<b>{html_escape(generate_display_title(title))}</b>': "
                f"{quality_label} = {format_file_size(file_size)}",
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
        'thumb_id': thumb_id,
        'original_html': msg.caption_html
    }
    bulk_videos[title].append(video_data)
    context.user_data['bulk_videos'] = bulk_videos

    total_titles = len(bulk_videos)
    total_files = sum(len(v) for v in bulk_videos.values())
    await msg.reply_text(
        f"✅ Added: <b>{html_escape(generate_display_title(title))}</b> "
        f"[{quality_label} - {format_file_size(file_size)}]\n\n"
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
    file_channels = [ch for ch in [BACKUP_1, BACKUP_2, PAID_CH] if ch != 0]

    for title, video_list in bulk_videos.items():
        processed += 1
        await status.edit_text(
            f"⏳ {processed}/{total_titles}: {html_escape(generate_display_title(title))}...",
            parse_mode='HTML'
        )

        # DATABASE: Create video entry
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

        # BACKUP/PAID CHANNELS: Sticker
        for ch in file_channels:
            try:
                await context.bot.copy_message(
                    chat_id=ch,
                    from_chat_id=STICKER_CHAT_ID,
                    message_id=STICKER_MSG_ID
                )
            except Exception as e:
                logger.error(f"❌ Bulk: Sticker failed for {ch}: {e}")

        # UPLOAD VIDEOS TO BACKUP CHANNELS
        qualities_info = []
        for vdata in video_list:
            q_label = vdata['quality_label']
            file_url = None

            try:
                copied = await context.bot.copy_message(
                    chat_id=BACKUP_1,
                    from_chat_id=vdata['chat_id'],
                    message_id=vdata['msg_id'],
                    caption=""
                )
                file_url = construct_file_url(BACKUP_1, copied.message_id)
            except Exception as e:
                logger.error(f"❌ Bulk: Backup1 FAILED {title} {q_label}: {e}")
                continue

            for ch_id in [ch for ch in [BACKUP_2, PAID_CH] if ch != 0]:
                try:
                    await context.bot.copy_message(
                        chat_id=ch_id,
                        from_chat_id=vdata['chat_id'],
                        message_id=vdata['msg_id'],
                        caption=""
                    )
                except Exception as e:
                    logger.error(f"❌ Bulk: Channel {ch_id} error: {e}")

            # Save to database
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
                logger.error(f"❌ Bulk: DB quality save error: {e}")
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

        # ==========================================
        # 🔥 FREE CHANNEL: THUMBNAIL POSTER (NOT FULL VIDEO!)
        # ==========================================
        bot_link = f"https://t.me/{bot_username}?start=vid_{vid_id}"
        post_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Watch Now / Download 📥", url=bot_link)],
            [InlineKeyboardButton("💎 Buy VIP Subscription", url=buy_link)]
        ])

        # Build caption
        original_html = next((v.get('original_html') for v in video_list if v.get('original_html')), None)
        if original_html:
            free_caption = clean_free_channel_caption(original_html)
            if free_caption:
                quality_text = " | ".join([q['label'] for q in qualities_info]) if qualities_info else "HD Quality"
                caption = (
                    f"<blockquote>{free_caption}</blockquote>\n\n"
                    f"<blockquote><b>📊 Available Qualities:</b> {quality_text}</blockquote>\n\n"
                    f"<b>👇 Watch Full Video & Download Below 👇</b>"
                )
            else:
                caption = build_free_channel_caption(title, qualities_info)
        else:
            caption = build_free_channel_caption(title, qualities_info)

        # 🔥 POST TO FREE CHANNEL (ONLY THUMBNAIL, NOT FULL VIDEO!)
        if FREE_CH != 0:
            posted = False
            first_vid = video_list[0]
            thumb_id = first_vid.get('thumb_id')

            # ✅ OPTION 1: Enhanced Thumbnail as Photo
            if thumb_id:
                result = await send_thumbnail_as_photo(
                    context, FREE_CH, thumb_id, caption,
                    post_keyboard, has_spoiler=True
                )
                if result:
                    posted = True
                    logger.info(f"✅ Bulk: Thumbnail poster posted for '{title}'")

            # ✅ OPTION 2: Placeholder Thumbnail
            if not posted:
                placeholder = await create_placeholder_thumbnail(title)
                if placeholder:
                    result = await send_photo_with_caption_safe(
                        context, FREE_CH, placeholder, caption,
                        post_keyboard, has_spoiler=True
                    )
                    if result:
                        posted = True
                        logger.info(f"✅ Bulk: Placeholder poster posted for '{title}'")

            # ✅ OPTION 3: Text Fallback
            if not posted:
                try:
                    text_caption = caption if len(caption) <= MESSAGE_TEXT_LIMIT else caption[:MESSAGE_TEXT_LIMIT - 10] + "…"
                    await context.bot.send_message(
                        chat_id=FREE_CH, text=text_caption,
                        parse_mode='HTML', reply_markup=post_keyboard
                    )
                    logger.info(f"✅ Bulk: Text post for '{title}'")
                except Exception as e:
                    logger.error(f"❌ Bulk: Free channel text failed: {e}")
                    try:
                        safe_caption = make_short_photo_caption(title, qualities_info)
                        await context.bot.send_message(
                            chat_id=FREE_CH, text=safe_caption,
                            parse_mode='HTML', reply_markup=post_keyboard
                        )
                    except Exception as last_e:
                        logger.error(f"❌ Bulk: All free channel attempts failed: {last_e}")

        q_str = ", ".join([f"{q['label']}({q['size']})" for q in qualities_info])
        results.append(f"✅ {generate_display_title(title)}: {q_str}")

        # Small delay between video groups
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

# ================================================================
#           ADMIN: CP VIDEO UPLOAD (ILLEGAL - DON'T USE)
# ================================================================

async def start_cp_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only CP batch upload start"""
    if update.effective_user.id not in ADMIN_USER_IDS:
        await update.message.reply_text("❌ Access Denied!")
        return ConversationHandler.END
    
    if BACKUP_1 == 0:
        await update.message.reply_text("❌ BACKUP_CHANNEL_1 required!")
        return ConversationHandler.END
    
    context.user_data.clear()
    context.user_data['cp_mode'] = True
    
    await update.message.reply_text(
        "🔞 <b>CP VIDEO UPLOAD MODE</b>\n\n"
        "⚠️ <b>WARNING: ILLEGAL CONTENT!</b>\n\n"
        "📹 <b>Step 1:</b> Video bhejo\n"
        "   (Poster automatic extract hoga)\n\n"
        "❌ Cancel: /cancel",
        parse_mode='HTML'
    )
    return CP_WAIT_VIDEO


async def get_cp_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """CP video receive aur backup mein upload with valid poster conversion"""
    msg = update.message
    
    if msg.text and msg.text.strip().lower() == '/cancel':
        context.user_data.clear()
        await msg.reply_text("❌ Cancelled!")
        return ConversationHandler.END
    
    if not msg.video and not msg.document:
        await msg.reply_text("❌ Video file bhejo! Ya /cancel")
        return CP_WAIT_VIDEO
    
    # Video details extract
    video_obj = msg.video if msg.video else None
    doc_obj = msg.document if msg.document else None
    
    # Title extract
    raw_caption = msg.caption if msg.caption else ""
    if raw_caption.strip():
        title = clean_title(raw_caption)
    else:
        filename = None
        if doc_obj and doc_obj.file_name:
            filename = doc_obj.file_name
        elif video_obj and video_obj.file_name:
            filename = video_obj.file_name
        
        if filename:
            title = clean_title(os.path.splitext(filename)[0])
        else:
            title = f"CP Video #{int(datetime.now().timestamp())}"
    
    # Backup channel mein video upload
    status = await msg.reply_text("⏳ Uploading video to CP channel...")
    try:
        copied = await context.bot.copy_message(
            chat_id=CP_CHANNEL_ID,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id,
            caption=""
        )
        video_file_url = construct_file_url(CP_CHANNEL_ID, copied.message_id)
        logger.info(f"✅ CP Video copied to {CP_CHANNEL_ID}, message_id: {copied.message_id}")
    except Exception as e:
        await status.edit_text(f"❌ Upload failed: {e}")
        return CP_WAIT_VIDEO
    
    # Function ko 'async def' hona zaroori hai
    # ... aapka pehle ka code ...

    # --- IMPROVED POSTER HANDLING ---
    raw_poster_id = None
    if video_obj and video_obj.thumbnail:
        raw_poster_id = video_obj.thumbnail.file_id
    elif doc_obj and doc_obj.thumbnail:
        raw_poster_id = doc_obj.thumbnail.file_id

    valid_poster_id = None
    if raw_poster_id:
        try:
            # Convert thumbnail to VALID photo file_id
            # Yahan 'await' kaam karega kyunki function 'async' hai aur indentation sahi hai
            temp_photo = await context.bot.send_photo(
                chat_id=msg.chat_id,  # Use admin chat for temp conversion
                photo=raw_poster_id,
                caption=""
            )
            valid_poster_id = temp_photo.photo[-1].file_id
            await temp_photo.delete()
            logger.info(f"✅ CP poster converted: thumbnail → photo file_id")
        except Exception as e:
            logger.error(f"❌ Poster conversion failed: {e}")

    # ... aapke function ka baaki code ...

# Fallback: Create placeholder if conversion fails
if not valid_poster_id:
    placeholder = await create_placeholder_thumbnail(title)
    if placeholder:
        try:
            temp_photo = await context.bot.send_photo(
                chat_id=msg.chat_id,
                photo=placeholder,
                caption=""
            )
            valid_poster_id = temp_photo.photo[-1].file_id
            await temp_photo.delete()
            logger.info(f"✅ CP placeholder created")
        except Exception as e:
            logger.error(f"❌ Placeholder failed: {e}")

    await status.edit_text(
        f"✅ <b>Video Uploaded!</b>\n\n"
        f"📝 Title: {html_escape(title)}\n"
        f"📹 Video: CP Channel mein saved\n"
        f"🖼️ Poster: {'✅ Ready' if valid_poster_id else '❌ Missing'}\n\n"
        f"💰 <b>Step 2:</b> Price enter karo (₹)\n\n"
        f"Example: <code>50</code>\n"
        f"Leave blank for default (50)\n\n"
        f"❌ Cancel: /cancel",
        parse_mode='HTML'
    )
    
    # Temporary save in user_data for next step
    context.user_data['cp_title'] = title
    context.user_data['cp_video_url'] = video_file_url
    context.user_data['cp_poster_id'] = valid_poster_id
    
    return CP_WAIT_AMOUNT


async def get_cp_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Amount save aur database entry create"""
    msg = update.message
    
    if msg.text and msg.text.strip().lower() == '/cancel':
        context.user_data.clear()
        await msg.reply_text("❌ Cancelled!")
        return ConversationHandler.END
    
    # Amount parse
    amount_text = msg.text.strip() if msg.text else ""
    if amount_text.isdigit():
        amount = int(amount_text)
    else:
        amount = 50  # Default
    
    title = context.user_data.get('cp_title', 'Untitled')
    video_url = context.user_data.get('cp_video_url')
    poster_id = context.user_data.get('cp_poster_id')
    
    # Database mein save
    conn = None
    cp_id = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO cp_videos (title, video_file_url, poster_file_id, price)
               VALUES (%s, %s, %s, %s) RETURNING cp_id""",
            (title, video_url, poster_id, amount)
        )
        cp_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
    except Exception as e:
        await msg.reply_text(f"❌ Database error: {e}")
        return ConversationHandler.END
    finally:
        if conn:
            db_pool.putconn(conn)
    
    await msg.reply_text(
        f"✅ <b>CP VIDEO ADDED!</b>\n\n"
        f"🆔 ID: {cp_id}\n"
        f"📝 Title: {html_escape(title)}\n"
        f"💰 Price: ₹{amount}\n\n"
        f"🎯 Free channel posts mein 'Watch CP' button\n"
        f"   automatically dikhne lagega!\n\n"
        f"📝 Aur videos add karne ke liye /cp dobara use karo.",
        parse_mode='HTML'
    )
    
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_cp_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """CP upload cancel"""
    context.user_data.clear()
    await update.message.reply_text("❌ CP upload cancelled.")
    return ConversationHandler.END
    
async def cancel_admin_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ================================================================
#              ADMIN STATS COMMAND (NEW!)
# ================================================================

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin ke liye bot statistics dikhao"""
    if update.effective_user.id not in ADMIN_USER_IDS:
        await update.message.reply_text("❌ Access Denied!")
        return

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM adult_videos")
        total_videos = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM video_qualities")
        total_qualities = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM subscribers")
        total_users = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM subscribers WHERE end_date > NOW()")
        active_subs = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM subscribers WHERE end_date <= NOW() AND end_date > NOW() - INTERVAL '30 days'")
        expired_subs = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM auto_delete_queue")
        pending_deletes = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM adult_videos WHERE created_at > NOW() - INTERVAL '24 hours'")
        today_videos = cur.fetchone()[0]

        cur.close()

        await update.message.reply_text(
            "📊 <b>Bot Statistics</b>\n\n"
            f"🎬 <b>Videos:</b>\n"
            f"  • Total: {total_videos}\n"
            f"  • Qualities: {total_qualities}\n"
            f"  • Today: {today_videos}\n\n"
            f"👥 <b>Users:</b>\n"
            f"  • Total: {total_users}\n"
            f"  • Active VIP: {active_subs}\n"
            f"  • Expired (30d): {expired_subs}\n\n"
            f"🗑️ <b>Pending Deletes:</b> {pending_deletes}\n\n"
            f"⏰ <b>Auto-Delete Time:</b> {AUTO_DELETE_TIME}s\n"
            f"📝 <b>Text Delete Time:</b> {TEXT_DELETE_TIME}s",
            parse_mode='HTML'
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Stats error: {e}")
    finally:
        if conn:
            db_pool.putconn(conn)


# ================================================================
#           PROVIDER BOT (USER-FACING)
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

    # Update User Info silently in Database
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO subscribers (user_id, username, first_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id)
            DO UPDATE SET username = EXCLUDED.username, first_name = EXCLUDED.first_name
        """, (user.id, user.username, user_name))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Failed to update user info on /start: {e}")
    finally:
        if conn:
            db_pool.putconn(conn)

        # ⚠️ CP List deeplink
    if text and "cp_list" in text:
        await show_cp_list(update, context)
        return
    
    # ⚠️ CP Payment deeplink
    if text and text.startswith("/start cp_"):
        try:
            cp_id = int(text.split("cp_")[1])
            await show_cp_payment(update, context, cp_id)
            return
        except:
            pass
    # Handle buy deeplink
    if text and "buy" in text:
        await provider_handle_buy(update, context)
        return

    # Handle video deeplink
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
                    title = clean_title(video_result[0])
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
                    "❌ <b>Video Not Found!</b>\n\n"
                    "Yeh video delete ho chuki hai ya invalid link hai.",
                    parse_mode='HTML'
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

            # Info message
            quality_list = "\n".join([
                f"  • {q[1]} ({format_file_size(q[3])})" for q in qualities
            ])
            selection_msg = await update.message.reply_text(
                f"👋 Hello <b>{html_escape(user_name)}</b>!\n\n"
                f"🎬 <b>{html_escape(generate_display_title(title))}</b>\n\n"
                f"📊 <b>Available Qualities:</b>\n{quality_list}\n\n"
                f"⚠️ Videos <b>{AUTO_DELETE_TIME // 60} min</b> mein auto-delete ho jayengi!\n"
                f"💾 Jaldi se <b>Saved Messages</b> mein forward kar lo!",
                parse_mode='HTML'
            )
            asyncio.create_task(schedule_delete(context, chat_id, selection_msg.message_id, TEXT_DELETE_TIME))

            # Send all qualities
            all_sent_ids = []
            for quality in qualities:
                msg_ids = await send_video_to_user(
                    update, context, chat_id, user_name, title, quality, return_msg_id=True
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

        except ValueError:
            err = await update.message.reply_text("❌ Invalid video ID.")
            asyncio.create_task(schedule_delete(context, chat_id, err.message_id, TEXT_DELETE_TIME))
        except Exception as e:
            logger.error(f"Provider Error: {e}")
            err = await update.message.reply_text("❌ Something went wrong. Try again.")
            asyncio.create_task(schedule_delete(context, chat_id, err.message_id, TEXT_DELETE_TIME))
        return

    # Normal /start - Welcome message
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
        f"  • Auto-delete for privacy\n"
        f"  • Priority support"
        f"{sub_status}\n\n"
        f"👇 Neeche menu se option select karein:",
        parse_mode='HTML',
        reply_markup=reply_markup
    )
    asyncio.create_task(schedule_delete(context, chat_id, welcome.message_id, TEXT_DELETE_TIME))
    asyncio.create_task(schedule_delete(context, chat_id, update.message.message_id, TEXT_DELETE_TIME))


async def provider_handle_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = msg.chat_id
    user = update.effective_user
    user_name = user.first_name

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
            
            # 🔥 AUTO-VERIFICATION DATA STORE
            context.user_data['expected_amount'] = float(amount)
            context.user_data['expected_upi'] = UPI_ID.lower()
            context.user_data['qr_generated_at'] = datetime.now()
            context.user_data['expected_note'] = note

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

        # ⚠️ CP Payment Approval
    if data.startswith("approve_cp_"):
        if user.id != ADMIN_USER_ID:
            await query.answer("❌ Admin only!", show_alert=True)
            return
        
        parts = data.split("_")
        target_user_id = int(parts[2])
        cp_id = int(parts[3])
        
        # Database mein mark karo
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO cp_purchases (user_id, cp_id, payment_verified)
                   VALUES (%s, %s, TRUE)
                   ON CONFLICT DO NOTHING""",
                (target_user_id, cp_id)
            )
            conn.commit()
            cur.close()
            logger.info(f"CP purchase approved: User {target_user_id}, CP {cp_id}")
        except Exception as e:
            logger.error(f"CP approval DB error: {e}")
            await query.edit_message_caption(
                caption=query.message.caption + f"\n\n❌ DB Error: {e}",
                parse_mode='HTML'
            )
            return
        finally:
            if conn:
                db_pool.putconn(conn)
        
        await query.edit_message_caption(
            caption=query.message.caption + "\n\n✅ <b>CP APPROVED & SENT!</b>",
            parse_mode='HTML'
        )
        
        # User ko video bhejo
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT title, video_file_url FROM cp_videos WHERE cp_id = %s",
                (cp_id,)
            )
            cp_data = cur.fetchone()
            cur.close()
            db_pool.putconn(conn)
            
            if not cp_data:
                raise Exception("Video not found")
            
            title, video_url = cp_data
            backup_ch, backup_msg = parse_file_url(video_url)
            
            # Notification
            await context.bot.send_message(
                chat_id=target_user_id,
                text=(
                    f"✅ <b>Payment Approved!</b>\n\n"
                    f"🎬 {html_escape(title)}\n\n"
                    f"📹 Sending video...\n"
                    f"⚠️ Will auto-delete in {AUTO_DELETE_TIME // 60} min!"
                ),
                parse_mode='HTML'
            )
            
            # Video send
            copied = await context.bot.copy_message(
                chat_id=target_user_id,
                from_chat_id=backup_ch,
                message_id=backup_msg,
                caption=(
                    f"🔞 <b>{html_escape(title)}</b>\n\n"
                    f"⚠️ Auto-delete: {AUTO_DELETE_TIME // 60} min\n"
                    f"💾 Save NOW!"
                ),
                parse_mode='HTML'
            )
            
            # Auto-delete schedule
            asyncio.create_task(
                auto_delete_with_notification(
                    context, target_user_id, [copied.message_id], AUTO_DELETE_TIME
                )
            )
            
        except Exception as e:
            logger.error(f"CP video send error: {e}")
            await context.bot.send_message(
                chat_id=target_user_id,
                text=f"❌ Video send failed!\n\nContact @{ADMIN_USERNAME}",
                parse_mode='HTML'
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
                INSERT INTO subscribers (user_id, end_date, notified, expiry_warned)
                VALUES (%s, %s, FALSE, FALSE)
                ON CONFLICT (user_id)
                DO UPDATE SET end_date = %s, notified = FALSE, expiry_warned = FALSE, start_date = CURRENT_TIMESTAMP
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

    # ---------- Menu Button Handling ----------
    if text == "💎 Buy VIP":
        await provider_handle_buy(update, context)
        return

    elif text == "🆓 Free Channel":
        info_msg = await msg.reply_text(
            f"🆓 <b>Join our Free Channel:</b>\n👉 {FREE_CHANNEL_LINK}",
            parse_mode='HTML'
        )
        asyncio.create_task(schedule_delete(context, chat_id, info_msg.message_id, TEXT_DELETE_TIME))
        asyncio.create_task(schedule_delete(context, chat_id, msg.message_id, TEXT_DELETE_TIME))
        return

    elif text == "👨‍💻 Contact Admin":
        info_msg = await msg.reply_text(
            f"👨‍💻 Admin se yahan baat karein:\n👉 @{ADMIN_USERNAME}",
            parse_mode='HTML'
        )
        asyncio.create_task(schedule_delete(context, chat_id, info_msg.message_id, TEXT_DELETE_TIME))
        asyncio.create_task(schedule_delete(context, chat_id, msg.message_id, TEXT_DELETE_TIME))
        return

        # ---------- CP Video ID handling ----------
    if text.isdigit():
        cp_id = int(text)
        # Check if it's a valid CP video
        conn = None
        exists = False
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT cp_id FROM cp_videos WHERE cp_id = %s", (cp_id,))
            if cur.fetchone():
                exists = True
            cur.close()
        except:
            pass
        finally:
            if conn:
                db_pool.putconn(conn)
                
        if exists:
            await show_cp_payment(update, context, cp_id)
            return
    # ---------- Payment Flow: UTR Step (Auto-Verification) ----------
    if payment_step == 'utr':
        utr_number = msg.text.strip()
        user_id = user.id

        # Basic UTR validation
        if len(utr_number) < 6 or not utr_number.isdigit():
            err = await msg.reply_text("❌ Invalid UTR! It should be a 12-digit number.\n❌ Cancel: /cancel")
            asyncio.create_task(schedule_delete(context, chat_id, err.message_id, TEXT_DELETE_TIME))
            asyncio.create_task(schedule_delete(context, chat_id, msg.message_id, TEXT_DELETE_TIME))
            return

        # QR expiry check (10 minutes)
        qr_time = context.user_data.get('qr_generated_at')
        if not qr_time or (datetime.now() - qr_time) > timedelta(minutes=10):
            err = await msg.reply_text("❌ QR Code expired! Please start again with /start → Buy.")
            asyncio.create_task(schedule_delete(context, chat_id, err.message_id, TEXT_DELETE_TIME))
            asyncio.create_task(schedule_delete(context, chat_id, msg.message_id, TEXT_DELETE_TIME))
            context.user_data.clear()
            return

        expected_amount = context.user_data.get('expected_amount')
        expected_upi = context.user_data.get('expected_upi')
        expected_note = context.user_data.get('expected_note')
        screenshot_id = context.user_data.get('screenshot_id')

        if not all([expected_amount, expected_upi, expected_note, screenshot_id]):
            err = await msg.reply_text("⚠️ Session expired. Please restart.")
            asyncio.create_task(schedule_delete(context, chat_id, err.message_id, TEXT_DELETE_TIME))
            context.user_data.clear()
            return

        # Admin ko optional notification
        try:
            await context.bot.send_photo(
                chat_id=ADMIN_USER_ID,
                photo=screenshot_id,
                caption=(
                    f"🤖 <b>AUTO-APPROVED PAYMENT</b>\n\n"
                    f"👤 {html_escape(user.first_name)}\n"
                    f"🆔 <code>{user_id}</code>\n"
                    f"🔢 UTR: <code>{utr_number}</code>\n"
                    f"💰 Amount: ₹{expected_amount}\n"
                    f"📝 Note: <code>{expected_note}</code>"
                ),
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Admin notification failed: {e}")

        # Auto-Approve and deliver
        if context.user_data.get('cp_payment_mode'):
            cp_id = context.user_data.get('cp_id')
            await deliver_cp_video(update, context, user_id, cp_id)
        else:
            days = 30  # Change as needed
            await auto_approve_subscription(update, context, user_id, days, expected_amount)

        # Cleanup session
        context.user_data.clear()
        return

    # ---------- Other Payment Step ----------
    if payment_step == 'screenshot':
        photo_err = await msg.reply_text(
            "❌ Photo chahiye! Payment ka <b>Screenshot (photo)</b> bhejein.\n\n"
            "❌ Cancel: /cancel",
            parse_mode='HTML'
        )
        asyncio.create_task(schedule_delete(context, chat_id, photo_err.message_id, TEXT_DELETE_TIME))
        asyncio.create_task(schedule_delete(context, chat_id, msg.message_id, TEXT_DELETE_TIME))
        return

    # ---------- Default Response ----------
    normal_err = await msg.reply_text(
        "🤔 Samajh nahi aaya.\n\n"
        "👉 Niche diye gaye menu buttons ka istemal karein.\n"
        "👉 Agar video link chahiye toh Free channel se link copy karein."
    )
    asyncio.create_task(schedule_delete(context, chat_id, normal_err.message_id, TEXT_DELETE_TIME))
    asyncio.create_task(schedule_delete(context, chat_id, msg.message_id, TEXT_DELETE_TIME))


# ================================================================
#   SEND VIDEO TO USER (ENHANCED)
# ================================================================
# ================================================================
#           PROVIDER BOT: CP FEATURES (ILLEGAL)
# ================================================================

async def show_cp_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = msg.chat_id
    user = update.effective_user
    user_name = user.first_name

    logger.info(f"CP list requested by user {user.id} ({user_name})")

    conn = None
    cp_videos = []
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT cp_id, title, poster_file_id, price FROM cp_videos ORDER BY created_at DESC LIMIT 50"
        )
        cp_videos = cur.fetchall()
        cur.close()
    except Exception as e:
        logger.error(f"CP list DB error: {e}")
        await msg.reply_text("❌ Database error. Try again later.")
        return
    finally:
        if conn:
            db_pool.putconn(conn)

    if not cp_videos:
        await msg.reply_text("❌ <b>No videos available!</b>\n\n🔔 Check back later.", parse_mode='HTML')
        asyncio.create_task(schedule_delete(context, chat_id, msg.message_id, TEXT_DELETE_TIME))
        return

    # Delete start message
    asyncio.create_task(schedule_delete(context, chat_id, msg.message_id, 5))

    # Loading message
    loading_msg = await msg.reply_text("🔞 <b>EXCLUSIVE COLLECTION</b>\n\n🖼️ <i>Converting posters...</i>", parse_mode='HTML')

    # 🔥 STEP 1: Convert ALL thumbnails to valid photo file_ids FIRST
    valid_media = []
    conversion_success = 0
    
    for cp_id, title, poster_id, price in cp_videos:
        if not poster_id:
            logger.warning(f"CP {cp_id}: No poster, skipping")
            continue
            
        valid_photo_id = None
        
        try:
            # Convert thumbnail → valid photo file_id
            temp_msg = await context.bot.send_photo(
                chat_id=chat_id,
                photo=poster_id,
                caption=""
            )
            valid_photo_id = temp_msg.photo[-1].file_id  # ✅ This is VALID photo file_id
            await temp_msg.delete()  # Clean up temp message
            conversion_success += 1
            logger.info(f"✅ CP {cp_id}: Thumbnail converted to photo")
        except Exception as e:
            logger.error(f"❌ CP {cp_id} conversion failed: {e}")
            continue
        
        # Build caption for media group
        caption = (
            f"🎬 <b>{html_escape(title[:55])}</b>\n"
            f"💰 Price: <b>₹{price}</b>\n"
            f"🆔 ID: <code>{cp_id}</code>"
        )
        
        valid_media.append(
            InputMediaPhoto(
                media=valid_photo_id,  # ✅ Now using VALID photo file_id
                caption=caption,
                parse_mode='HTML'
            )
        )
    
    await loading_msg.delete()

    if not valid_media:
        await msg.reply_text(
            "❌ <b>No valid posters available!</b>\n\n"
            "Admin ko batao posters missing hain.",
            parse_mode='HTML'
        )
        return

    logger.info(f"✅ {conversion_success}/{len(cp_videos)} posters converted successfully")

    # 🔥 STEP 2: Send media groups (max 10 per group)
    try:
        chunk_size = 10
        for i in range(0, len(valid_media), chunk_size):
            chunk = valid_media[i:i+chunk_size]
            await context.bot.send_media_group(chat_id=chat_id, media=chunk)
            logger.info(f"✅ Sent media group {i//chunk_size + 1} ({len(chunk)} items)")
            await asyncio.sleep(1)  # Rate limit protection
        
    except Exception as e:
        logger.error(f"❌ Media group send failed: {e}")
        await msg.reply_text("❌ Failed to send posters. Try /start cp_list again.")
        return

    # 🔥 STEP 3: Instructions
    instruction = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "👉 <b>How to Buy:</b>\n\n"
            "1️⃣ Copy Video ID from poster caption\n"
            "2️⃣ Send that ID here\n"
            "3️⃣ Get payment QR\n\n"
            f"📌 <b>Example:</b> <code>123</code>\n\n"
            f"🔞 Total: {len(valid_media)} videos"
        ),
        parse_mode='HTML',
        disable_web_page_preview=True
    )
    asyncio.create_task(schedule_delete(context, chat_id, instruction.message_id, TEXT_DELETE_TIME * 2))
    
    # Prepare media group
    media_group = []
    for (cp_id, title, poster_id, price) in cp_videos:
        if not poster_id:
            logger.warning(f"CP video {cp_id} has no poster, skipping from album")
            continue
            
        caption = (
            f"🎬 <b>{html_escape(title[:60])}</b>\n"
            f"💰 Price: <b>₹{price}</b>\n"
            f"🆔 Video ID: <code>{cp_id}</code>"
        )
        
        media_group.append(
            InputMediaPhoto(
                media=poster_id,
                caption=caption,
                parse_mode='HTML'
            )
        )
    
    if not media_group:
        await msg.reply_text("❌ No posters available to display.")
        return
        
    try:
        # Send album (max 10 media per group, Telegram limit)
        chunk_size = 10
        for i in range(0, len(media_group), chunk_size):
            chunk = media_group[i:i+chunk_size]
            await context.bot.send_media_group(chat_id=chat_id, media=chunk)
            await asyncio.sleep(1)  # Rate limit से बचने के लिए
        
        # Instruction message
        instruction = await msg.reply_text(
            f"👉 <b>How to Buy:</b>\n\n"
            f"1️⃣ Find the Video ID in the caption of the poster you like.\n"
            f"2️⃣ Send that Video ID to me here.\n"
            f"3️⃣ I will give you the payment QR.\n\n"
            f"📌 <i>Example: just type</i> <code>123</code>",
            parse_mode='HTML'
        )
        asyncio.create_task(schedule_delete(context, chat_id, instruction.message_id, TEXT_DELETE_TIME * 2))
        
    except Exception as e:
        logger.error(f"Failed to send CP media group: {e}")
        await msg.reply_text("❌ Failed to send posters. Please try again later.")


async def show_cp_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, cp_id: int):
    """CP video payment QR"""
    msg = update.message
    chat_id = msg.chat_id
    user = update.effective_user
    user_name = user.first_name
    
    # Check already purchased
    conn = None
    already_bought = False
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT purchase_id FROM cp_purchases WHERE user_id = %s AND cp_id = %s AND payment_verified = TRUE",
            (user.id, cp_id)
        )
        if cur.fetchone():
            already_bought = True
        cur.close()
    except:
        pass
    finally:
        if conn:
            db_pool.putconn(conn)
    
    if already_bought:
        await send_cp_video(update, context, cp_id)
        return
    
    # Fetch CP details
    conn = None
    cp_data = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT title, price FROM cp_videos WHERE cp_id = %s",
            (cp_id,)
        )
        cp_data = cur.fetchone()
        cur.close()
    except:
        pass
    finally:
        if conn:
            db_pool.putconn(conn)
    
    if not cp_data:
        await msg.reply_text("❌ Video not found!")
        return
    
    title, price = cp_data
    
    # Payment mode activate
    context.user_data['cp_payment_mode'] = True
    context.user_data['cp_id'] = cp_id
    context.user_data['payment_step'] = 'screenshot'
    
    # Delete start message
    asyncio.create_task(schedule_delete(context, chat_id, msg.message_id, 5))
    
    # QR generate
    try:
        if QR_AVAILABLE:
            qr_image, note = generate_upi_qr(user.id, user_name, price)
            context.user_data['payment_note'] = note
            
            # 🔥 AUTO-VERIFICATION DATA STORE (CP)
            context.user_data['expected_amount'] = float(price)
            context.user_data['expected_upi'] = UPI_ID.lower()
            context.user_data['qr_generated_at'] = datetime.now()
            context.user_data['expected_note'] = note
            
            qr_msg = await msg.reply_photo(
                photo=qr_image,
                caption=(
                    f"🔞 <b>{html_escape(title[:50])}</b>\n\n"
                    f"💰 Price: <b>₹{price}</b>\n\n"
                    f"📱 Scan QR & pay ₹{price}\n"
                    f"📝 Note: <code>{note}</code> (auto-filled)\n\n"
                    f"✅ After payment:\n"
                    f"1️⃣ Send screenshot\n"
                    f"2️⃣ Send UTR number\n\n"
                    f"❌ Cancel: /cancel"
                ),
                parse_mode='HTML'
            )
            asyncio.create_task(schedule_delete(context, chat_id, qr_msg.message_id, QR_DELETE_TIME))
        else:
            raise Exception("QR unavailable")
    except:
        note = f"CP-{user.id}-{cp_id}"
        context.user_data['payment_note'] = note
        fallback = await msg.reply_text(
            f"🔞 <b>{html_escape(title[:50])}</b>\n\n"
            f"💰 Price: <b>₹{price}</b>\n\n"
            f"💳 UPI: <code>{UPI_ID}</code>\n"
            f"📝 Note: <code>{note}</code>\n\n"
            f"Steps:\n"
            f"1️⃣ Pay ₹{price}\n"
            f"2️⃣ Screenshot bhejo\n"
            f"3️⃣ UTR bhejo\n\n"
            f"❌ Cancel: /cancel",
            parse_mode='HTML'
        )
        asyncio.create_task(schedule_delete(context, chat_id, fallback.message_id, QR_DELETE_TIME))


async def send_cp_video(update, context, cp_id):
    """Send CP video after verification"""
    msg = update.message
    chat_id = msg.chat_id
    
    # Fetch video
    conn = None
    cp_data = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT title, video_file_url FROM cp_videos WHERE cp_id = %s",
            (cp_id,)
        )
        cp_data = cur.fetchone()
        cur.close()
    except:
        pass
    finally:
        if conn:
            db_pool.putconn(conn)
    
    if not cp_data:
        await msg.reply_text("❌ Video not found!")
        return
    
    title, video_url = cp_data
    backup_ch, backup_msg = parse_file_url(video_url)
    
    if not backup_ch or not backup_msg:
        await msg.reply_text("❌ Invalid video!")
        return
    
    # Send video
    status = await msg.reply_text("📹 Sending video...")
    try:
        copied = await context.bot.copy_message(
            chat_id=chat_id,
            from_chat_id=backup_ch,
            message_id=backup_msg,
            caption=(
                f"🔞 <b>{html_escape(title)}</b>\n\n"
                f"⚠️ Auto-delete in {AUTO_DELETE_TIME // 60} min\n"
                f"💾 Save to Saved Messages!"
            ),
            parse_mode='HTML'
        )
        
        await status.delete()
        
        # Auto-delete
        asyncio.create_task(
            auto_delete_with_notification(
                context, chat_id, [copied.message_id], AUTO_DELETE_TIME
            )
        )
        
    except Exception as e:
        await status.edit_text(f"❌ Send failed: {e}\n\nContact @{ADMIN_USERNAME}")
        
async def send_video_to_user(update, context, chat_id, user_name, title,
                             quality_data, is_callback=False, return_msg_id=False):
    q_id, q_label, file_url, file_size = quality_data

    is_old_file_id = not file_url.startswith("https://t.me/c/") if file_url else False

    join_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Buy VIP Access",
                              url=f"https://t.me/{PROVIDER_BOT_USERNAME}?start=buy")],
        [InlineKeyboardButton("🆓 Join Free Channel", url=FREE_CHANNEL_LINK)]
    ])

    sent_msg_id = None

    if is_old_file_id:
        try:
            fallback = await context.bot.send_video(
                chat_id=chat_id, video=file_url, caption="",
                reply_markup=join_keyboard, supports_streaming=True
            )
            sent_msg_id = fallback.message_id
        except Exception as e:
            logger.error(f"Old file_id send failed: {e}")
    else:
        backup_channel_id, backup_msg_id = parse_file_url(file_url)
        if backup_channel_id and backup_msg_id:
            try:
                copied = await context.bot.copy_message(
                    chat_id=chat_id, from_chat_id=backup_channel_id, message_id=backup_msg_id,
                    caption="",
                    reply_markup=join_keyboard
                )
                sent_msg_id = copied.message_id
            except Exception as e:
                logger.error(f"Copy message failed for user {chat_id}: {e}")

    if sent_msg_id and not return_msg_id:
        asyncio.create_task(
            auto_delete_with_notification(
                context=context, chat_id=chat_id,
                message_ids_to_delete=[sent_msg_id], delete_time=AUTO_DELETE_TIME
            )
        )

    return sent_msg_id

async def auto_approve_subscription(update, context, user_id, days, amount):
    """Auto-approve VIP subscription without admin intervention"""
    end_date = datetime.now() + timedelta(days=days)
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO subscribers (user_id, end_date, notified, expiry_warned)
            VALUES (%s, %s, FALSE, FALSE)
            ON CONFLICT (user_id) DO UPDATE 
            SET end_date = %s, notified = FALSE, expiry_warned = FALSE, start_date = CURRENT_TIMESTAMP
        """, (user_id, end_date, end_date))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Auto-approve DB error: {e}")
        await update.message.reply_text("❌ Server error. Contact admin.")
        return
    finally:
        if conn:
            db_pool.putconn(conn)

    # User ko success message
    invite_link = None
    if PAID_CH != 0:
        try:
            invite = await context.bot.create_chat_invite_link(
                chat_id=PAID_CH,
                member_limit=1,
                expire_date=datetime.now() + timedelta(days=1),
                name=f"VIP-{user_id}"
            )
            invite_link = invite.invite_link
        except:
            pass

    success_msg = await update.message.reply_text(
        f"✅ <b>Payment Verified!</b>\n\n"
        f"💰 Amount: ₹{amount}\n"
        f"📅 VIP valid till: {end_date.strftime('%d-%m-%Y')}\n\n"
        f"{'🔗 Join: ' + invite_link if invite_link else 'Admin will contact you.'}",
        parse_mode='HTML'
    )
    asyncio.create_task(schedule_delete(context, update.effective_chat.id, success_msg.message_id, TEXT_DELETE_TIME))

async def deliver_cp_video(update, context, user_id, cp_id):
    """Deliver CP video after auto-verification"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT title, video_file_url FROM cp_videos WHERE cp_id = %s", (cp_id,))
        row = cur.fetchone()
        cur.close()
    except Exception as e:
        logger.error(f"CP fetch error: {e}")
        await update.message.reply_text("❌ Video not found.")
        return
    finally:
        if conn:
            db_pool.putconn(conn)

    if not row:
        await update.message.reply_text("❌ Video not found.")
        return

    title, file_url = row
    backup_ch, backup_msg = parse_file_url(file_url)

    if not backup_ch or not backup_msg:
        await update.message.reply_text("❌ Invalid video link.")
        return

    # Send video
    try:
        copied = await context.bot.copy_message(
            chat_id=update.effective_chat.id,
            from_chat_id=backup_ch,
            message_id=backup_msg,
            caption=(
                f"🔞 <b>{html_escape(title)}</b>\n\n"
                f"⚠️ Auto-delete in {AUTO_DELETE_TIME // 60} min\n"
                f"💾 Save to Saved Messages!"
            ),
            parse_mode='HTML'
        )
        # Auto-delete schedule
        asyncio.create_task(
            auto_delete_with_notification(
                context, update.effective_chat.id, [copied.message_id], AUTO_DELETE_TIME
            )
        )
        # Mark as purchased
        conn2 = None
        try:
            conn2 = get_db_connection()
            cur2 = conn2.cursor()
            cur2.execute(
                "INSERT INTO cp_purchases (user_id, cp_id, payment_verified) VALUES (%s, %s, TRUE) ON CONFLICT DO NOTHING",
                (user_id, cp_id)
            )
            conn2.commit()
            cur2.close()
        except Exception as e:
            logger.error(f"CP purchase record error: {e}")
        finally:
            if conn2:
                db_pool.putconn(conn2)

    except Exception as e:
        logger.error(f"CP video send error: {e}")
        await update.message.reply_text(f"❌ Delivery failed: {e}\n\nContact @{ADMIN_USERNAME}")
# ================= BACKGROUND TASKS =================

async def periodic_cleanup(context):
    """Purane records saaf karo periodically"""
    while True:
        await asyncio.sleep(3600)
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("DELETE FROM adult_videos WHERE created_at < NOW() - INTERVAL '7 days'")
            deleted = cur.rowcount
            
            # Purane delete queue entries bhi saaf karo
            cur.execute("DELETE FROM auto_delete_queue WHERE delete_at < NOW() - INTERVAL '1 hour'")
            cleaned_queue = cur.rowcount
            
            conn.commit()
            cur.close()
            if deleted > 0 or cleaned_queue > 0:
                logger.info(f"🧹 Cleaned: {deleted} old videos, {cleaned_queue} stale queue entries")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        finally:
            if conn:
                db_pool.putconn(conn)


async def persistent_auto_delete_worker(app: Application):
    """Background worker: restart ke baad bhi messages delete karega"""
    await asyncio.sleep(10)
    logger.info("🧹 Persistent Auto-Delete Worker Started!")

    while True:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()

            cur.execute(
                "SELECT id, chat_id, message_id FROM auto_delete_queue WHERE delete_at <= NOW() LIMIT 50"
            )
            rows = cur.fetchall()

            if rows:
                for row_id, chat_id, msg_id in rows:
                    try:
                        await app.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                        logger.info(f"🧹 Worker deleted missed msg {msg_id} from {chat_id}")
                    except Exception:
                        pass

                    cur.execute("DELETE FROM auto_delete_queue WHERE id = %s", (row_id,))

                conn.commit()
            cur.close()
        except Exception as e:
            logger.error(f"Auto-delete worker error: {e}")
        finally:
            if conn:
                db_pool.putconn(conn)

        await asyncio.sleep(10)


async def schedule_daily_check(provider_app_instance: Application):
    """Daily subscription check at 2:00 AM IST"""
    await asyncio.sleep(60)
    logger.info("🕒 SUBSCRIPTION CHECKER STARTED")

    # Test check on startup
    try:
        await check_expiring_soon(provider_app_instance)
        await check_expired_subscriptions(provider_app_instance)
        logger.info("✅ Initial test check completed")
    except Exception as e:
        logger.error(f"❌ Initial test check failed: {e}")

    while True:
        try:
            from datetime import timezone, timedelta as td
            ist_tz = timezone(td(hours=5, minutes=30))
            now_ist = datetime.now(ist_tz)

            target_time = now_ist.replace(hour=2, minute=0, second=0, microsecond=0)
            if now_ist >= target_time:
                target_time += timedelta(days=1)

            wait_seconds = (target_time - now_ist).total_seconds()
            logger.info(f"⏰ Next check: {target_time.strftime('%d-%m-%Y %H:%M IST')} ({int(wait_seconds)}s)")

            await asyncio.sleep(wait_seconds)

            logger.info("🔍 RUNNING DAILY SUBSCRIPTION CHECKS")
            await check_expiring_soon(provider_app_instance)
            await check_expired_subscriptions(provider_app_instance)
            logger.info("✅ DAILY CHECK COMPLETED")

        except Exception as e:
            logger.error(f"❌ SCHEDULER ERROR: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(3600)


async def check_expiring_soon(provider_app_instance: Application):
    """24h mein expire hone wale subscriptions ko warn karo"""
    conn = None
    count = 0
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT user_id, end_date, first_name FROM subscribers
            WHERE end_date > NOW()
            AND end_date <= NOW() + INTERVAL '24 hours'
            AND expiry_warned = FALSE
        """)
        expiring_users = cur.fetchall()
        logger.info(f"   Found {len(expiring_users)} expiring subscriptions")

        for (user_id, end_date, first_name) in expiring_users:
            hours_left = int((end_date - datetime.now()).total_seconds() / 3600)

            try:
                await provider_app_instance.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "⚠️ <b>Subscription Expiring Soon!</b>\n\n"
                        f"📅 Expires: {end_date.strftime('%d-%m-%Y %H:%M')}\n"
                        f"⏰ Time Left: ~{hours_left} hours\n\n"
                        "🔁 Renew: /start → Buy VIP\n\n"
                        f"❓ Help: @{ADMIN_USERNAME}"
                    ),
                    parse_mode='HTML'
                )
                count += 1
            except Exception as e:
                logger.error(f"   Failed to warn user {user_id}: {e}")

            try:
                await provider_app_instance.bot.send_message(
                    chat_id=ADMIN_USER_ID,
                    text=(
                        f"⚠️ <b>USER PLAN EXPIRING SOON</b>\n\n"
                        f"👤 ID: <code>{user_id}</code> | Name: {first_name or 'Unknown'}\n"
                        f"📅 Expiry: {end_date.strftime('%d-%m-%Y %H:%M')} | Hours: ~{hours_left}"
                    ),
                    parse_mode='HTML'
                )
            except:
                pass

            cur.execute("UPDATE subscribers SET expiry_warned = TRUE WHERE user_id = %s", (user_id,))
            conn.commit()
            await asyncio.sleep(2)

        logger.info(f"   ✅ Processed {count} expiring subscriptions")
        cur.close()
    except Exception as e:
        logger.error(f"   ❌ Expiring check error: {e}")
    finally:
        if conn:
            db_pool.putconn(conn)


async def check_expired_subscriptions(provider_app_instance: Application):
    """Expired subscriptions ko notify karo"""
    conn = None
    count = 0
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT user_id, end_date, first_name FROM subscribers
            WHERE end_date <= NOW()
            AND end_date > NOW() - INTERVAL '7 days'
            AND notified = FALSE
        """)
        expired_users = cur.fetchall()
        logger.info(f"   Found {len(expired_users)} expired subscriptions")

        for (user_id, end_date, first_name) in expired_users:
            try:
                await provider_app_instance.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "❌ <b>VIP Subscription Expired!</b>\n\n"
                        f"📅 Expired on: {end_date.strftime('%d-%m-%Y %H:%M')}\n\n"
                        "🔁 Renew: /start → Buy VIP\n\n"
                        f"❓ Help: @{ADMIN_USERNAME}"
                    ),
                    parse_mode='HTML'
                )
                count += 1
            except Exception as e:
                logger.error(f"   Failed to notify user {user_id}: {e}")

            try:
                await provider_app_instance.bot.send_message(
                    chat_id=ADMIN_USER_ID,
                    text=(
                        f"🔔 <b>USER PLAN EXPIRED</b>\n\n"
                        f"👤 ID: <code>{user_id}</code> | Name: {first_name or 'Unknown'}\n"
                        f"📅 Expired: {end_date.strftime('%d-%m-%Y %H:%M')}"
                    ),
                    parse_mode='HTML'
                )
            except:
                pass

            cur.execute("UPDATE subscribers SET notified = TRUE WHERE user_id = %s", (user_id,))
            conn.commit()
            await asyncio.sleep(2)

        logger.info(f"   ✅ Processed {count} expired subscriptions")
        cur.close()
    except Exception as e:
        logger.error(f"   ❌ Expired check error: {e}")
    finally:
        if conn:
            db_pool.putconn(conn)


# ============ TESTING COMMAND (ADMIN ONLY) ============
async def test_expiry_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual trigger for testing expiry notifications"""
    if update.effective_user.id not in ADMIN_USER_IDS:
        await update.message.reply_text("❌ Admin only!")
        return

    status_msg = await update.message.reply_text("🔍 Running manual expiry check...")

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT user_id, end_date, expiry_warned FROM subscribers
            WHERE end_date > NOW()
            AND end_date <= NOW() + INTERVAL '24 hours'
        """)
        expiring = cur.fetchall()

        cur.execute("""
            SELECT user_id, end_date, notified FROM subscribers
            WHERE end_date <= NOW()
            AND end_date > NOW() - INTERVAL '7 days'
        """)
        expired = cur.fetchall()

        cur.close()

        result_text = f"📊 <b>Manual Check Results:</b>\n\n"
        result_text += f"⚠️ <b>Expiring Soon (24h):</b> {len(expiring)}\n"
        for (uid, edate, warned) in expiring:
            hours = int((edate - datetime.now()).total_seconds() / 3600)
            result_text += f"  • User {uid}: {hours}h left (warned={warned})\n"

        result_text += f"\n❌ <b>Expired:</b> {len(expired)}\n"
        for (uid, edate, notif) in expired:
            result_text += f"  • User {uid}: {edate.strftime('%d-%m-%Y')} (notified={notif})\n"

        if not expiring and not expired:
            result_text += "\n✅ No subscriptions need notifications right now."

        await status_msg.edit_text(result_text, parse_mode='HTML')

    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {e}")
    finally:
        if conn:
            db_pool.putconn(conn)


# ================================================================
#                   RUN BOTH BOTS
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
    
    # ⚠️ CP Conversation Handler
    cp_conv = ConversationHandler(
        entry_points=[CommandHandler('cp', start_cp_upload)],
        states={
            CP_WAIT_VIDEO: [
                CommandHandler('cancel', cancel_cp_flow),
                CommandHandler('start', admin_start),
                MessageHandler(filters.ALL & ~filters.COMMAND, get_cp_video),
            ],
            CP_WAIT_AMOUNT: [
                CommandHandler('cancel', cancel_cp_flow),
                CommandHandler('start', admin_start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_cp_amount),
            ]
        },
        fallbacks=[
            CommandHandler('cancel', cancel_cp_flow),
            CommandHandler('start', admin_start),
        ],
        allow_reentry=True
    )
    
    # Ye lines ab properly 4-space indented hain
    main_app.add_handler(cp_conv)
    main_app.add_handler(upload_conv)   # 👈 Yeh line add karo

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
    main_app.add_handler(CommandHandler('check_expiry', test_expiry_check))
    main_app.add_handler(CommandHandler('stats', admin_stats))
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
        print("🎉 BOTH BOTS RUNNING SUCCESSFULLY! v3.0 Enhanced")
        print("=" * 60)
        print(f"👤 Admin User IDs: {ADMIN_USER_IDS}")
        print(f"🤖 Provider Bot: @{PROVIDER_BOT_USERNAME}")
        print(f"📦 Backup Channel: {BACKUP_1}")
        print(f"🆓 Free Channel: {FREE_CH}")
        print(f"💎 Paid Channel: {PAID_CH}")
        print(f"🕒 Video Auto-Delete: {AUTO_DELETE_TIME}s")
        print(f"📝 Text Delete: {TEXT_DELETE_TIME}s")
        print(f"🖼️ PIL Available: {PIL_AVAILABLE}")
        print(f"📱 QR Available: {QR_AVAILABLE}")
        print("=" * 60)

        # Background tasks
        print("\n🔄 Starting background tasks...")
        asyncio.create_task(periodic_cleanup(None))
        asyncio.create_task(schedule_daily_check(provider_app))
        asyncio.create_task(persistent_auto_delete_worker(provider_app))
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
#                   MAIN ENTRY POINT
# ================================================================

if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("🔍 PRE-FLIGHT CHECKS v3.0")
    print("=" * 60)

    required = ['MAIN_BOT_TOKEN', 'PROVIDER_BOT_TOKEN', 'DATABASE_URL']
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"❌ ERROR: Missing environment variables: {', '.join(missing)}")
        exit(1)
    print("✅ Required environment variables present")

    if BACKUP_1 == 0:
        print("❌ ERROR: BACKUP_CHANNEL_1 is REQUIRED!")
        exit(1)
    print(f"✅ Backup channel configured: {BACKUP_1}")

    print("\n📊 Initializing database...")
    try:
        init_db_pool()
        setup_db()
        print("✅ Database ready")
    except Exception as e:
        print(f"❌ DATABASE ERROR: {e}")
        exit(1)

    print("\n🌐 Starting web server...")
    flask_thread = Thread(target=run_flask, daemon=False)
    flask_thread.start()
    import time
    time.sleep(2)
    print(f"✅ Web server started on port {os.environ.get('PORT', 8080)}")

    print("\n" + "=" * 60)
    print("🚀 LAUNCHING TELEGRAM BOTS v3.0")
    print("=" * 60)

    try:
        asyncio.run(run_bots())
    except KeyboardInterrupt:
        print("\n\n🛑 Interrupted by user")
    except Exception as e:
        print(f"\n\n❌ FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        exit(1)
