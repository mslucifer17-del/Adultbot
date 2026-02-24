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

# ===== MAIN BOT CONVERSATION STATES (Admin Only) =====
WAIT_TRIM, WAIT_FULL = range(2)
BULK_WAIT_VIDEO, BULK_CONFIRM = range(2)

# ===== PROVIDER BOT PAYMENT STATES (via user_data, NOT ConversationHandler) =====
# user_data['payment_step'] = 'screenshot' | 'utr' | None

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
        f"𝐅𝐨𝐫 𝐒𝐮𝐛𝐬𝐜𝐫𝐢𝐩𝐭𝐢𝐨𝐧 𝗠𝗲𝘀𝘀𝗮𝗴𝗲 𝗠𝗲- @Niyativideobot\n"
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
    """Check if user has active subscription. Returns (is_active, end_date)"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT end_date FROM subscribers WHERE user_id = %s",
            (user_id,)
        )
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
    """Admin bot /start - only admin uses this bot"""
    context.user_data.clear()

    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text(
            "❌ <b>Access Denied!</b>\n\n"
            "Yeh Admin Bot hai. Sirf admin use kar sakta hai.\n\n"
            "👉 Videos ke liye @Niyativideobot use karein.",
            parse_mode='HTML'
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "🤖 <b>Admin Bot Ready!</b>\n\n"
        "📌 <b>Commands:</b>\n"
        "  /post - Single video post\n"
        "  /bulk - Bulk upload multiple videos\n"
        "  /cancel - Cancel current operation\n"
        "  /start - Reset everything\n\n"
        "⚡ <b>Multi-Quality Support Active</b>\n\n"
        "🎬 Shuru karne ke liye /post ya /bulk use karo!",
        parse_mode='HTML'
    )
    return ConversationHandler.END


async def start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Access Denied!")
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
        context.user_data['trim_file'] = 'use_thumbnail'
        context.user_data['trim_type'] = 'skip'
        await msg.reply_text(
            "⏭️ <b>Skipped!</b>\n\n"
            "🔞 Ab <b>FULL VIDEO(s)</b> bhejo.\n"
            "📊 Multiple qualities? Sab bhejo, phir <code>/done</code>\n\n"
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
        largest_photo = msg.photo[-1]
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
    file_id = video_obj.file_id if video_obj else doc_obj.file_id
    duration = 0
    quality_label, width, height, file_size = detect_quality_label(
        video_obj=video_obj, document_obj=doc_obj, caption=raw_caption
    )
    if video_obj:
        duration = video_obj.duration or 0
    full_videos = context.user_data.get('full_videos', [])
    for existing in full_videos:
        if existing['quality_label'] == quality_label:
            await msg.reply_text(
                f"⚠️ <b>{quality_label}</b> pehle se add hai! Aur quality bhejo ya /done.",
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
    trim_file_id = context.user_data.get('trim_file', 'use_thumbnail')
    total = len(full_videos)
    status = await msg.reply_text(f"⏳ Processing {total} quality(ies)...")

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
    for idx, vdata in enumerate(full_videos):
        q_label = vdata['quality_label']
        src_chat_id = vdata['chat_id']
        src_msg_id = vdata['msg_id']
        await status.edit_text(f"⏳ Uploading {idx + 1}/{total}: {q_label}...")
        backup_caption = build_backup_caption(title, q_label)
        backup_msg_id = None
        if BACKUP_1 != 0:
            try:
                copied_msg = await context.bot.copy_message(
                    chat_id=BACKUP_1, from_chat_id=src_chat_id,
                    message_id=src_msg_id, caption=backup_caption, parse_mode='HTML'
                )
                backup_msg_id = copied_msg.message_id
            except Exception as e:
                logger.error(f"Backup1 error {q_label}: {e}")
        for ch_id in [ch for ch in [BACKUP_2, PAID_CH] if ch != 0]:
            try:
                await context.bot.copy_message(
                    chat_id=ch_id, from_chat_id=src_chat_id,
                    message_id=src_msg_id, caption=backup_caption, parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Channel {ch_id} error: {e}")
        conn2 = None
        try:
            conn2 = get_db_connection()
            cur2 = conn2.cursor()
            cur2.execute(
                """INSERT INTO video_qualities
                   (vid_id, quality_label, file_id, file_size, width, height, duration,
                    backup_msg_id, backup_channel_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
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
        qualities_info.append({'label': q_label, 'size': format_file_size(vdata['file_size'])})

    await status.edit_text("⏳ Generating link...")
    web_link = f"{WEB_DOMAIN}/watch/{vid_id}"
    gplink = await shorten_link(web_link)

    await status.edit_text("⏳ Posting to Free Channel...")
    caption = build_free_channel_caption(title, gplink, qualities_info)
    try:
        if FREE_CH != 0:
            if trim_type == 'photo':
                await context.bot.send_photo(
                    chat_id=FREE_CH, photo=trim_file_id,
                    caption=caption, parse_mode='HTML'
                )
            elif trim_type in ['video', 'animation', 'document']:
                trim_chat_id = context.user_data.get('trim_chat_id')
                trim_msg_id = context.user_data.get('trim_msg_id')
                if trim_chat_id and trim_msg_id:
                    try:
                        await context.bot.copy_message(
                            chat_id=FREE_CH, from_chat_id=trim_chat_id,
                            message_id=trim_msg_id, caption=caption, parse_mode='HTML'
                        )
                    except:
                        await context.bot.send_video(
                            chat_id=FREE_CH, video=trim_file_id,
                            caption=caption, parse_mode='HTML', supports_streaming=True
                        )
                else:
                    await context.bot.send_video(
                        chat_id=FREE_CH, video=trim_file_id,
                        caption=caption, parse_mode='HTML', supports_streaming=True
                    )
            elif trim_type == 'skip':
                first_video = full_videos[0]
                try:
                    await context.bot.copy_message(
                        chat_id=FREE_CH, from_chat_id=first_video['chat_id'],
                        message_id=first_video['msg_id'], caption=caption, parse_mode='HTML'
                    )
                except:
                    await context.bot.send_message(
                        chat_id=FREE_CH, text=caption, parse_mode='HTML'
                    )
    except Exception as e:
        logger.error(f"Free channel error: {e}")

    quality_list = "\n".join([f"  • {q['label']} ({q['size']})" for q in qualities_info])
    display_title = generate_display_title(title)
    await status.edit_text(
        f"✅ <b>ALL DONE!</b>\n\n"
        f"🎬 Title: {html_escape(display_title)}\n"
        f"🆔 ID: {vid_id}\n"
        f"🔗 Link: {gplink}\n\n"
        f"📊 <b>Qualities:</b>\n{quality_list}",
        parse_mode='HTML'
    )
    context.user_data.clear()
    return ConversationHandler.END


# ===== BULK UPLOAD =====
async def start_bulk_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Access Denied!")
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data['bulk_videos'] = {}
    await update.message.reply_text(
        "📦 <b>BULK UPLOAD MODE!</b>\n\n"
        "🎬 Videos ek-ek karke forward karo.\n"
        "📊 Same title = same video group\n\n"
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
    file_id = video_obj.file_id if video_obj else doc_obj.file_id
    quality_label, width, height, file_size = detect_quality_label(
        video_obj=video_obj, document_obj=doc_obj, caption=raw_caption
    )
    duration = (video_obj.duration or 0) if video_obj else 0
    video_data = {
        'file_id': file_id, 'quality_label': quality_label,
        'width': width, 'height': height, 'file_size': file_size,
        'duration': duration, 'chat_id': msg.chat_id, 'msg_id': msg.message_id
    }
    bulk_videos = context.user_data.get('bulk_videos', {})
    if title not in bulk_videos:
        bulk_videos[title] = []
    existing = [v['quality_label'] for v in bulk_videos[title]]
    if quality_label in existing:
        await msg.reply_text(
            f"⚠️ {html_escape(title)}: {quality_label} already added!",
            parse_mode='HTML'
        )
        return BULK_WAIT_VIDEO
    bulk_videos[title].append(video_data)
    context.user_data['bulk_videos'] = bulk_videos
    total_titles = len(bulk_videos)
    total_files = sum(len(v) for v in bulk_videos.values())
    summary = ""
    for t, vids in bulk_videos.items():
        q_list = ", ".join([v['quality_label'] for v in vids])
        summary += f"  📹 {html_escape(generate_display_title(t))}: {q_list}\n"
    await msg.reply_text(
        f"✅ <b>Added!</b> {html_escape(generate_display_title(title))}: {quality_label}\n\n"
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
            backup_msg_id = None
            if BACKUP_1 != 0:
                try:
                    copied = await context.bot.copy_message(
                        chat_id=BACKUP_1, from_chat_id=vdata['chat_id'],
                        message_id=vdata['msg_id'], caption=backup_caption, parse_mode='HTML'
                    )
                    backup_msg_id = copied.message_id
                except Exception as e:
                    logger.error(f"Backup1 error {title} {q_label}: {e}")
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
                       (vid_id, quality_label, file_id, file_size, width, height,
                        duration, backup_msg_id, backup_channel_id)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
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
            qualities_info.append({'label': q_label, 'size': format_file_size(vdata['file_size'])})
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
                    await context.bot.send_video(
                        chat_id=FREE_CH, video=first_vid['file_id'],
                        caption=caption, parse_mode='HTML', supports_streaming=True
                    )
                except Exception as e2:
                    logger.error(f"Fallback failed: {e2}")
        q_str = ", ".join([q['label'] for q in qualities_info])
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
    """
    Provider Bot /start handler.
    - If vid_ID: show video/quality selection
    - If normal /start: show USER welcome menu with buttons
    """
    # ALWAYS clear payment state on /start
    context.user_data.pop('payment_step', None)
    context.user_data.pop('screenshot_id', None)

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
                        """SELECT quality_id, quality_label, file_id, file_size,
                                  backup_msg_id, backup_channel_id
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
                q_id, q_label, file_id, file_size, backup_msg_id, backup_ch = q
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
        [InlineKeyboardButton("🆓 Free Channel", url=FREE_CHANNEL_LINK)],
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
    """Cancel payment flow"""
    context.user_data.pop('payment_step', None)
    context.user_data.pop('screenshot_id', None)
    await update.message.reply_text(
        "❌ <b>Cancelled!</b>\n\nDobara /start type karein.",
        parse_mode='HTML'
    )


async def provider_handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    SINGLE callback handler for ALL provider bot buttons.
    Routes based on callback_data prefix.
    """
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id
    user = query.from_user
    user_name = user.first_name

    logger.info(f"📲 Callback from {user.id}: {data}")

    # ========== BUY SUBSCRIPTION ==========
    if data == "buy_sub":
        # Check existing subscription
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

        # Set payment state
        context.user_data['payment_step'] = 'screenshot'

        await query.message.reply_text(
            f"💎 <b>VIP Subscription - {SUBSCRIPTION_AMOUNT}₹ / Month</b>\n\n"
            f"💳 <b>UPI ID:</b> <code>{UPI_ID}</code>\n\n"
            f"⚠️ <b>Steps:</b>\n"
            f"1️⃣ Upar diye gaye UPI par {SUBSCRIPTION_AMOUNT}₹ pay karein\n"
            f"2️⃣ Payment ka <b>Screenshot</b> yahan bhejein\n"
            f"3️⃣ Phir <b>UTR/Reference Number</b> bhejein\n"
            f"4️⃣ Admin verify karke VIP link bhejega!\n\n"
            f"📸 <b>Ab payment screenshot (photo) bhejo...</b>\n\n"
            f"❌ Cancel: /cancel",
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
                """SELECT quality_id, quality_label, file_id, file_size,
                          backup_msg_id, backup_channel_id
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
        # Send invite link to user
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
    """Handle photos sent to provider bot (payment screenshots)"""
    msg = update.message
    payment_step = context.user_data.get('payment_step')

    if payment_step == 'screenshot':
        # User is sending payment screenshot
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

    # Normal photo (not in payment flow) - ignore or reply
    await msg.reply_text(
        "📸 Photo received, lekin koi active process nahi hai.\n\n"
        "👉 /start type karein menu dekhne ke liye."
    )


async def provider_handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages sent to provider bot (UTR number)"""
    msg = update.message
    payment_step = context.user_data.get('payment_step')

    if payment_step == 'utr':
        # User is sending UTR number
        utr_number = msg.text.strip()

        if len(utr_number) < 4:
            await msg.reply_text(
                "❌ UTR number bahut chota hai. Sahi UTR bhejein.\n❌ Cancel: /cancel"
            )
            return

        screenshot_id = context.user_data.get('screenshot_id')
        user = update.effective_user
        username_text = f"@{user.username}" if user.username else "N/A"

        # Send to admin for approval
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
                    f"💰 Amount: {SUBSCRIPTION_AMOUNT}₹\n"
                    f"📅 Date: {datetime.now().strftime('%d-%m-%Y %H:%M')}\n\n"
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
            return

        await msg.reply_text(
            "⏳ <b>Verification Pending!</b>\n\n"
            "✅ Payment details admin ko bhej di gayi.\n"
            "🕒 Admin verify karte hi VIP link mil jayega.\n\n"
            "⏱️ Usually 5-30 minutes lagta hai.\n\n"
            "❓ Problem? @ownermahi",
            parse_mode='HTML'
        )

        # Clear payment state
        context.user_data.pop('payment_step', None)
        context.user_data.pop('screenshot_id', None)
        return

    if payment_step == 'screenshot':
        # User sent text instead of photo
        await msg.reply_text(
            "❌ Photo chahiye! Payment ka <b>Screenshot (photo)</b> bhejein.\n\n"
            "❌ Cancel: /cancel",
            parse_mode='HTML'
        )
        return

    # Normal text (not in payment flow)
    await msg.reply_text(
        "🤔 Samajh nahi aaya.\n\n"
        "👉 /start type karein menu dekhne ke liye.\n"
        "👉 Video ke liye free channel ka link use karein."
    )


async def send_video_to_user(update, context, chat_id, user_name, title,
                              quality_data, is_callback=False, return_msg_id=False):
    q_id, q_label, file_id, file_size, backup_msg_id, backup_ch = quality_data
    size_str = format_file_size(file_size)

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

    if backup_msg_id and backup_ch and backup_ch != 0:
        try:
            copied = await context.bot.copy_message(
                chat_id=chat_id, from_chat_id=backup_ch,
                message_id=backup_msg_id, caption=caption_text
            )
            sent_msg_id = copied.message_id
            logger.info(f"✅ Sent {q_label} to {chat_id} via copy")
        except Exception as e:
            logger.error(f"copy failed {q_label}: {e}")

    if not sent_msg_id:
        try:
            fallback = await context.bot.send_video(
                chat_id=chat_id, video=file_id,
                caption=caption_text, supports_streaming=True
            )
            sent_msg_id = fallback.message_id
            logger.info(f"✅ Sent {q_label} to {chat_id} via file_id")
        except Exception as e:
            logger.error(f"Fallback failed {q_label}: {e}")
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Error sending {q_label} video. Try again."
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
    """Check every 12 hours for expiring subscriptions and notify users."""
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

        await asyncio.sleep(43200)  # 12 hours


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
    # NO ConversationHandler - clean simple handlers
    provider_app = Application.builder().token(PROVIDER_BOT_TOKEN).build()

    # 1. /start - always works, shows menu or video
    provider_app.add_handler(CommandHandler('start', provider_start))

    # 2. /cancel - cancels payment flow
    provider_app.add_handler(CommandHandler('cancel', provider_cancel))

    # 3. All callback buttons (quality, payment, admin actions)
    provider_app.add_handler(CallbackQueryHandler(provider_handle_callback))

    # 4. Photo messages (payment screenshots)
    provider_app.add_handler(MessageHandler(filters.PHOTO, provider_handle_photo))

    # 5. Text messages (UTR numbers)
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
        logger.info(f"📌 Admin Bot: Only for ADMIN_USER_ID={ADMIN_USER_ID}")
        logger.info(f"📌 Provider Bot: For all users")
        logger.info(f"📌 UPI: {UPI_ID}")
        logger.info(f"📌 Amount: {SUBSCRIPTION_AMOUNT}₹")
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
