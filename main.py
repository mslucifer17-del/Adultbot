import os
import re
import json
import asyncio
import logging
import aiohttp
import psycopg2
from html import escape as html_escape
from psycopg2 import pool
from flask import Flask, redirect
from threading import Thread
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
WEB_DOMAIN = os.environ.get("WEB_DOMAIN", "https://my-bot.onrender.com")
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))

FREE_CH = int(os.environ.get("FREE_CHANNEL_ID", "0"))
PAID_CH = int(os.environ.get("PAID_CHANNEL_ID", "0"))
BACKUP_1 = int(os.environ.get("BACKUP_CHANNEL_1", "0"))
BACKUP_2 = int(os.environ.get("BACKUP_CHANNEL_2", "0"))

AUTO_DELETE_TIME = int(os.environ.get("AUTO_DELETE_TIME", "300"))

# Conversation states
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

        # Main videos table - now stores multiple qualities
        cur.execute("""
            CREATE TABLE IF NOT EXISTS adult_videos (
                vid_id SERIAL PRIMARY KEY,
                title TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Quality table - each video can have multiple qualities
        cur.execute("""
            CREATE TABLE IF NOT EXISTS video_qualities (
                quality_id SERIAL PRIMARY KEY,
                vid_id INTEGER REFERENCES adult_videos(vid_id) ON DELETE CASCADE,
                quality_label TEXT,
                file_id TEXT,
                file_size BIGINT DEFAULT 0,
                width INTEGER DEFAULT 0,
                height INTEGER DEFAULT 0,
                duration INTEGER DEFAULT 0,
                backup_msg_id INTEGER,
                backup_channel_id BIGINT DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()
        cur.close()
        logger.info("✅ Database tables created/verified with multi-quality support")
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
    """
    Detect quality from video metadata or caption.
    Returns quality label like '480p', '720p', '1080p', '4K', etc.
    """
    width = 0
    height = 0
    file_size = 0

    if video_obj:
        width = video_obj.width or 0
        height = video_obj.height or 0
        file_size = video_obj.file_size or 0
    elif document_obj:
        file_size = document_obj.file_size or 0

    # Check caption for quality hints
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

    # Detect from resolution
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

    # Detect from file size if no resolution
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
        f"💎 <b>Join VIP for Direct Files:</b> https://t.me/YOUR_VIP_LINK"
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
    """
    Auto delete multiple messages after specified time.
    message_ids_to_delete can be a single int or list of ints.
    """
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


# ================= WEB REDIRECTOR (FLASK) =================
app = Flask(__name__)


@app.route('/')
def home():
    return "✅ Server is Running!"


@app.route('/watch/<int:vid_id>')
def watch_video(vid_id):
    bot_username = os.environ.get("PROVIDER_BOT_USERNAME", "your_bot")
    return redirect(f"https://t.me/{bot_username}?start=vid_{vid_id}")


def run_flask():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)


# ================= MAIN BOT: ADMIN START/RESET =================
async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset everything on /start"""
    context.user_data.clear()
    await update.message.reply_text(
        "🤖 <b>Admin Bot Ready!</b>\n\n"
        "📌 <b>Commands:</b>\n"
        "  /post - Single video post (with optional trim/preview)\n"
        "  /bulk - Bulk upload multiple videos\n"
        "  /cancel - Cancel current operation\n"
        "  /start - Reset everything\n\n"
        "⚡ <b>Multi-Quality Support:</b>\n"
        "  • Agar ek video ke multiple qualities hain\n"
        "  • Toh sab ek-ek karke bhejo\n"
        "  • Bot automatically detect karega quality\n"
        "  • Users ko quality choose karne ka option milega!\n\n"
        "🎬 Shuru karne ke liye /post ya /bulk use karo!",
        parse_mode='HTML'
    )
    return ConversationHandler.END


# ================= SINGLE UPLOAD FLOW (MULTI-QUALITY) =================
async def start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Access Denied! Only admin can use this command.")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data['mode'] = 'single'
    context.user_data['full_videos'] = []  # Store multiple quality videos

    await update.message.reply_text(
        "⚡ <b>Single Post Mode (Multi-Quality Support)!</b>\n\n"
        "✂️ Sabse pehle <b>TRIM/PREVIEW</b> bhejo:\n"
        "  • 📹 Choti trimmed video\n"
        "  • 🖼️ Ya koi image/photo\n"
        "  • ⏭️ Ya <code>/skip</code> agar kuch nahi hai\n\n"
        "❌ Cancel karne ke liye /cancel",
        parse_mode='HTML'
    )
    return WAIT_TRIM


async def get_trim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return WAIT_TRIM

    # Handle /skip
    if msg.text and msg.text.strip().lower() == '/skip':
        context.user_data['trim_file'] = 'use_thumbnail'
        context.user_data['trim_type'] = 'skip'
        await msg.reply_text(
            "⏭️ <b>Trim/Preview Skipped!</b>\n\n"
            "🔞 Ab <b>FULL VIDEO(s)</b> bhejo.\n\n"
            "📊 <b>Multiple Qualities?</b>\n"
            "  • Ek-ek karke saari qualities bhejo\n"
            "  • Jab sab ho jaye toh <code>/done</code> likho\n\n"
            "❌ Cancel: /cancel",
            parse_mode='HTML'
        )
        return WAIT_FULL

    # Handle /cancel
    if msg.text and msg.text.strip().lower() == '/cancel':
        context.user_data.clear()
        await msg.reply_text("❌ Upload cancelled.")
        return ConversationHandler.END

    # Handle /start (reset)
    if msg.text and msg.text.strip().lower() == '/start':
        context.user_data.clear()
        await msg.reply_text("🔄 Reset! Use /post to start again.")
        return ConversationHandler.END

    # Handle Photo
    if msg.photo:
        largest_photo = msg.photo[-1]  # Best quality
        context.user_data['trim_file'] = largest_photo.file_id
        context.user_data['trim_type'] = 'photo'

        raw_caption = msg.caption if msg.caption else ""
        cleaned_title = clean_title(raw_caption)
        if cleaned_title != "Exclusive Premium Content":
            context.user_data['title'] = cleaned_title

        context.user_data['trim_chat_id'] = msg.chat_id
        context.user_data['trim_msg_id'] = msg.message_id

        await msg.reply_text(
            f"✅ <b>Preview Image Saved!</b>\n\n"
            f"📝 Title: {html_escape(cleaned_title)}\n\n"
            "🔞 Ab <b>FULL VIDEO(s)</b> bhejo.\n"
            "📊 Multiple qualities? Sab bhejo, phir <code>/done</code>\n\n"
            "❌ Cancel: /cancel",
            parse_mode='HTML'
        )
        return WAIT_FULL

    # Handle Video/Animation/Document
    if msg.video or msg.document or msg.animation:
        raw_caption = msg.caption if msg.caption else ""
        cleaned_title = clean_title(raw_caption)
        context.user_data['title'] = cleaned_title

        if msg.video:
            context.user_data['trim_file'] = msg.video.file_id
            context.user_data['trim_type'] = 'video'
        elif msg.animation:
            context.user_data['trim_file'] = msg.animation.file_id
            context.user_data['trim_type'] = 'animation'
        else:
            context.user_data['trim_file'] = msg.document.file_id
            context.user_data['trim_type'] = 'document'

        context.user_data['trim_chat_id'] = msg.chat_id
        context.user_data['trim_msg_id'] = msg.message_id

        await msg.reply_text(
            f"✅ <b>Trim Video Saved!</b>\n\n"
            f"📝 Title: {html_escape(cleaned_title)}\n\n"
            "🔞 Ab <b>FULL VIDEO(s)</b> bhejo.\n"
            "📊 Multiple qualities? Sab bhejo, phir <code>/done</code>\n\n"
            "❌ Cancel: /cancel",
            parse_mode='HTML'
        )
        return WAIT_FULL

    # Invalid input
    await msg.reply_text(
        "❌ Invalid! Please send:\n"
        "  • 📹 Trim Video\n"
        "  • 🖼️ Photo/Image\n"
        "  • ⏭️ /skip\n"
        "  • ❌ /cancel"
    )
    return WAIT_TRIM


async def get_full_and_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return WAIT_FULL

    # Handle /done - process all collected videos
    if msg.text and msg.text.strip().lower() == '/done':
        full_videos = context.user_data.get('full_videos', [])
        if not full_videos:
            await msg.reply_text(
                "❌ Koi video nahi mili!\n"
                "Pehle video(s) bhejo, phir /done likho."
            )
            return WAIT_FULL
        return await finalize_single_post(update, context)

    # Handle /cancel
    if msg.text and msg.text.strip().lower() == '/cancel':
        context.user_data.clear()
        await msg.reply_text("❌ Upload cancelled.")
        return ConversationHandler.END

    # Handle /start (reset)
    if msg.text and msg.text.strip().lower() == '/start':
        context.user_data.clear()
        await msg.reply_text("🔄 Reset! Use /post to start again.")
        return ConversationHandler.END

    # Must be video or document
    if not msg.video and not msg.document:
        await msg.reply_text(
            "❌ Yeh video nahi hai!\n\n"
            "📹 Video file bhejo ya:\n"
            "  • /done - Sab videos bhej chuke ho toh\n"
            "  • /cancel - Cancel karna ho toh"
        )
        return WAIT_FULL

    # Extract video info
    raw_caption = msg.caption if msg.caption else ""

    # Set title from first video if not already set
    title = context.user_data.get('title', '')
    if not title or title == "Exclusive Premium Content":
        title = clean_title(raw_caption)
        context.user_data['title'] = title

    # Detect quality
    video_obj = msg.video
    doc_obj = msg.document
    file_id = video_obj.file_id if video_obj else doc_obj.file_id
    duration = 0

    quality_label, width, height, file_size = detect_quality_label(
        video_obj=video_obj,
        document_obj=doc_obj,
        caption=raw_caption
    )

    if video_obj:
        duration = video_obj.duration or 0

    # Check for duplicate quality
    full_videos = context.user_data.get('full_videos', [])
    for existing in full_videos:
        if existing['quality_label'] == quality_label:
            await msg.reply_text(
                f"⚠️ <b>{quality_label}</b> quality pehle se add ho chuki hai!\n"
                f"Koi aur quality bhejo ya /done likho.",
                parse_mode='HTML'
            )
            return WAIT_FULL

    video_data = {
        'file_id': file_id,
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
        f"📋 <b>All Qualities So Far:</b>\n{quality_list}\n\n"
        f"📹 Aur quality bhejo ya <code>/done</code> likho\n"
        f"❌ Cancel: /cancel",
        parse_mode='HTML'
    )
    return WAIT_FULL


async def finalize_single_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process all collected videos and post to channels."""
    msg = update.message
    full_videos = context.user_data.get('full_videos', [])
    title = context.user_data.get('title', 'Exclusive Premium Content')
    trim_type = context.user_data.get('trim_type', 'skip')
    trim_file_id = context.user_data.get('trim_file', 'use_thumbnail')

    total = len(full_videos)
    status = await msg.reply_text(f"⏳ Processing {total} quality(ies)...")

    # ======= STEP 1: Create video entry in DB =======
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
        logger.info(f"✅ Video entry created with ID: {vid_id}")
    except Exception as e:
        logger.error(f"❌ Database error: {e}")
        await status.edit_text(f"❌ Database error: {e}")
        context.user_data.clear()
        return ConversationHandler.END
    finally:
        if conn:
            db_pool.putconn(conn)

    # ======= STEP 2: Upload each quality to backup & save to DB =======
    qualities_info = []

    for idx, vdata in enumerate(full_videos):
        q_label = vdata['quality_label']
        file_id = vdata['file_id']
        src_chat_id = vdata['chat_id']
        src_msg_id = vdata['msg_id']
        file_size = vdata['file_size']
        width = vdata['width']
        height = vdata['height']
        duration = vdata['duration']

        await status.edit_text(
            f"⏳ Uploading quality {idx + 1}/{total}: {q_label}..."
        )

        backup_caption = build_backup_caption(title, q_label)
        backup_msg_id = None

        # Upload to BACKUP_1
        if BACKUP_1 != 0:
            try:
                copied_msg = await context.bot.copy_message(
                    chat_id=BACKUP_1,
                    from_chat_id=src_chat_id,
                    message_id=src_msg_id,
                    caption=backup_caption,
                    parse_mode='HTML'
                )
                backup_msg_id = copied_msg.message_id
                logger.info(f"✅ Quality {q_label}: Uploaded to Backup 1, Msg ID: {backup_msg_id}")
            except Exception as e:
                logger.error(f"❌ Quality {q_label}: Failed to copy to Backup 1: {e}")

        # Upload to BACKUP_2 & PAID_CH
        channels_to_upload = [ch for ch in [BACKUP_2, PAID_CH] if ch != 0]
        for ch_id in channels_to_upload:
            try:
                await context.bot.copy_message(
                    chat_id=ch_id,
                    from_chat_id=src_chat_id,
                    message_id=src_msg_id,
                    caption=backup_caption,
                    parse_mode='HTML'
                )
                logger.info(f"✅ Quality {q_label}: Uploaded to channel {ch_id}")
            except Exception as e:
                logger.error(f"❌ Quality {q_label}: Failed to upload to {ch_id}: {e}")

        # Save quality to DB
        conn2 = None
        try:
            conn2 = get_db_connection()
            cur2 = conn2.cursor()
            cur2.execute(
                """INSERT INTO video_qualities 
                   (vid_id, quality_label, file_id, file_size, width, height, duration, 
                    backup_msg_id, backup_channel_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (vid_id, q_label, file_id, file_size, width, height, duration,
                 backup_msg_id, BACKUP_1)
            )
            conn2.commit()
            cur2.close()
        except Exception as e:
            logger.error(f"❌ DB error saving quality {q_label}: {e}")
        finally:
            if conn2:
                db_pool.putconn(conn2)

        qualities_info.append({
            'label': q_label,
            'size': format_file_size(file_size)
        })

    # ======= STEP 3: Generate link =======
    await status.edit_text("⏳ Generating link...")
    web_link = f"{WEB_DOMAIN}/watch/{vid_id}"
    gplink = await shorten_link(web_link)

    # ======= STEP 4: Post to Free Channel =======
    await status.edit_text("⏳ Posting to Free Channel...")
    caption = build_free_channel_caption(title, gplink, qualities_info)

    try:
        if FREE_CH != 0:
            if trim_type == 'photo':
                # Send photo preview
                await context.bot.send_photo(
                    chat_id=FREE_CH,
                    photo=trim_file_id,
                    caption=caption,
                    parse_mode='HTML'
                )
            elif trim_type in ['video', 'animation', 'document']:
                # Send trim video
                trim_chat_id = context.user_data.get('trim_chat_id')
                trim_msg_id = context.user_data.get('trim_msg_id')
                if trim_chat_id and trim_msg_id:
                    try:
                        await context.bot.copy_message(
                            chat_id=FREE_CH,
                            from_chat_id=trim_chat_id,
                            message_id=trim_msg_id,
                            caption=caption,
                            parse_mode='HTML'
                        )
                    except Exception as e:
                        logger.error(f"copy_message for trim failed: {e}")
                        await context.bot.send_video(
                            chat_id=FREE_CH,
                            video=trim_file_id,
                            caption=caption,
                            parse_mode='HTML',
                            supports_streaming=True
                        )
                else:
                    await context.bot.send_video(
                        chat_id=FREE_CH,
                        video=trim_file_id,
                        caption=caption,
                        parse_mode='HTML',
                        supports_streaming=True
                    )
            elif trim_type == 'skip':
                # Try to extract thumbnail from first video
                first_video = full_videos[0]
                thumbnail_sent = False

                # Try to get thumbnail from original message
                try:
                    # Send first video's thumbnail as photo
                    src_chat = first_video['chat_id']
                    src_msg = first_video['msg_id']

                    # Get the original message to find thumbnail
                    # We'll use the first video itself as preview
                    await context.bot.copy_message(
                        chat_id=FREE_CH,
                        from_chat_id=src_chat,
                        message_id=src_msg,
                        caption=caption,
                        parse_mode='HTML'
                    )
                    thumbnail_sent = True
                except Exception as e:
                    logger.error(f"Failed to use video as preview: {e}")

                if not thumbnail_sent:
                    # Fallback: send text message
                    await context.bot.send_message(
                        chat_id=FREE_CH,
                        text=caption,
                        parse_mode='HTML'
                    )

            logger.info(f"✅ Posted to Free Channel")
    except Exception as e:
        logger.error(f"❌ Error posting to free channel: {e}")

    # ======= STEP 5: Final status =======
    quality_list = "\n".join([f"  • {q['label']} ({q['size']})" for q in qualities_info])
    display_title = generate_display_title(title)

    await status.edit_text(
        f"✅ <b>ALL DONE!</b>\n\n"
        f"🎬 Title: {html_escape(display_title)}\n"
        f"🆔 Video ID: {vid_id}\n"
        f"🔗 Link: {gplink}\n\n"
        f"📊 <b>Qualities Saved:</b>\n{quality_list}\n\n"
        f"🎉 Users ko quality choose karne ka option milega!",
        parse_mode='HTML'
    )

    context.user_data.clear()
    return ConversationHandler.END


# ================= BULK UPLOAD (MULTI-QUALITY) =================
async def start_bulk_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Access Denied!")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data['bulk_videos'] = {}  # vid_title -> [video_data list]
    context.user_data['bulk_count'] = 0
    context.user_data['bulk_current_title'] = None

    await update.message.reply_text(
        "📦 <b>BULK UPLOAD MODE!</b>\n\n"
        "🎬 Videos ek-ek karke forward karo.\n\n"
        "📊 <b>Multi-Quality Support:</b>\n"
        "  • Same title ki multiple qualities automatically group hongi\n"
        "  • Alag title = alag video entry\n\n"
        "📝 <b>Commands:</b>\n"
        "  • /done - Sab ho gaya, process karo\n"
        "  • /cancel - Cancel karo\n\n"
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
        await msg.reply_text("🔄 Reset! Use /bulk to start again.")
        return ConversationHandler.END

    if not msg.video and not msg.document:
        await msg.reply_text("❌ Video file bhejo. Ya /done / /cancel likho.")
        return BULK_WAIT_VIDEO

    raw_caption = msg.caption if msg.caption else ""
    title = clean_title(raw_caption)

    video_obj = msg.video
    doc_obj = msg.document
    file_id = video_obj.file_id if video_obj else doc_obj.file_id

    quality_label, width, height, file_size = detect_quality_label(
        video_obj=video_obj, document_obj=doc_obj, caption=raw_caption
    )
    duration = (video_obj.duration or 0) if video_obj else 0

    video_data = {
        'file_id': file_id,
        'quality_label': quality_label,
        'width': width,
        'height': height,
        'file_size': file_size,
        'duration': duration,
        'chat_id': msg.chat_id,
        'msg_id': msg.message_id
    }

    bulk_videos = context.user_data.get('bulk_videos', {})

    if title not in bulk_videos:
        bulk_videos[title] = []

    # Check duplicate quality for same title
    existing_qualities = [v['quality_label'] for v in bulk_videos[title]]
    if quality_label in existing_qualities:
        await msg.reply_text(
            f"⚠️ <b>{html_escape(title)}</b>\n"
            f"Quality <b>{quality_label}</b> already added!\n"
            f"Different quality bhejo ya agle video pe jao.",
            parse_mode='HTML'
        )
        return BULK_WAIT_VIDEO

    bulk_videos[title].append(video_data)
    context.user_data['bulk_videos'] = bulk_videos

    total_titles = len(bulk_videos)
    total_files = sum(len(v) for v in bulk_videos.values())

    # Show summary
    summary = ""
    for t, vids in bulk_videos.items():
        display_t = generate_display_title(t)
        q_list = ", ".join([v['quality_label'] for v in vids])
        summary += f"  📹 {html_escape(display_t)}: {q_list}\n"

    await msg.reply_text(
        f"✅ <b>Video Added!</b>\n\n"
        f"📝 Title: {html_escape(generate_display_title(title))}\n"
        f"📊 Quality: {quality_label} ({format_file_size(file_size)})\n\n"
        f"📋 <b>Summary ({total_titles} videos, {total_files} files):</b>\n"
        f"{summary}\n"
        f"📹 Aur bhejo ya /done likho\n"
        f"❌ Cancel: /cancel",
        parse_mode='HTML'
    )
    return BULK_WAIT_VIDEO


async def finalize_bulk_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    bulk_videos = context.user_data.get('bulk_videos', {})

    if not bulk_videos:
        await msg.reply_text("❌ Koi video nahi mili! Videos bhejo phir /done likho.")
        return BULK_WAIT_VIDEO

    total_titles = len(bulk_videos)
    total_files = sum(len(v) for v in bulk_videos.values())

    status = await msg.reply_text(
        f"⏳ <b>Processing {total_titles} videos ({total_files} files)...</b>",
        parse_mode='HTML'
    )

    processed = 0
    results = []

    for title, video_list in bulk_videos.items():
        processed += 1
        await status.edit_text(
            f"⏳ Processing {processed}/{total_titles}: {html_escape(generate_display_title(title))}...",
            parse_mode='HTML'
        )

        # Create video entry
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
        except Exception as e:
            logger.error(f"❌ DB error for '{title}': {e}")
            results.append(f"❌ {generate_display_title(title)}: DB Error")
            continue
        finally:
            if conn:
                db_pool.putconn(conn)

        # Upload each quality
        qualities_info = []
        for vdata in video_list:
            q_label = vdata['quality_label']
            backup_caption = build_backup_caption(title, q_label)
            backup_msg_id = None

            # Backup 1
            if BACKUP_1 != 0:
                try:
                    copied = await context.bot.copy_message(
                        chat_id=BACKUP_1,
                        from_chat_id=vdata['chat_id'],
                        message_id=vdata['msg_id'],
                        caption=backup_caption,
                        parse_mode='HTML'
                    )
                    backup_msg_id = copied.message_id
                except Exception as e:
                    logger.error(f"Backup1 error for {title} {q_label}: {e}")

            # Backup 2 & Paid
            for ch_id in [ch for ch in [BACKUP_2, PAID_CH] if ch != 0]:
                try:
                    await context.bot.copy_message(
                        chat_id=ch_id,
                        from_chat_id=vdata['chat_id'],
                        message_id=vdata['msg_id'],
                        caption=backup_caption,
                        parse_mode='HTML'
                    )
                except Exception as e:
                    logger.error(f"Channel {ch_id} error: {e}")

            # Save quality to DB
            conn2 = None
            try:
                conn2 = get_db_connection()
                cur2 = conn2.cursor()
                cur2.execute(
                    """INSERT INTO video_qualities 
                       (vid_id, quality_label, file_id, file_size, width, height, 
                        duration, backup_msg_id, backup_channel_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (vid_id, q_label, vdata['file_id'], vdata['file_size'],
                     vdata['width'], vdata['height'], vdata['duration'],
                     backup_msg_id, BACKUP_1)
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
                'size': format_file_size(vdata['file_size'])
            })

        # Generate link
        web_link = f"{WEB_DOMAIN}/watch/{vid_id}"
        gplink = await shorten_link(web_link)

        # Post to free channel
        caption = build_free_channel_caption(title, gplink, qualities_info)

        if FREE_CH != 0:
            first_vid = video_list[0]
            try:
                await context.bot.copy_message(
                    chat_id=FREE_CH,
                    from_chat_id=first_vid['chat_id'],
                    message_id=first_vid['msg_id'],
                    caption=caption,
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Free channel error: {e}")
                try:
                    await context.bot.send_video(
                        chat_id=FREE_CH,
                        video=first_vid['file_id'],
                        caption=caption,
                        parse_mode='HTML',
                        supports_streaming=True
                    )
                except Exception as e2:
                    logger.error(f"Fallback also failed: {e2}")

        q_str = ", ".join([q['label'] for q in qualities_info])
        results.append(f"✅ {generate_display_title(title)}: {q_str}")

        # Small delay to avoid rate limiting
        await asyncio.sleep(1)

    # Final summary
    result_text = "\n".join(results)
    await status.edit_text(
        f"🎉 <b>BULK UPLOAD COMPLETE!</b>\n\n"
        f"📊 Total: {total_titles} videos, {total_files} files\n\n"
        f"<b>Results:</b>\n{result_text}",
        parse_mode='HTML'
    )

    context.user_data.clear()
    return ConversationHandler.END


async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Upload process cancelled.")
    return ConversationHandler.END


# ================= PROVIDER BOT =================
async def provider_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id
    user_name = update.effective_user.first_name

    if text and "vid_" in text:
        try:
            vid_id = int(text.split("vid_")[1])

            # Fetch video info and qualities
            conn = None
            title = None
            qualities = []
            try:
                conn = get_db_connection()
                cur = conn.cursor()

                cur.execute(
                    "SELECT title FROM adult_videos WHERE vid_id = %s", (vid_id,)
                )
                video_result = cur.fetchone()

                if video_result:
                    title = video_result[0]
                    cur.execute(
                        """SELECT quality_id, quality_label, file_id, file_size, 
                                  backup_msg_id, backup_channel_id
                           FROM video_qualities 
                           WHERE vid_id = %s 
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
                    "❌ Video Not Found!\n\n"
                    "Yeh video delete ho chuki hai ya invalid link hai.\n"
                    "Naya link free channel se lein."
                )
                return

            if not qualities:
                await update.message.reply_text(
                    "❌ No video files found for this entry.\n"
                    "Please contact admin."
                )
                return

            # If only 1 quality, send directly
            if len(qualities) == 1:
                await send_video_to_user(
                    update, context, chat_id, user_name, title,
                    qualities[0]
                )
                return

            # Multiple qualities - show selection buttons
            keyboard = []
            for q in qualities:
                q_id, q_label, file_id, file_size, backup_msg_id, backup_ch = q
                size_str = format_file_size(file_size)
                btn_text = f"📹 {q_label} ({size_str})"
                callback_data = f"quality_{vid_id}_{q_id}"
                keyboard.append(
                    [InlineKeyboardButton(btn_text, callback_data=callback_data)]
                )

            # Add "All Qualities" button
            keyboard.append(
                [InlineKeyboardButton(
                    "📦 Download All Qualities",
                    callback_data=f"allquality_{vid_id}"
                )]
            )

            safe_title = html_escape(title)
            await update.message.reply_text(
                f"👋 Hello <b>{html_escape(user_name)}</b>!\n\n"
                f"🎬 <b>{safe_title}</b>\n\n"
                f"📊 <b>Select Quality:</b>\n"
                f"Choose your preferred quality below.\n\n"
                f"⚠️ Videos auto-delete after 5 minutes!\n"
                f"💾 Forward to Saved Messages immediately!",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        except ValueError:
            await update.message.reply_text("❌ Invalid video ID.")
        except Exception as e:
            logger.error(f"Provider Bot Error: {e}")
            await update.message.reply_text("❌ Something went wrong. Please try again.")
    else:
        await update.message.reply_text(
            "🔞 <b>Welcome!</b>\n\n"
            "Please use a valid video link to access content.\n"
            "Click on a video link from our free channel.\n\n"
            "⚠️ All videos auto-delete after 5 minutes for security.",
            parse_mode='HTML'
        )


async def handle_quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    chat_id = query.message.chat_id
    user_name = query.from_user.first_name

    if data.startswith("quality_"):
        # Single quality selection
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
                """SELECT quality_id, quality_label, file_id, file_size, 
                          backup_msg_id, backup_channel_id
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

        # Delete the selection message
        try:
            await query.message.delete()
        except:
            pass

        await send_video_to_user(
            update, context, chat_id, user_name, title, quality,
            is_callback=True
        )

    elif data.startswith("allquality_"):
        # All qualities
        vid_id = int(data.split("_")[1])

        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()

            cur.execute("SELECT title FROM adult_videos WHERE vid_id = %s", (vid_id,))
            vid_result = cur.fetchone()
            title = vid_result[0] if vid_result else "Unknown"

            cur.execute(
                """SELECT quality_id, quality_label, file_id, file_size, 
                          backup_msg_id, backup_channel_id
                   FROM video_qualities WHERE vid_id = %s
                   ORDER BY file_size ASC""",
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

        # Delete selection message
        try:
            await query.message.delete()
        except:
            pass

        # Send all qualities
        sent_msg_ids = []
        for quality in qualities:
            msg_id = await send_video_to_user(
                update, context, chat_id, user_name, title, quality,
                is_callback=True, return_msg_id=True
            )
            if msg_id:
                sent_msg_ids.append(msg_id)
            await asyncio.sleep(1)  # Avoid rate limiting

        # Schedule auto-delete for all
        if sent_msg_ids:
            asyncio.create_task(
                auto_delete_with_notification(
                    context=context,
                    chat_id=chat_id,
                    message_ids_to_delete=sent_msg_ids,
                    delete_time=AUTO_DELETE_TIME
                )
            )


async def send_video_to_user(update, context, chat_id, user_name, title,
                              quality_data, is_callback=False, return_msg_id=False):
    """
    Send a single quality video to user.
    quality_data: (quality_id, quality_label, file_id, file_size, backup_msg_id, backup_channel_id)
    """
    q_id, q_label, file_id, file_size, backup_msg_id, backup_ch = quality_data
    size_str = format_file_size(file_size)

    # Warning message
    try:
        warning_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"👋 Hello {user_name}!\n\n"
                f"📊 Quality: {q_label} ({size_str})\n\n"
                "⚠️ IMPORTANT:\n"
                "🕒 Video 5 minutes baad auto-delete hogi.\n"
                "💾 Saved Messages mein forward kar lena!\n\n"
                "⏳ Video bhej rahe hain..."
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

    # PRIMARY: copy_message from backup channel
    if backup_msg_id and backup_ch and backup_ch != 0:
        try:
            copied = await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=backup_ch,
                message_id=backup_msg_id,
                caption=caption_text
            )
            sent_msg_id = copied.message_id
            logger.info(f"✅ Sent {q_label} to user {chat_id} via copy_message")
        except Exception as e:
            logger.error(f"copy_message failed for {q_label}: {e}")

    # FALLBACK: send via file_id
    if not sent_msg_id:
        try:
            fallback = await context.bot.send_video(
                chat_id=chat_id,
                video=file_id,
                caption=caption_text,
                supports_streaming=True
            )
            sent_msg_id = fallback.message_id
            logger.info(f"✅ Sent {q_label} to user {chat_id} via file_id")
        except Exception as e:
            logger.error(f"Fallback send failed for {q_label}: {e}")
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Error sending {q_label} quality video. Please try again."
                )
            except:
                pass

    if return_msg_id:
        return sent_msg_id

    # Schedule auto-delete for single video
    if sent_msg_id:
        asyncio.create_task(
            auto_delete_with_notification(
                context=context,
                chat_id=chat_id,
                message_ids_to_delete=sent_msg_id,
                delete_time=AUTO_DELETE_TIME
            )
        )

    return sent_msg_id


async def periodic_cleanup(context):
    while True:
        await asyncio.sleep(3600)
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            # Delete old videos and their qualities (CASCADE)
            cur.execute(
                "DELETE FROM adult_videos WHERE created_at < NOW() - INTERVAL '7 days'"
            )
            deleted_count = cur.rowcount
            conn.commit()
            cur.close()
            db_pool.putconn(conn)
            if deleted_count > 0:
                logger.info(f"🗑️ Cleaned up {deleted_count} old video records")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")


# ================= RUN MULTIPLE BOTS =================
async def run_bots():
    if not MAIN_BOT_TOKEN:
        logger.error("❌ MAIN_BOT_TOKEN not found!")
        return

    main_app = Application.builder().token(MAIN_BOT_TOKEN).build()

    # Single upload conversation
    upload_conv = ConversationHandler(
        entry_points=[CommandHandler('post', start_upload)],
        states={
            WAIT_TRIM: [
                CommandHandler('skip', get_trim),
                CommandHandler('cancel', cancel_flow),
                CommandHandler('start', admin_start),
                MessageHandler(filters.ALL & ~filters.COMMAND, get_trim),
            ],
            WAIT_FULL: [
                CommandHandler('done', get_full_and_process),
                CommandHandler('cancel', cancel_flow),
                CommandHandler('start', admin_start),
                MessageHandler(filters.ALL & ~filters.COMMAND, get_full_and_process),
            ]
        },
        fallbacks=[
            CommandHandler('cancel', cancel_flow),
            CommandHandler('start', admin_start),
        ],
        allow_reentry=True
    )
    main_app.add_handler(upload_conv)

    # Bulk upload conversation
    bulk_conv = ConversationHandler(
        entry_points=[CommandHandler('bulk', start_bulk_upload)],
        states={
            BULK_WAIT_VIDEO: [
                CommandHandler('done', process_bulk_video),
                CommandHandler('cancel', cancel_flow),
                CommandHandler('start', admin_start),
                MessageHandler(filters.ALL & ~filters.COMMAND, process_bulk_video),
            ]
        },
        fallbacks=[
            CommandHandler('cancel', cancel_flow),
            CommandHandler('start', admin_start),
        ],
        allow_reentry=True
    )
    main_app.add_handler(bulk_conv)

    # Start command (outside conversations)
    main_app.add_handler(CommandHandler('start', admin_start))

    logger.info("✅ Main Bot handlers configured")

    if not PROVIDER_BOT_TOKEN:
        logger.error("❌ PROVIDER_BOT_TOKEN not found!")
        return

    provider_app = Application.builder().token(PROVIDER_BOT_TOKEN).build()
    provider_app.add_handler(CommandHandler('start', provider_start))
    provider_app.add_handler(CallbackQueryHandler(handle_quality_callback))
    logger.info("✅ Provider Bot handlers configured")

    try:
        await main_app.initialize()
        await main_app.start()
        await main_app.updater.start_polling()
        logger.info("✅ Main Bot started!")

        await provider_app.initialize()
        await provider_app.start()
        await provider_app.updater.start_polling()
        logger.info("✅ Provider Bot started!")

        logger.info("✅ Both Telegram Bots Started Successfully!")
        logger.info(
            "⚠️ REMINDER: Provider Bot ko BACKUP_1 channel mein Admin banana zaroori hai!"
        )

        asyncio.create_task(periodic_cleanup(None))
        logger.info("✅ Periodic cleanup task started")

        while True:
            await asyncio.sleep(3600)

    except Exception as e:
        logger.error(f"❌ Bot startup error: {e}")
        raise
    finally:
        await main_app.updater.stop()
        await main_app.stop()
        await main_app.shutdown()
        await provider_app.updater.stop()
        await provider_app.stop()
        await provider_app.shutdown()


if __name__ == '__main__':
    required_vars = ['MAIN_BOT_TOKEN', 'PROVIDER_BOT_TOKEN', 'DATABASE_URL']
    missing_vars = [var for var in required_vars if not os.environ.get(var)]

    if missing_vars:
        logger.error(f"❌ Missing required environment variables: {missing_vars}")
        exit(1)

    try:
        init_db_pool()
        setup_db()
    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")
        exit(1)

    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    logger.info("✅ Flask server started in background thread")

    try:
        asyncio.run(run_bots())
    except KeyboardInterrupt:
        logger.info("🛑 Shutting down...")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
