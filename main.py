import os
import re
import json
import asyncio
import logging
import aiohttp
import psycopg2
import qrcode
from html import escape as html_escape
from psycopg2 import pool
from flask import Flask, redirect
from threading import Thread
from datetime import datetime, timedelta
from io import BytesIO
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes, ConversationHandler
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ================= VARIABLES =================
MAIN_BOT_TOKEN = os.environ.get("MAIN_BOT_TOKEN")
PROVIDER_BOT_TOKEN = os.environ.get("PROVIDER_BOT_TOKEN")
PROVIDER_BOT_USERNAME = os.environ.get("PROVIDER_BOT_USERNAME")
GPLINKS_API_KEY = os.environ.get("GPLINKS_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")
WEB_DOMAIN = os.environ.get("WEB_DOMAIN", "https://my-bot.onrender.com").strip()
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))

FREE_CH = int(os.environ.get("FREE_CHANNEL_ID", "0"))
PAID_CH = int(os.environ.get("PAID_CHANNEL_ID", "0"))
BACKUP_1 = int(os.environ.get("BACKUP_CHANNEL_1", "0"))
BACKUP_2 = int(os.environ.get("BACKUP_CHANNEL_2", "0"))

AUTO_DELETE_TIME = int(os.environ.get("AUTO_DELETE_TIME", "300"))
UPI_ID = os.environ.get("UPI_ID", "tumhara@upi")
FREE_CHANNEL_LINK = os.environ.get("FREE_CHANNEL_LINK", "https://t.me/your_free_channel")
SUBSCRIPTION_AMOUNT = os.environ.get("SUBSCRIPTION_AMOUNT", "10")

# ===== MAIN BOT CONVERSATION STATES =====
WAIT_TRIM, WAIT_FULL = range(2)
BULK_WAIT_VIDEO, BULK_CONFIRM = range(2)

# ================= DATABASE SETUP =================
db_pool = None


def init_db_pool():
    global db_pool
    try:
        db_pool = pool.SimpleConnectionPool(1, 20, dsn=DATABASE_URL)
        logger.info("✅ Database pool created successfully")
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

        # ===== MIGRATION: old schema → new schema =====
        try:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'video_qualities'
            """)
            existing_cols = [row[0] for row in cur.fetchall()]

            if existing_cols:
                if 'file_id' in existing_cols and 'file_url' not in existing_cols:
                    cur.execute("ALTER TABLE video_qualities RENAME COLUMN file_id TO file_url")
                    logger.info("✅ Migrated: file_id → file_url")
                elif 'file_url' not in existing_cols:
                    cur.execute("ALTER TABLE video_qualities ADD COLUMN file_url TEXT")
                    logger.info("✅ Added file_url column")
        except Exception as e:
            logger.warning(f"Migration note: {e}")

        conn.commit()
        cur.close()
        logger.info("✅ Database tables created/verified")
    except Exception as e:
        logger.error(f"❌ Database setup failed: {e}")
        if conn:
            conn.rollback()
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
    """
    Generate UPI QR code with pre-filled amount and note.
    Returns BytesIO image object and note string.
    """
    safe_name = re.sub(r'[^a-zA-Z0-9 ]', '', user_name)[:30].strip()
    if not safe_name:
        safe_name = "User"

    note = f"TG-{user_id}-{safe_name}"

    upi_url = (
        f"upi://pay"
        f"?pa={UPI_ID}"
        f"&pn=VIP Subscription"
        f"&am={amount}"
        f"&tn={note}"
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


def build_free_channel_caption(title, gplink, qualities_info):
    safe_title = html_escape(title)
    safe_gplink = html_escape(gplink)
    quality_text = ""
    if qualities_info:
        quality_text = "\n📊 <b>Available Qualities:</b>\n"
        for q in qualities_info:
            quality_text += f"  • {q['label']} ({q['size']})\n"
    return (
        f"🔞 <b>{safe_title}</b>\n"
        f"{quality_text}\n"
        f"🔥 <b>Watch Full Video &amp; Download:</b>\n"
        f"👉 {safe_gplink}\n\n"
        f"𝘼𝙡𝙡 𝙫𝙞𝙙𝙚𝙤 𝙛𝙞𝙡𝙚 𝙙𝙞𝙧𝙚𝙘𝙩𝙡𝙮 - 𝟭𝟬 ₹ 𝐌𝐨𝐧𝐭𝐡𝐥𝐲 \n"
        f"𝐅𝐨𝐫 𝐒𝐮𝐛𝐬𝐜𝐫𝐢𝐩𝐭𝐢𝐨𝐧 𝗠𝗲𝘀𝘀𝗮𝗴𝗲 𝗠𝗲- @BhabhiFilesBot\n"
        f"𝐀𝐧𝐲 𝐏𝐫𝐨𝐛𝐥𝐞𝐦 𝐃𝐌 𝐀𝐝𝐦𝐢𝐧\n"
        f"➲𝐎𝐰𝐧𝐞𝐫  @ownermahi"
    )


def build_backup_caption(title, quality_label=""):
    safe_title = html_escape(title)
    if quality_label:
        return f"🔒 {safe_title} [{quality_label}]"
    return f"🔒 {safe_title}"


async def shorten_link(long_url):
    if not GPLINKS_API_KEY:
        logger.warning("GPLINKS_API_KEY not set, returning original URL")
        return long_url
    api_url = f"https://gplinks.in/api?api={GPLINKS_API_KEY}&url={long_url}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, timeout=10) as resp:
                data = await resp.json()
                if data.get("status") == "success":
                    return data.get("shortenedUrl", long_url)
    except Exception as e:
        logger.error(f"GPLink Error: {e}")
    return long_url


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
            await asyncio.sleep(5)
            await warning_msg.delete()
        except Exception as e:
            logger.error(f"Warning message error: {e}")
        await asyncio.sleep(30)
        for msg_id in message_ids_to_delete:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                logger.info(f"✅ Message {msg_id} deleted for chat: {chat_id}")
            except Exception as e:
                logger.error(f"Failed to delete message {msg_id}: {e}")
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "🗑️ Video(s) Auto-Deleted!\n\n"
                    "✅ Agar forward kar liya hai toh saved messages mein check karein.\n"
                    "❌ Nahi kiya toh dobara link se access karein."
                )
            )
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


# ================= WEB REDIRECTOR (FLASK) =================
app = Flask(__name__)


@app.route('/')
def home():
    return "✅ Server is Running!"


@app.route('/watch/<int:vid_id>')
def watch_video(vid_id):
    bot_username = os.environ.get("PROVIDER_BOT_USERNAME", "your_bot").strip()
    return redirect(f"https://t.me/{bot_username}?start=vid_{vid_id}")


def run_flask():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)


# ================================================================
#                    MAIN BOT (ADMIN ONLY)
# ================================================================

async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text(
            "❌ <b>Access Denied!</b>\n\n"
            "Yeh Admin Bot hai. Sirf admin use kar sakta hai.\n\n"
            "👉 Videos ke liye @BhabhiFilesBot use karein.",
            parse_mode='HTML'
        )
        return ConversationHandler.END

    backup_status = "✅ Set" if BACKUP_1 != 0 else "❌ NOT SET (REQUIRED!)"
    await update.message.reply_text(
        "🤖 <b>Admin Bot Ready!</b>\n\n"
        "📌 <b>Commands:</b>\n"
        "  /post - Single video post\n"
        "  /bulk - Bulk upload multiple videos\n"
        "  /cancel - Cancel current operation\n"
        "  /start - Reset everything\n\n"
        f"📦 Backup Channel: {backup_status}\n"
        "⚡ <b>File URL Mode Active</b> (saves backup channel URLs)\n"
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
        context.user_data['title'] = cleaned_title

        if msg.video:
            context.user_data['trim_type'] = 'video'
        elif msg.animation:
            context.user_data['trim_type'] = 'animation'
        else:
            context.user_data['trim_type'] = 'document'

        context.user_data['trim_chat_id'] = msg.chat_id
        context.user_data['trim_msg_id'] = msg.message_id
        await msg.reply_text(
            f"✅ <b>Trim Video Saved!</b>\n\n"
            f"📝 Title: {html_escape(cleaned_title)}\n\n"
            "🔞 Ab <b>FULL VIDEO(s)</b> bhejo, phir <code>/done</code>\n\n"
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

    # ===== DUPLICATE CHECK: FILE SIZE BASED =====
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
        'msg_id': msg.message_id
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
    total = len(full_videos)
    status = await msg.reply_text(f"⏳ Processing {total} quality(ies)...")

    # Create video entry in DB
    conn = None
    vid_id = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO adult_videos (title) VALUES (%s) RETURNING vid_id",
            (title,)
        )
        vid_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        logger.info(f"✅ Video entry created: ID {vid_id}")
    except Exception as e:
        logger.error(f"❌ DB error: {e}")
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

        # ===== COPY TO BACKUP_1 AND GET file_url =====
        file_url = None
        try:
            copied_msg = await context.bot.copy_message(
                chat_id=BACKUP_1, from_chat_id=src_chat_id,
                message_id=src_msg_id, caption=backup_caption, parse_mode='HTML'
            )
            file_url = construct_file_url(BACKUP_1, copied_msg.message_id)
            logger.info(f"✅ Backup1 OK: {q_label} → {file_url}")
        except Exception as e:
            logger.error(f"❌ Backup1 FAILED for {q_label}: {e}")
            failed_qualities.append(q_label)
            continue

        # Copy to other channels (optional, not stored)
        for ch_id in [ch for ch in [BACKUP_2, PAID_CH] if ch != 0]:
            try:
                await context.bot.copy_message(
                    chat_id=ch_id, from_chat_id=src_chat_id,
                    message_id=src_msg_id, caption=backup_caption, parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Channel {ch_id} copy error: {e}")

        # ===== SAVE file_url TO DATABASE =====
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
            logger.info(f"✅ DB saved: {q_label} → {file_url}")
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
        await status.edit_text(
            "❌ <b>ALL FAILED!</b>\n\n"
            "Koi bhi video backup channel mein copy nahi ho payi.\n"
            "Check: Bot admin hai BACKUP_CHANNEL_1 ka?",
            parse_mode='HTML'
        )
        context.user_data.clear()
        return ConversationHandler.END

    # Generate GPLink
    await status.edit_text("⏳ Generating link...")
    web_link = f"{WEB_DOMAIN}/watch/{vid_id}"
    gplink = await shorten_link(web_link)

    # Post to Free Channel
    await status.edit_text("⏳ Posting to Free Channel...")
    caption = build_free_channel_caption(title, gplink, qualities_info)

    try:
        if FREE_CH != 0:
            trim_chat_id = context.user_data.get('trim_chat_id')
            trim_msg_id = context.user_data.get('trim_msg_id')

            if trim_type in ['photo', 'video', 'animation', 'document']:
                if trim_chat_id and trim_msg_id:
                    try:
                        await context.bot.copy_message(
                            chat_id=FREE_CH, from_chat_id=trim_chat_id,
                            message_id=trim_msg_id, caption=caption, parse_mode='HTML'
                        )
                    except Exception as e:
                        logger.error(f"Free channel trim copy failed: {e}")
                        await context.bot.send_message(
                            chat_id=FREE_CH, text=caption, parse_mode='HTML'
                        )
            elif trim_type == 'skip':
                first_video = full_videos[0]
                try:
                    await context.bot.copy_message(
                        chat_id=FREE_CH, from_chat_id=first_video['chat_id'],
                        message_id=first_video['msg_id'], caption=caption, parse_mode='HTML'
                    )
                except Exception as e:
                    logger.error(f"Free channel video copy failed: {e}")
                    await context.bot.send_message(
                        chat_id=FREE_CH, text=caption, parse_mode='HTML'
                    )
    except Exception as e:
        logger.error(f"Free channel error: {e}")

    # Final report
    quality_list = "\n".join([f"  • {q['label']} ({q['size']})" for q in qualities_info])
    url_list = "\n".join([f"  📎 {q['url']}" for q in qualities_info])
    failed_text = ""
    if failed_qualities:
        failed_text = f"\n⚠️ Failed: {', '.join(failed_qualities)}"

    display_title = generate_display_title(title)
    await status.edit_text(
        f"✅ <b>ALL DONE!</b>\n\n"
        f"🎬 Title: {html_escape(display_title)}\n"
        f"🆔 ID: {vid_id}\n"
        f"🔗 GPLink: {gplink}\n\n"
        f"📊 <b>Qualities:</b>\n{quality_list}\n\n"
        f"📎 <b>Backup URLs:</b>\n{url_list}"
        f"{failed_text}",
        parse_mode='HTML'
    )
    context.user_data.clear()
    return ConversationHandler.END


# ===== BULK UPLOAD =====
async def start_bulk_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Access Denied!")
        return ConversationHandler.END

    if BACKUP_1 == 0:
        await update.message.reply_text(
            "❌ <b>BACKUP_CHANNEL_1 not set!</b>\n\n"
            "File URL mode requires a backup channel.",
            parse_mode='HTML'
        )
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data['bulk_videos'] = {}
    await update.message.reply_text(
        "📦 <b>BULK UPLOAD MODE!</b>\n\n"
        "🎬 Videos ek-ek karke forward karo.\n"
        "📊 Same title = same video group\n"
        "⚠️ Duplicate = same file size → rejected\n\n"
        "📝 /done - Process karo | /cancel - Cancel\n\n"
        "⚡ Pehli video bhejo...",
        parse_mode='HTML'
    )
    return BULK_WAIT_VIDEO


async def process_bulk_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    if msg.text and msg.text.strip().lower() == '/done':
        return await finalize_bulk_upload(update, context)
    if msg.text and msg.text.strip().lower() == '/cancel':
        context.user_data.clear()
        await msg.reply_text("❌ Bulk upload cancelled.")
        return ConversationHandler.END
    if msg.text and msg.text.strip().lower() == '/start':
        context.user_data.clear()
        await msg.reply_text("🔄 Reset!")
        return ConversationHandler.END

    if not msg.video and not msg.document:
        await msg.reply_text("❌ Video bhejo! Ya /done / /cancel")
        return BULK_WAIT_VIDEO

    raw_caption = msg.caption if msg.caption else ""
    title = clean_title(raw_caption)
    video_obj = msg.video
    doc_obj = msg.document
    quality_label, width, height, file_size = detect_quality_label(
        video_obj=video_obj, document_obj=doc_obj, caption=raw_caption
    )
    duration = (video_obj.duration or 0) if video_obj else 0

    video_data = {
        'quality_label': quality_label,
        'width': width, 'height': height, 'file_size': file_size,
        'duration': duration, 'chat_id': msg.chat_id, 'msg_id': msg.message_id
    }

    bulk_videos = context.user_data.get('bulk_videos', {})
    if title not in bulk_videos:
        bulk_videos[title] = []

    # ===== DUPLICATE CHECK: FILE SIZE BASED =====
    existing_sizes = [v['file_size'] for v in bulk_videos[title]]
    if file_size in existing_sizes and file_size > 0:
        await msg.reply_text(
            f"⚠️ <b>Duplicate!</b> {html_escape(generate_display_title(title))}\n"
            f"📦 File size {format_file_size(file_size)} already exists.\n"
            f"Same size = same file. Skip kiya.",
            parse_mode='HTML'
        )
        return BULK_WAIT_VIDEO

    bulk_videos[title].append(video_data)
    context.user_data['bulk_videos'] = bulk_videos

    total_titles = len(bulk_videos)
    total_files = sum(len(v) for v in bulk_videos.values())
    summary = ""
    for t, vids in bulk_videos.items():
        q_list = ", ".join([f"{v['quality_label']}({format_file_size(v['file_size'])})" for v in vids])
        summary += f"  📹 {html_escape(generate_display_title(t))}: {q_list}\n"

    await msg.reply_text(
        f"✅ <b>Added!</b> {html_escape(generate_display_title(title))}: "
        f"{quality_label} ({format_file_size(file_size)})\n\n"
        f"📋 <b>Summary ({total_titles} videos, {total_files} files):</b>\n{summary}\n"
        f"📹 Aur bhejo ya /done\n❌ /cancel",
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

            # ===== COPY TO BACKUP_1 AND GET file_url =====
            file_url = None
            try:
                copied = await context.bot.copy_message(
                    chat_id=BACKUP_1, from_chat_id=vdata['chat_id'],
                    message_id=vdata['msg_id'], caption=backup_caption, parse_mode='HTML'
                )
                file_url = construct_file_url(BACKUP_1, copied.message_id)
                logger.info(f"✅ Bulk backup: {title} {q_label} → {file_url}")
            except Exception as e:
                logger.error(f"❌ Backup1 FAILED {title} {q_label}: {e}")
                continue

            # Copy to other channels (optional)
            for ch_id in [ch for ch in [BACKUP_2, PAID_CH] if ch != 0]:
                try:
                    await context.bot.copy_message(
                        chat_id=ch_id, from_chat_id=vdata['chat_id'],
                        message_id=vdata['msg_id'], caption=backup_caption, parse_mode='HTML'
                    )
                except Exception as e:
                    logger.error(f"Channel {ch_id} error: {e}")

            # ===== SAVE file_url TO DB =====
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

        # Generate link and post to free channel
        web_link = f"{WEB_DOMAIN}/watch/{vid_id}"
        gplink = await shorten_link(web_link)
        caption = build_free_channel_caption(title, gplink, qualities_info)

        if FREE_CH != 0:
            first_vid = video_list[0]
            try:
                await context.bot.copy_message(
                    chat_id=FREE_CH, from_chat_id=first_vid['chat_id'],
                    message_id=first_vid['msg_id'], caption=caption, parse_mode='HTML'
                )
            except:
                try:
                    await context.bot.send_message(
                        chat_id=FREE_CH, text=caption, parse_mode='HTML'
                    )
                except Exception as e2:
                    logger.error(f"Free channel failed: {e2}")

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
#            PROVIDER BOT (USER-FACING) - ALL HANDLERS
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

    logger.info(f"👤 Provider /start from user {user.id} ({user_name}): {text}")

    # ===== VIDEO LINK HANDLING =====
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
                await update.message.reply_text(
                    "❌ Video Not Found!\n\nYeh video delete ho chuki hai ya invalid link hai."
                )
                return
            if not qualities:
                await update.message.reply_text("❌ No video files found. Contact admin.")
                return

            # Single quality - send directly
            if len(qualities) == 1:
                await send_video_to_user(
                    update, context, chat_id, user_name, title, qualities[0]
                )
                return

            # Multiple qualities - show selection
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

            await update.message.reply_text(
                f"👋 Hello <b>{html_escape(user_name)}</b>!\n\n"
                f"🎬 <b>{html_escape(title)}</b>\n\n"
                f"📊 <b>Select Quality:</b>\n\n"
                f"⚠️ Videos auto-delete after 5 minutes!\n"
                f"💾 Forward to Saved Messages immediately!",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except ValueError:
            await update.message.reply_text("❌ Invalid video ID.")
        except Exception as e:
            logger.error(f"Provider Error: {e}")
            await update.message.reply_text("❌ Something went wrong. Try again.")
        return

    # ===== NORMAL /start - USER WELCOME MENU =====
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
        [InlineKeyboardButton(
            f"💎 Buy VIP ({SUBSCRIPTION_AMOUNT}₹/Month)",
            callback_data="buy_sub"
        )],
        [InlineKeyboardButton("🆓 Free Channel", url="https://t.me/+wcYoTQhIz-ZmOTY1")],
        [InlineKeyboardButton("👨‍💻 Contact Admin", url="https://t.me/ownermahi")]
    ]

    await update.message.reply_text(
        f"🔞 <b>Welcome {html_escape(user_name)}!</b>\n\n"
        f"🎬 Premium Videos sirf {SUBSCRIPTION_AMOUNT}₹/month mein.\n\n"
        f"📌 <b>Features:</b>\n"
        f"  • Direct video files without ads\n"
        f"  • All qualities available\n"
        f"  • Priority support"
        f"{sub_status}\n\n"
        f"👇 Neeche buttons use karein:",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def provider_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('payment_step', None)
    context.user_data.pop('screenshot_id', None)
    context.user_data.pop('qr_expiry', None)
    context.user_data.pop('payment_note', None)
    await update.message.reply_text(
        "❌ <b>Cancelled!</b>\n\nDobara /start type karein.",
        parse_mode='HTML'
    )


async def provider_handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id
    user = query.from_user
    user_name = user.first_name

    logger.info(f"📲 Callback from {user.id}: {data}")

    # ========== BUY SUBSCRIPTION ==========
    if data == "buy_sub":
        is_active, end_date = check_active_subscription(user.id)
        if is_active and end_date:
            remaining = (end_date - datetime.now()).days
            await query.message.reply_text(
                f"✅ <b>Tumhari subscription already active hai!</b>\n\n"
                f"📅 Expires: {end_date.strftime('%d-%m-%Y')}\n"
                f"⏳ {remaining} days remaining",
                parse_mode='HTML'
            )
            return

        # ===== GENERATE DYNAMIC QR CODE =====
        amount = SUBSCRIPTION_AMOUNT
        qr_image, note = generate_upi_qr(user.id, user_name, amount)

        # QR validity time (10 minutes)
        qr_validity_minutes = 10
        expiry_time = datetime.now() + timedelta(minutes=qr_validity_minutes)
        context.user_data['payment_step'] = 'screenshot'
        context.user_data['qr_expiry'] = expiry_time
        context.user_data['payment_note'] = note

        await query.message.reply_photo(
            photo=qr_image,
            caption=(
                f"💎 <b>VIP Subscription - {amount}₹ / Month</b>\n\n"
                f"📱 <b>Scan QR Code</b> from any UPI app:\n"
                f"  • Google Pay\n"
                f"  • PhonePe\n"
                f"  • Paytm\n"
                f"  • Any UPI App\n\n"
                f"💰 Amount: <b>₹{amount}</b> (pre-filled)\n"
                f"📝 Note: <code>{note}</code> (auto-filled)\n\n"
                f"⚠️ <b>Important:</b>\n"
                f"  • Amount change MAT karna\n"
                f"  • Note/Remark change MAT karna\n"
                f"  • QR valid for <b>{qr_validity_minutes} minutes</b> only\n"
                f"  • Expiry: <b>{expiry_time.strftime('%H:%M:%S')}</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"✅ Payment ke baad:\n"
                f"1️⃣ Payment ka <b>Screenshot</b> bhejo\n"
                f"2️⃣ Phir <b>UTR/Reference Number</b> bhejo\n\n"
                f"📸 <b>Ab payment karo aur screenshot bhejo...</b>\n\n"
                f"❌ Cancel: /cancel"
            ),
            parse_mode='HTML'
        )
        return

    # ========== QUALITY SELECTION ==========
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

    # ========== ALL QUALITIES ==========
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

        sent_msg_ids = []
        for quality in qualities:
            msg_id = await send_video_to_user(
                update, context, chat_id, user_name, title, quality,
                is_callback=True, return_msg_id=True
            )
            if msg_id:
                sent_msg_ids.append(msg_id)
            await asyncio.sleep(1)

        if sent_msg_ids:
            asyncio.create_task(
                auto_delete_with_notification(
                    context=context, chat_id=chat_id,
                    message_ids_to_delete=sent_msg_ids, delete_time=AUTO_DELETE_TIME
                )
            )
        return

    # ========== ADMIN: APPROVE PAYMENT ==========
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
            logger.info(f"✅ Subscriber {target_user_id} approved for {days} days")
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
                        f"Admin se link lein: @ownermahi"
                    ),
                    parse_mode='HTML'
                )
        except Exception as e:
            logger.error(f"Invite link error: {e}")
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_USER_ID,
                    text=f"⚠️ User <code>{target_user_id}</code> ko link bhejne mein error: {e}",
                    parse_mode='HTML'
                )
            except:
                pass
        return

    # ========== ADMIN: REJECT PAYMENT ==========
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
                    "🔁 Dobara try: /start\n"
                    "❓ Help: @ownermahi"
                ),
                parse_mode='HTML'
            )
        except:
            pass
        return


async def provider_handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    payment_step = context.user_data.get('payment_step')

    if payment_step == 'screenshot':
        # ===== CHECK QR EXPIRY =====
        qr_expiry = context.user_data.get('qr_expiry')
        if qr_expiry and datetime.now() > qr_expiry:
            context.user_data.pop('payment_step', None)
            context.user_data.pop('qr_expiry', None)
            context.user_data.pop('payment_note', None)
            context.user_data.pop('screenshot_id', None)
            await msg.reply_text(
                "❌ <b>QR Code Expired!</b>\n\n"
                "⏰ 10 minute ka time khatam ho gaya.\n"
                "🔁 Naya QR lene ke liye /start → Buy VIP dabao.\n\n"
                "⚠️ Agar payment ho gaya hai toh admin se contact karo: @ownermahi",
                parse_mode='HTML'
            )
            return

        context.user_data['screenshot_id'] = msg.photo[-1].file_id
        context.user_data['payment_step'] = 'utr'
        await msg.reply_text(
            "✅ <b>Screenshot Received!</b>\n\n"
            "🔢 Ab <b>UTR ya Reference Number</b> type karke bhejein.\n\n"
            "💡 UTR number payment SMS ya app mein milta hai.\n\n"
            "❌ Cancel: /cancel",
            parse_mode='HTML'
        )
        return

    await msg.reply_text(
        "📸 Photo received, lekin koi active process nahi hai.\n\n"
        "👉 /start type karein menu dekhne ke liye."
    )


async def provider_handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    payment_step = context.user_data.get('payment_step')

    if payment_step == 'utr':
        utr_number = msg.text.strip()
        if len(utr_number) < 4:
            await msg.reply_text(
                "❌ UTR number bahut chota hai. Sahi UTR bhejein.\n❌ Cancel: /cancel"
            )
            return

        screenshot_id = context.user_data.get('screenshot_id')
        payment_note = context.user_data.get('payment_note', 'N/A')
        user = update.effective_user
        username_text = f"@{user.username}" if user.username else "N/A"

        admin_keyboard = [
            [
                InlineKeyboardButton("✅ Approve (30 Days)", callback_data=f"approve_{user.id}_30"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user.id}")
            ],
            [
                InlineKeyboardButton("✅ Approve (7 Days Trial)", callback_data=f"approve_{user.id}_7")
            ]
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
                    f"💡 UPI mein <code>{payment_note}</code> search karo verify ke liye\n\n"
                    f"👇 Verify karke approve/reject karein:"
                ),
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(admin_keyboard)
            )
        except Exception as e:
            logger.error(f"Failed to send payment to admin: {e}")
            await msg.reply_text("❌ Error! Please try again or contact @ownermahi")
            context.user_data.pop('payment_step', None)
            context.user_data.pop('screenshot_id', None)
            context.user_data.pop('qr_expiry', None)
            context.user_data.pop('payment_note', None)
            return

        await msg.reply_text(
            "⏳ <b>Verification Pending!</b>\n\n"
            "✅ Payment details admin ko bhej di gayi.\n"
            "🕒 Admin verify karte hi VIP link mil jayega.\n\n"
            "⏱️ Usually 5-30 minutes lagta hai.\n\n"
            "❓ Problem? @ownermahi",
            parse_mode='HTML'
        )

        context.user_data.pop('payment_step', None)
        context.user_data.pop('screenshot_id', None)
        context.user_data.pop('qr_expiry', None)
        context.user_data.pop('payment_note', None)
        return

    if payment_step == 'screenshot':
        await msg.reply_text(
            "❌ Photo chahiye! Payment ka <b>Screenshot (photo)</b> bhejein.\n\n"
            "❌ Cancel: /cancel",
            parse_mode='HTML'
        )
        return

    await msg.reply_text(
        "🤔 Samajh nahi aaya.\n\n"
        "👉 /start type karein menu dekhne ke liye.\n"
        "👉 Video ke liye free channel ka link use karein."
    )


# ================================================================
#  SEND VIDEO TO USER - COPIES FROM BACKUP CHANNEL USING file_url
# ================================================================

async def send_video_to_user(update, context, chat_id, user_name, title,
                              quality_data, is_callback=False, return_msg_id=False):
    q_id, q_label, file_url, file_size = quality_data
    size_str = format_file_size(file_size)

    # ===== DETECT OLD file_id vs NEW file_url =====
    is_old_file_id = not file_url.startswith("https://t.me/c/") if file_url else False

    # Warning message
    try:
        warning_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"👋 Hello {user_name}!\n\n"
                f"📊 Quality: {q_label} ({size_str})\n\n"
                "⚠️ Video 5 min baad auto-delete hogi.\n"
                "💾 Saved Messages mein forward kar lo!\n\n"
                "⏳ Sending..."
            )
        )
        await asyncio.sleep(2)
        try:
            await warning_msg.delete()
        except:
            pass
    except Exception as e:
        logger.error(f"Warning msg error: {e}")

    caption_text = (
        f"🎬 {title}\n"
        f"📊 Quality: {q_label} ({size_str})\n\n"
        f"⏱️ Auto-Delete: 5 minutes\n"
        f"💾 Forward to Saved Messages ASAP!\n\n"
        f"⚠️ Yeh file automatically delete ho jayegi."
    )

    sent_msg_id = None

    if is_old_file_id:
        # ===== OLD METHOD: file_id se directly send =====
        logger.info(f"📦 Old file_id detected for {q_label}, using send_video")
        try:
            fallback = await context.bot.send_video(
                chat_id=chat_id, video=file_url,
                caption=caption_text, supports_streaming=True
            )
            sent_msg_id = fallback.message_id
            logger.info(f"✅ Sent {q_label} to {chat_id} via old file_id")
        except Exception as e:
            logger.error(f"❌ Old file_id send failed: {e}")
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Error sending {q_label}. Contact @ownermahi",
                    parse_mode='HTML'
                )
            except:
                pass
    else:
        # ===== NEW METHOD: backup channel URL se copy =====
        backup_channel_id, backup_msg_id = parse_file_url(file_url)

        if not backup_channel_id or not backup_msg_id:
            logger.error(f"❌ Invalid file_url: {file_url}")
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Invalid file URL for {q_label}. Contact @ownermahi"
                )
            except:
                pass
            return None

        try:
            copied = await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=backup_channel_id,
                message_id=backup_msg_id,
                caption=caption_text
            )
            sent_msg_id = copied.message_id
            logger.info(f"✅ Sent {q_label} to {chat_id} from backup URL")
        except Exception as e:
            logger.error(f"❌ Copy from backup failed: {e} | URL: {file_url}")
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Error sending {q_label}. File may be deleted. Contact @ownermahi"
                )
            except:
                pass

    if return_msg_id:
        return sent_msg_id

    if sent_msg_id:
        asyncio.create_task(
            auto_delete_with_notification(
                context=context, chat_id=chat_id,
                message_ids_to_delete=sent_msg_id, delete_time=AUTO_DELETE_TIME
            )
        )
    return sent_msg_id


# ================= BACKGROUND TASKS =================
async def periodic_cleanup(context):
    while True:
        await asyncio.sleep(3600)
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("DELETE FROM adult_videos WHERE created_at < NOW() - INTERVAL '7 days'")
            deleted = cur.rowcount
            conn.commit()
            cur.close()
            db_pool.putconn(conn)
            if deleted > 0:
                logger.info(f"🗑️ Cleaned {deleted} old records")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")


async def notify_expired_subs(provider_app_instance: Application):
    await asyncio.sleep(60)
    logger.info("✅ Subscription expiry checker started")
    while True:
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                SELECT user_id, end_date FROM subscribers
                WHERE end_date < NOW() + INTERVAL '2 days'
                AND end_date > NOW() - INTERVAL '7 days'
                AND notified = FALSE
            """)
            users = cur.fetchall()

            for (user_id, end_date) in users:
                is_expired = end_date < datetime.now()
                if is_expired:
                    msg_text = (
                        "⚠️ <b>Subscription Expired!</b>\n\n"
                        f"📅 Expired on: {end_date.strftime('%d-%m-%Y')}\n\n"
                        "🔁 Renew: /start → Buy Subscription\n"
                        "❓ Help: @ownermahi"
                    )
                else:
                    remaining = (end_date - datetime.now()).days
                    msg_text = (
                        "⚠️ <b>Subscription Expiry Alert!</b>\n\n"
                        f"📅 Expires in <b>{remaining} days</b> ({end_date.strftime('%d-%m-%Y')})\n\n"
                        "🔁 Renew now: /start → Buy Subscription\n"
                        "❓ Help: @ownermahi"
                    )
                try:
                    await provider_app_instance.bot.send_message(
                        chat_id=user_id, text=msg_text, parse_mode='HTML'
                    )
                except Exception as e:
                    logger.error(f"Notify user {user_id} error: {e}")
                try:
                    status = "EXPIRED" if is_expired else f"Expires in {remaining}d"
                    await provider_app_instance.bot.send_message(
                        chat_id=ADMIN_USER_ID,
                        text=f"🔔 Sub Alert: User <code>{user_id}</code> - {status}",
                        parse_mode='HTML'
                    )
                except:
                    pass
                cur.execute("UPDATE subscribers SET notified = TRUE WHERE user_id = %s", (user_id,))
                conn.commit()
                await asyncio.sleep(2)

            cur.close()
            db_pool.putconn(conn)
        except Exception as e:
            logger.error(f"Expiry check error: {e}")

        await asyncio.sleep(43200)


# ================================================================
#                    RUN BOTH BOTS
# ================================================================

async def run_bots():
    if not MAIN_BOT_TOKEN:
        logger.error("❌ MAIN_BOT_TOKEN not found!")
        return
    if not PROVIDER_BOT_TOKEN:
        logger.error("❌ PROVIDER_BOT_TOKEN not found!")
        return

    if BACKUP_1 == 0:
        logger.warning("⚠️ BACKUP_CHANNEL_1 not set! File URL mode won't work!")
        logger.warning("⚠️ Both bots must be admin of BACKUP_CHANNEL_1")

    # ============ MAIN BOT (ADMIN ONLY) ============
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
    logger.info("✅ Main Bot (Admin) handlers configured")

    # ============ PROVIDER BOT (USER-FACING) ============
    provider_app = Application.builder().token(PROVIDER_BOT_TOKEN).build()
    provider_app.add_handler(CommandHandler('start', provider_start))
    provider_app.add_handler(CommandHandler('cancel', provider_cancel))
    provider_app.add_handler(CallbackQueryHandler(provider_handle_callback))
    provider_app.add_handler(MessageHandler(filters.PHOTO, provider_handle_photo))
    provider_app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, provider_handle_text
    ))
    logger.info("✅ Provider Bot (User) handlers configured")

    # ============ START BOTH ============
    try:
        await main_app.initialize()
        await main_app.start()
        await main_app.updater.start_polling()
        logger.info("✅ Main Bot started!")

        await provider_app.initialize()
        await provider_app.start()
        await provider_app.updater.start_polling()
        logger.info("✅ Provider Bot started!")

        logger.info("=" * 50)
        logger.info("✅ BOTH BOTS RUNNING!")
        logger.info(f"📌 Admin Bot: ADMIN_USER_ID={ADMIN_USER_ID}")
        logger.info(f"📌 Provider Bot: For all users")
        logger.info(f"📌 Backup Channel: {BACKUP_1}")
        logger.info(f"📌 File URL Mode: Active")
        logger.info(f"📌 Duplicate Check: File Size based")
        logger.info(f"📌 UPI QR Code: Dynamic Generation Active")
        logger.info("=" * 50)

        asyncio.create_task(periodic_cleanup(None))
        asyncio.create_task(notify_expired_subs(provider_app))
        logger.info("✅ Background tasks started")

        while True:
            await asyncio.sleep(3600)

    except Exception as e:
        logger.error(f"❌ Startup error: {e}")
        raise
    finally:
        await main_app.updater.stop()
        await main_app.stop()
        await main_app.shutdown()
        await provider_app.updater.stop()
        await provider_app.stop()
        await provider_app.shutdown()


# ================================================================
if __name__ == '__main__':
    required = ['MAIN_BOT_TOKEN', 'PROVIDER_BOT_TOKEN', 'DATABASE_URL']
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        logger.error(f"❌ Missing env vars: {missing}")
        exit(1)

    if BACKUP_1 == 0:
        logger.error("❌ BACKUP_CHANNEL_1 is REQUIRED for file URL mode!")
        logger.error("❌ Set BACKUP_CHANNEL_1 env variable (e.g. -1002683355160)")
        logger.error("❌ Both bots must be admin of this channel!")
        exit(1)

    try:
        init_db_pool()
        setup_db()
    except Exception as e:
        logger.error(f"❌ DB init failed: {e}")
        exit(1)

    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    logger.info("✅ Flask started")

    try:
        asyncio.run(run_bots())
    except KeyboardInterrupt:
        logger.info("🛑 Shutting down...")
    except Exception as e:
        logger.error(f"❌ Fatal: {e}")
