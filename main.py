import os
import re
import json
import asyncio
import logging
import aiohttp
import psycopg2
from html import escape as html_escape
from psycopg2 import pool
from psycopg2.extras import execute_values
from flask import Flask, redirect
from threading import Thread
from io import BytesIO
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ConversationHandler
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

# Quality handling constants
QUALITY_ORDER = ["2160p", "1440p", "1080p", "720p", "480p", "360p", "240p", "HQ", "LQ"]

WAIT_TRIM, WAIT_FULL, WAIT_FINALIZE = range(3)
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
        
        # Main table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS adult_videos (
                vid_id SERIAL PRIMARY KEY,
                title TEXT,
                full_file_id TEXT,
                backup_msg_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Multi-quality table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS adult_video_files (
                file_entry_id SERIAL PRIMARY KEY,
                vid_id INTEGER REFERENCES adult_videos(vid_id) ON DELETE CASCADE,
                quality TEXT NOT NULL,
                file_id TEXT NOT NULL,
                backup_msg_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (vid_id, quality)
            )
        """)
        
        try:
            cur.execute("ALTER TABLE adult_videos ADD COLUMN backup_msg_id INTEGER;")
        except Exception:
            conn.rollback()
        
        try:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_adult_videos_title ON adult_videos(title);")
        except Exception:
            pass
            
        conn.commit()
        cur.close()
        logger.info("✅ Database tables created/verified")
    except Exception as e:
        logger.error(f"❌ Database setup failed: {e}")
    finally:
        if conn:
            db_pool.putconn(conn)

# ================= QUALITY FUNCTIONS =================

def normalize_quality_label(raw: str | None) -> str:
    if not raw:
        return "HQ"
    raw = raw.lower().strip()
    replacements = {"4k": "2160p", "2k": "1440p", "fullhd": "1080p"}
    return replacements.get(raw, raw.upper())

def quality_rank(label: str) -> int:
    norm = normalize_quality_label(label)
    try:
        return QUALITY_ORDER.index(norm)
    except ValueError:
        return len(QUALITY_ORDER)

def detect_quality_from_message(msg) -> str:
    candidates = []
    source_text = " ".join(filter(None, [
        msg.caption if msg.caption else "",
        msg.document.file_name if msg.document and msg.document.file_name else "",
    ])).lower()

    match = re.search(r"(2160p|1440p|1080p|720p|480p|360p|240p|4k|2k|fullhd|hq|lq)", source_text)
    if match:
        candidates.append(match.group(1))

    if msg.video:
        height = msg.video.height or 0
        inferred = (
            "2160p" if height >= 2000 else
            "1440p" if height >= 1300 else
            "1080p" if height >= 900 else
            "720p" if height >= 650 else
            "480p" if height >= 450 else
            "360p" if height >= 320 else
            "240p" if height >= 200 else
            "HQ"
        )
        candidates.append(inferred)

    return normalize_quality_label(candidates if candidates else "HQ")

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

def build_free_channel_caption(title, gplink):
    safe_title = html_escape(title)
    safe_gplink = html_escape(gplink)
    return (
        f"🔞 <b>{safe_title}</b>\n\n"
        f"🔥 <b>Watch Full Video &amp; Download:</b>\n"
        f"👉 {safe_gplink}\n\n"
        f"💎 <b>Join VIP for Direct Files:</b> https://t.me/+wcYoTQhIz-ZmOTY1"
    )

def build_backup_caption(title):
    safe_title = html_escape(title)
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

def get_db_connection():
    if db_pool is None:
        raise Exception("Database pool not initialized")
    return db_pool.getconn()

async def auto_delete_with_notification(context, chat_id, message_id_to_delete, delete_time=AUTO_DELETE_TIME):
    try:
        wait_time = max(delete_time - 30, 60)
        await asyncio.sleep(wait_time)
        
        try:
            warning_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "⚠️ IMPORTANT NOTICE\n\n"
                    "🕒 Yeh video 30 seconds mein auto-delete ho jayegi!\n\n"
                    "💾 Jaldi se Saved Messages mein forward kar lo!\n\n"
                    "🔒 Yeh copyright protection ke liye hai."
                )
            )
            await asyncio.sleep(5)
            await warning_msg.delete()
        except Exception as e:
            logger.error(f"Warning message error: {e}")
        
        await asyncio.sleep(30)
        
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id_to_delete)
            logger.info(f"✅ Video message deleted for chat: {chat_id}")
        except Exception as e:
            logger.error(f"Failed to delete video message: {e}")
            
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "🗑️ Video Auto-Deleted!\n\n"
                    "✅ Agar aapne forward kar liya hai toh saved messages mein check karein.\n"
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

# ================= UPDATED: OPTIONAL TRIM VIDEO/PHOTO =================

async def start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Access Denied! Only admin can use this command.")
        return ConversationHandler.END
    
    context.user_data.clear()
    context.user_data['quality_payloads'] = []
    
    await update.message.reply_text(
        "⚡ Single Post Mode - Multi-Quality!\n\n"
        "📸 STEP 1: OPTIONAL TRIM VIDEO/PHOTO\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "✅ Agar aapke paas PREVIEW (trimmed video ya thumbnail photo) hai toh:\n"
        "   👉 Video/Photo bhejo\n\n"
        "❌ Agar nahi hai toh:\n"
        "   👉 /skip likho\n\n"
        "🎯 Fir main aapko FULL VIDEOS ka quality variant dene bolungi."
    )
    return WAIT_TRIM

# ================= TRIM HANDLER (UPDATED) =================
async def get_trim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return WAIT_TRIM
    
    # Check for /skip command first
    if msg.text and msg.text.strip().lower() == '/skip':
        context.user_data['trim_file'] = None
        context.user_data['trim_type'] = None
        logger.info("✅ Trim skipped")
        await msg.reply_text(
            "⏭️ Trim Skipped!\n\n"
            "✅ Koi dikkat nahi.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📹 STEP 2: FULL QUALITY VIDEOS\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Ab sab quality variants bhejo!\n\n"
            "✅ /done likho"
        )
        return WAIT_FULL
    
    # Media handling
    media_found = False
    media_type = None
    file_id = None
    
    if msg.video:
        media_found = True
        media_type = 'video'
        file_id = msg.video.file_id
        logger.info(f"📹 Video detected (forwarded: {bool(msg.forward_origin)})")
    elif msg.photo:
        media_found = True
        media_type = 'photo'
        file_id = msg.photo[-1].file_id
        logger.info("📸 Photo detected")
    elif msg.document:
        media_found = True
        media_type = 'document'
        file_id = msg.document.file_id
        logger.info(f"📄 Document detected: {msg.document.file_name}")
    elif msg.animation:
        media_found = True
        media_type = 'animation'
        file_id = msg.animation.file_id
        logger.info("🎬 Animation detected")
    
    if not media_found:
        logger.warning(f"❌ Unsupported type from user")
        await msg.reply_text(
            "❌ ERROR: Ye video/photo nahi hai!\n\n"
            "📸 ACCEPTED:\n"
            "✅ Video (forwarded bhi ho sakte ho)\n"
            "✅ Photo\n"
            "✅ Document\n"
            "✅ GIF/Animation\n\n"
            "👉 /skip karke aage badho"
        )
        return WAIT_TRIM
    
    raw_caption = msg.caption if msg.caption else ""
    cleaned_title = clean_title(raw_caption)
    
    context.user_data['title'] = cleaned_title
    context.user_data['trim_file'] = file_id
    context.user_data['trim_type'] = media_type
    context.user_data['trim_chat_id'] = msg.chat_id
    context.user_data['trim_msg_id'] = msg.message_id
    
    logger.info(f"✅ Trim saved: {media_type} - Title: {cleaned_title}")
    
    await msg.reply_text(
        f"✅ Trim {media_type.upper()} Accepted!\n\n"
        f"📝 Title: {cleaned_title}\n"
        f"📁 Type: {media_type.upper()}\n"
        f"{'🔄 (Forwarded)' if msg.forward_origin else '📤 (Original)'}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📹 STEP 2: FULL QUALITY VIDEOS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Ab sab quality variants bhejo:\n"
        "📊 480p, 720p, 1080p, 2160p etc.\n\n"
        "Har quality separately:\n"
        "1️⃣ 480p video\n"
        "2️⃣ 720p video\n"
        "3️⃣ 1080p video\n\n"
        "✅ /done likho"
    )
    return WAIT_FULL
    
    # Check if it's a video
    if msg.video:
        raw_caption = msg.caption if msg.caption else ""
        cleaned_title = clean_title(raw_caption)
        
        context.user_data['title'] = cleaned_title
        context.user_data['trim_file'] = msg.video.file_id
        context.user_data['trim_type'] = 'video'
        context.user_data['trim_chat_id'] = msg.chat_id
        context.user_data['trim_msg_id'] = msg.message_id
        
        await msg.reply_text(
            f"✅ Trim Video Accepted!\n\n"
            f"📝 Title: {cleaned_title}\n"
            f"⏱️ Type: Video\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📹 STEP 2: FULL QUALITY VIDEOS\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Ab sab quality variants bhejo:\n"
            "📊 480p, 720p, 1080p, 2160p etc.\n\n"
            "Har quality separately:\n"
            "1️⃣ Video 1 (480p)\n"
            "2️⃣ Video 2 (720p)\n"
            "3️⃣ Video 3 (1080p)\n\n"
            "✅ Sab ho gaye toh /done likho"
        )
        return WAIT_FULL
    
    # Check if it's a photo
    elif msg.photo:
        raw_caption = msg.caption if msg.caption else ""
        cleaned_title = clean_title(raw_caption)
        
        context.user_data['title'] = cleaned_title
        context.user_data['trim_file'] = msg.photo[-1].file_id
        context.user_data['trim_type'] = 'photo'
        context.user_data['trim_chat_id'] = msg.chat_id
        context.user_data['trim_msg_id'] = msg.message_id
        
        await msg.reply_text(
            f"✅ Trim Photo Accepted!\n\n"
            f"📝 Title: {cleaned_title}\n"
            f"🖼️ Type: Photo/Thumbnail\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📹 STEP 2: FULL QUALITY VIDEOS\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Ab sab quality variants bhejo:\n"
            "📊 480p, 720p, 1080p, 2160p etc.\n\n"
            "Har quality separately:\n"
            "1️⃣ Video 1 (480p)\n"
            "2️⃣ Video 2 (720p)\n"
            "3️⃣ Video 3 (1080p)\n\n"
            "✅ Sab ho gaye toh /done likho"
        )
        return WAIT_FULL
    
    # Check if it's a document
    elif msg.document:
        raw_caption = msg.caption if msg.caption else ""
        cleaned_title = clean_title(raw_caption)
        
        context.user_data['title'] = cleaned_title
        context.user_data['trim_file'] = msg.document.file_id
        context.user_data['trim_type'] = 'document'
        context.user_data['trim_chat_id'] = msg.chat_id
        context.user_data['trim_msg_id'] = msg.message_id
        
        await msg.reply_text(
            f"✅ Trim Document Accepted!\n\n"
            f"📝 Title: {cleaned_title}\n"
            f"📄 Type: Document\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📹 STEP 2: FULL QUALITY VIDEOS\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Ab sab quality variants bhejo:\n"
            "📊 480p, 720p, 1080p, 2160p etc.\n\n"
            "Har quality separately:\n"
            "1️⃣ Video 1 (480p)\n"
            "2️⃣ Video 2 (720p)\n"
            "3️⃣ Video 3 (1080p)\n\n"
            "✅ Sab ho gaye toh /done likho"
        )
        return WAIT_FULL
    
    elif msg.animation:
        raw_caption = msg.caption if msg.caption else ""
        cleaned_title = clean_title(raw_caption)
        
        context.user_data['title'] = cleaned_title
        context.user_data['trim_file'] = msg.animation.file_id
        context.user_data['trim_type'] = 'animation'
        context.user_data['trim_chat_id'] = msg.chat_id
        context.user_data['trim_msg_id'] = msg.message_id
        
        await msg.reply_text(
            f"✅ Trim Animation Accepted!\n\n"
            f"📝 Title: {cleaned_title}\n"
            f"🎬 Type: GIF/Animation\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📹 STEP 2: FULL QUALITY VIDEOS\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Ab sab quality variants bhejo:\n"
            "📊 480p, 720p, 1080p, 2160p etc.\n\n"
            "Har quality separately:\n"
            "1️⃣ Video 1 (480p)\n"
            "2️⃣ Video 2 (720p)\n"
            "3️⃣ Video 3 (1080p)\n\n"
            "✅ Sab ho gaye toh /done likho"
        )
        return WAIT_FULL
    
    else:
        await msg.reply_text(
            "❌ ERROR: Ye video/photo nahi hai!\n\n"
            "📸 ACCEPTED FORMATS:\n"
            "✅ Video\n"
            "✅ Photo\n"
            "✅ Document\n"
            "✅ GIF/Animation\n\n"
            "❌ NA ACCEPTED:\n"
            "✗ Text\n"
            "✗ Audio\n"
            "✗ Voice\n\n"
            "OR\n\n"
            "👉 /skip karke aage badho (agar preview nahi chahiye)"
        )
        return WAIT_TRIM

async def get_full_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        logger.warning("❌ No message object found")
        return WAIT_FULL

    # 🔍 DEBUG: Dekho kya aa raha hai
    logger.info(f"📨 Message Type Received:")
    logger.info(f"   - has_video: {bool(msg.video)}")
    logger.info(f"   - has_document: {bool(msg.document)}")
    logger.info(f"   - has_animation: {bool(msg.animation)}")
    logger.info(f"   - has_text: {bool(msg.text)}")
    logger.info(f"   - is_forwarded: {bool(msg.forward_origin)}")
    logger.info(f"   - caption: {msg.caption[:50] if msg.caption else 'None'}")

    # Check for /done command
    if msg.text and msg.text.strip().lower() == '/done':
        logger.info("✅ /done command detected")
        return await finalize_single_upload(update, context)

    # Check for /cancel command
    if msg.text and msg.text.strip().lower() == '/cancel':
        logger.info("✅ /cancel command detected")
        context.user_data.clear()
        await msg.reply_text("❌ Upload process cancelled.")
        return ConversationHandler.END

    # Accept videos and documents
    if not msg.video and not msg.document and not msg.animation:
        logger.warning(f"❌ Unsupported message type received")
        await msg.reply_text(
            "❌ ERROR: Ye video/document nahi hai!\n\n"
            "📹 Supported:\n"
            "✅ Video (.mp4, .mkv, .avi, .mov)\n"
            "✅ Document\n"
            "✅ GIF/Animation\n\n"
            "OR /done likho"
        )
        return WAIT_FULL

    # Process video
    quality = detect_quality_from_message(msg)
    file_id = msg.video.file_id if msg.video else (msg.animation.file_id if msg.animation else msg.document.file_id)
    
    logger.info(f"✅ Processing: {quality} - File ID: {file_id[:20]}...")
    
    entry = {
        "quality": quality,
        "file_id": file_id,
        "source_chat_id": msg.chat_id,
        "source_msg_id": msg.message_id,
        "thumb_file_id": msg.video.thumbnail.file_id if msg.video and msg.video.thumbnail else None,
        "is_forwarded": bool(msg.forward_origin)
    }
    
    context.user_data['quality_payloads'].append(entry)
    
    saved_count = len(context.user_data['quality_payloads'])
    qualities_list = ", ".join([v['quality'] for v in context.user_data['quality_payloads']])
    
    await msg.reply_text(
        f"✅ {quality} Quality Saved!\n\n"
        f"📊 Total Variants: {saved_count}\n"
        f"📋 List: {qualities_list}\n"
        f"{'🔄 (Forwarded)' if msg.forward_origin else '📤 (Original)'}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "➕ AUR QUALITY VARIANTS:\n"
        "👉 Doosra quality variant bhejo\n\n"
        "✅ FINALIZE:\n"
        "👉 /done likho\n\n"
        "❌ CANCEL:\n"
        "👉 /cancel likho"
    )
    logger.info(f"✅ Quality saved. Current count: {saved_count}")
    return WAIT_FULL

async def finalize_single_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quality_payloads = context.user_data.get("quality_payloads", [])
    
    if not quality_payloads:
        await update.message.reply_text("⚠️ Abhi tak koi full video nahi mila. Pehle quality files bhejo.")
        return WAIT_FULL

    status = await update.message.reply_text("⏳ Processing saari qualities...")

    try:
        title = context.user_data.get('title', 'Exclusive Premium Content')
        saved_variants = []

        # Upload all variants to BACKUP_1
        for idx, payload in enumerate(quality_payloads):
            try:
                status_text = f"⏳ Uploading {payload['quality']}... ({idx+1}/{len(quality_payloads)})"
                await status.edit_text(status_text)
                
                backup_msg_id = None
                if BACKUP_1:
                    try:
                        copied = await context.bot.copy_message(
                            chat_id=BACKUP_1,
                            from_chat_id=payload['source_chat_id'],
                            message_id=payload['source_msg_id'],
                            caption=build_backup_caption(title),
                            parse_mode='HTML'
                        )
                        backup_msg_id = copied.message_id
                        logger.info(f"✅ {payload['quality']} uploaded to BACKUP_1: {backup_msg_id}")
                    except Exception as e:
                        logger.error(f"❌ Failed to backup {payload['quality']}: {e}")
                
                saved_variants.append({**payload, "backup_msg_id": backup_msg_id})
            except Exception as e:
                logger.error(f"Error processing variant: {e}")
                continue

        if not saved_variants:
            await status.edit_text("❌ Koi bhi quality upload nahi ho saki.")
            return ConversationHandler.END

        # Find best quality (lowest rank = best)
        hero_variant = min(saved_variants, key=lambda item: quality_rank(item['quality']))

        await status.edit_text("⏳ Database mein save ho raha hai...")

        # Database operations
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute(
            "INSERT INTO adult_videos (title, full_file_id, backup_msg_id) VALUES (%s, %s, %s) RETURNING vid_id",
            (title, hero_variant['file_id'], hero_variant['backup_msg_id'])
        )
        vid_id = cur.fetchone()

        # Insert all variants
        execute_values(
            cur,
            """
            INSERT INTO adult_video_files (vid_id, quality, file_id, backup_msg_id)
            VALUES %s
            """,
            [(vid_id, var['quality'], var['file_id'], var['backup_msg_id']) for var in saved_variants]
        )
        conn.commit()
        cur.close()
        db_pool.putconn(conn)

        logger.info(f"✅ Video saved to DB with ID: {vid_id}")

        # Copy to BACKUP_2 & PAID_CH
        await status.edit_text("⏳ Uploading to Paid Channels...")
        channels_to_upload = [ch for ch in [BACKUP_2, PAID_CH] if ch != 0]
        for ch_id in channels_to_upload:
            try:
                await context.bot.copy_message(
                    chat_id=ch_id,
                    from_chat_id=hero_variant['source_chat_id'],
                    message_id=hero_variant['source_msg_id'],
                    caption=build_backup_caption(title),
                    parse_mode='HTML'
                )
                logger.info(f"✅ Uploaded to channel: {ch_id}")
            except Exception as e:
                logger.error(f"❌ Failed to upload to {ch_id}: {e}")

        # Generate link
        await status.edit_text("⏳ Generating GPLinks...")
        web_link = f"{WEB_DOMAIN}/watch/{vid_id}"
        gplink = await shorten_link(web_link)

        # Post to Free Channel
        await status.edit_text("⏳ Posting to Free Channel...")
        caption = build_free_channel_caption(title, gplink)

        if FREE_CH != 0:
            try:
                trim_type = context.user_data.get('trim_type', 'video')
                trim_file_id = context.user_data.get('trim_file')

                if trim_type == 'photo' and trim_file_id == 'use_thumbnail':
                    await context.bot.send_photo(
                        chat_id=FREE_CH,
                        photo=hero_variant.get('thumb_file_id') or "https://i.imgur.com/6XK4F6K.png",
                        caption=caption,
                        parse_mode='HTML'
                    )
                elif trim_type in ['video', 'document']:
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
                            logger.error(f"copy_message failed: {e}")
                    
                logger.info(f"✅ Posted to Free Channel")
            except Exception as e:
                logger.error(f"❌ Error posting to free channel: {e}")

        display_title = generate_display_title(title)
        quality_list = ", ".join([v['quality'] for v in saved_variants])
        
        await status.edit_text(
            f"✅ ALL DONE!\n\n"
            f"🎬 Title: {display_title}\n"
            f"📊 Qualities: {quality_list}\n"
            f"🔗 Link: {gplink}\n"
            f"🖼️ Qualities Count: {len(saved_variants)}\n"
            f"⭐ Best Quality: {hero_variant['quality']}"
        )

    except Exception as e:
        logger.error(f"❌ Error in finalization: {e}")
        await status.edit_text(f"❌ Error: {str(e)[:100]}")

    context.user_data.clear()
    return ConversationHandler.END

# ================= BULK UPLOAD FEATURE =================
async def start_bulk_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Access Denied! Only admin can use this command.")
        return ConversationHandler.END
    
    context.user_data['bulk_videos'] = []
    context.user_data['bulk_count'] = 0
    
    await update.message.reply_text(
        "📦 BULK UPLOAD MODE ACTIVATED!\n\n"
        "🎬 Ab aap multiple videos ek saath forward kar sakte ho.\n\n"
        "📝 Instructions:\n"
        "1️⃣ Videos ek-ek karke forward karo\n"
        "2️⃣ Har video automatically process hogi\n"
        "3️⃣ /done likho jab saari videos bhej do\n"
        "4️⃣ /cancel se bulk upload cancel kar sakte ho\n\n"
        "⚡ Chalo shuru karte hain! Pehli video bhejo..."
    )
    return BULK_WAIT_VIDEO

async def process_bulk_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    
    if msg.text and msg.text.strip().lower() == '/done':
        bulk_count = context.user_data.get('bulk_count', 0)
        await msg.reply_text(
            f"✅ BULK UPLOAD COMPLETED!\n\n"
            f"📊 Total Videos Processed: {bulk_count}\n\n"
            f"🎉 Saari videos channel par post ho gayi hain!"
        )
        context.user_data.clear()
        return ConversationHandler.END
    
    if msg.text and msg.text.strip().lower() == '/cancel':
        context.user_data.clear()
        await msg.reply_text("❌ Bulk upload cancelled.")
        return ConversationHandler.END
    
    if not msg.video and not msg.document:
        await msg.reply_text("❌ Please forward a video file. Or type /done to finish.")
        return BULK_WAIT_VIDEO
    
    bulk_count = context.user_data.get('bulk_count', 0) + 1
    context.user_data['bulk_count'] = bulk_count
    
    status = await msg.reply_text(f"⏳ Processing Video #{bulk_count}...")
    
    try:
        raw_caption = msg.caption if msg.caption else ""
        title = clean_title(raw_caption)
        
        full_file_id = msg.video.file_id if msg.video else msg.document.file_id
        backup_caption = build_backup_caption(title)
        quality = detect_quality_from_message(msg)
        
        # BACKUP_1 par copy
        backup_msg_id = None
        if BACKUP_1 != 0:
            try:
                copied_msg = await context.bot.copy_message(
                    chat_id=BACKUP_1,
                    from_chat_id=msg.chat_id,
                    message_id=msg.message_id,
                    caption=backup_caption,
                    parse_mode='HTML'
                )
                backup_msg_id = copied_msg.message_id
                logger.info(f"✅ Bulk #{bulk_count}: Uploaded to Backup 1, Msg ID: {backup_msg_id}")
            except Exception as e:
                logger.error(f"❌ Bulk #{bulk_count}: Failed to copy to Backup 1: {e}")
        
        # Database save
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO adult_videos (title, full_file_id, backup_msg_id) VALUES (%s, %s, %s) RETURNING vid_id",
            (title, full_file_id, backup_msg_id)
        )
        vid_id = cur.fetchone()
        
        cur.execute(
            "INSERT INTO adult_video_files (vid_id, quality, file_id, backup_msg_id) VALUES (%s, %s, %s, %s)",
            (vid_id, quality, full_file_id, backup_msg_id)
        )
        conn.commit()
        cur.close()
        db_pool.putconn(conn)
        
        # BACKUP_2 & PAID_CH
        channels_to_upload = [ch for ch in [BACKUP_2, PAID_CH] if ch != 0]
        for ch_id in channels_to_upload:
            try:
                await context.bot.copy_message(
                    chat_id=ch_id,
                    from_chat_id=msg.chat_id,
                    message_id=msg.message_id,
                    caption=backup_caption,
                    parse_mode='HTML'
                )
                logger.info(f"✅ Bulk #{bulk_count}: Uploaded to channel {ch_id}")
            except Exception as e:
                logger.error(f"Failed to upload to {ch_id}: {e}")
        
        # Generate link
        web_link = f"{WEB_DOMAIN}/watch/{vid_id}"
        gplink = await shorten_link(web_link)
        
        # Free channel
        caption = build_free_channel_caption(title, gplink)
        
        if FREE_CH != 0:
            try:
                await context.bot.copy_message(
                    chat_id=FREE_CH,
                    from_chat_id=msg.chat_id,
                    message_id=msg.message_id,
                    caption=caption,
                    parse_mode='HTML'
                )
                logger.info(f"✅ Bulk #{bulk_count}: Posted to Free Channel")
            except Exception as e:
                logger.error(f"copy_message to free channel failed: {e}")
        
        display_title = generate_display_title(title)
        await status.edit_text(
            f"✅ Video #{bulk_count} Done!\n\n"
            f"📝 Title: {display_title}\n"
            f"📊 Quality: {quality}\n"
            f"🔗 Link: {gplink}\n\n"
            f"📥 Aur videos forward karo ya /done likho!"
        )
        
    except Exception as e:
        logger.error(f"Bulk upload error: {e}")
        await status.edit_text(f"❌ Error: {str(e)[:100]}\n\nContinue with next video or /done.")
    
    return BULK_WAIT_VIDEO

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
            
            conn = get_db_connection()
            cur = conn.cursor()
            
            # Get video info
            cur.execute("SELECT title FROM adult_videos WHERE vid_id = %s", (vid_id,))
            title_row = cur.fetchone()
            
            if not title_row:
                cur.close()
                db_pool.putconn(conn)
                await update.message.reply_text(
                    "❌ Video Not Found!\n\n"
                    "Yeh video delete ho chuki hai ya invalid link hai."
                )
                return
            
            title = title_row
            
            # Get all qualities
            cur.execute("""
                SELECT file_entry_id, quality, file_id, backup_msg_id
                FROM adult_video_files
                WHERE vid_id = %s
                ORDER BY 1
            """, (vid_id,))
            variants = cur.fetchall()
            cur.close()
            db_pool.putconn(conn)
            
            if not variants:
                await update.message.reply_text("❌ Koi quality available nahi hai.")
                return
            
            # Create buttons
            buttons = []
            sorted_variants = sorted(variants, key=lambda row: quality_rank(row[1]))
            
            for file_entry_id, quality, file_id, backup_msg_id in sorted_variants:
                btn_text = f"📥 {quality.upper()}"
                buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"quality:{file_entry_id}")])
            
            buttons.append([InlineKeyboardButton("❌ Close", callback_data="close_panel")])
            
            # Send message
            await update.message.reply_text(
                text=(
                    f"🎬 <b>{html_escape(title)}</b>\n\n"
                    f"👇 Apni pasand ki quality choose karein:\n\n"
                    f"⏱️ Auto-delete: {AUTO_DELETE_TIME // 60} minutes"
                ),
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode='HTML'
            )
            
        except ValueError:
            await update.message.reply_text("❌ Invalid video ID.")
        except Exception as e:
            logger.error(f"Provider Bot Error: {e}")
            await update.message.reply_text("❌ Something went wrong. Please try again.")
    else:
        await update.message.reply_text(
            "🔞 Welcome!\n\n"
            "Please use a valid video link to access content.\n"
            "Example: Click on a video link from our free channel.\n\n"
            "⚠️ Note: All videos auto-delete after some time for security."
        )

async def provider_quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "close_panel":
        try:
            await query.message.delete()
        except:
            pass
        return

    try:
        _, file_entry_id = query.data.split(":")
        file_entry_id = int(file_entry_id)

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT vf.file_id, vf.quality, vf.backup_msg_id, v.title
            FROM adult_video_files vf
            JOIN adult_videos v ON vf.vid_id = v.vid_id
            WHERE vf.file_entry_id = %s
        """, (file_entry_id,))
        row = cur.fetchone()
        cur.close()
        db_pool.putconn(conn)

        if not row:
            await query.message.reply_text("❌ Yeh quality available nahi rahi.")
            return

        file_id, quality, backup_msg_id, title = row

        caption = (
            f"🎬 {html_escape(title)}\n"
            f"📦 Quality: {quality}\n"
            f"⏱️ Auto-Delete: {AUTO_DELETE_TIME // 60} minutes\n"
            f"💾 Forward to Saved Messages!"
        )

        sent_msg = None
        
        # Primary: copy from backup
        if backup_msg_id and BACKUP_1:
            try:
                copied = await context.bot.copy_message(
                    chat_id=query.message.chat_id,
                    from_chat_id=BACKUP_1,
                    message_id=backup_msg_id,
                    caption=caption
                )
                sent_msg = copied.message_id
                logger.info(f"✅ Sent {quality} using copy_message")
            except Exception as e:
                logger.error(f"copy_message failed: {e}")

        # Fallback: file_id
        if not sent_msg:
            try:
                sent = await context.bot.send_video(
                    chat_id=query.message.chat_id,
                    video=file_id,
                    caption=caption,
                    supports_streaming=True
                )
                sent_msg = sent.message_id
                logger.info(f"✅ Sent {quality} using fallback")
            except Exception as e:
                logger.error(f"Fallback failed: {e}")
                await query.message.reply_text("❌ Video bhejne mein error aaya.")
                return

        # Schedule auto-delete
        asyncio.create_task(
            auto_delete_with_notification(
                context=context,
                chat_id=query.message.chat_id,
                message_id_to_delete=sent_msg,
                delete_time=AUTO_DELETE_TIME
            )
        )

        logger.info(f"✅ Auto-delete scheduled")
        
    except Exception as e:
        logger.error(f"Callback error: {e}")
        await query.message.reply_text("❌ Error processing request.")

async def periodic_cleanup(context):
    while True:
        await asyncio.sleep(3600)
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("DELETE FROM adult_videos WHERE created_at < NOW() - INTERVAL '7 days'")
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
    
    # Properly indented upload_conv block
    # In run_bots() function:

upload_conv = ConversationHandler(
    entry_points=[CommandHandler('post', start_upload)],
    states={
        WAIT_TRIM: [
            # All media types
            MessageHandler(
                filters.VIDEO | filters.PHOTO | filters.ANIMATION | 
                filters.Document.VIDEO | filters.Document.ALL,
                get_trim
            ),
            # Commands
            MessageHandler(filters.COMMAND, get_trim),
            # Text fallback
            MessageHandler(filters.TEXT, get_trim),
        ],
        WAIT_FULL: [
            # All video types including forwarded
            MessageHandler(
                filters.VIDEO | filters.Document.VIDEO | filters.ANIMATION | 
                filters.Document.ALL,
                get_full_video
            ),
            # Commands (for /done, /cancel, /skip)
            MessageHandler(filters.COMMAND, get_full_video),
            # Text fallback
            MessageHandler(filters.TEXT, get_full_video),
        ]
    },
    fallbacks=[CommandHandler('cancel', cancel_flow)]
)
main_app.add_handler(upload_conv)
    
    bulk_conv = ConversationHandler(
        entry_points=[CommandHandler('bulk', start_bulk_upload)],
        states={
            BULK_WAIT_VIDEO: [
                MessageHandler(filters.ALL & ~filters.COMMAND, process_bulk_video),
                MessageHandler(filters.COMMAND, process_bulk_video)
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel_flow)]
    )
    main_app.add_handler(bulk_conv)
    
    logger.info("✅ Main Bot handlers configured")

    if not PROVIDER_BOT_TOKEN:
        logger.error("❌ PROVIDER_BOT_TOKEN not found!")
        return
    
    provider_app = Application.builder().token(PROVIDER_BOT_TOKEN).build()
    provider_app.add_handler(CommandHandler('start', provider_start))
    provider_app.add_handler(CallbackQueryHandler(provider_quality_callback, pattern=r"^(quality|close_panel)"))
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
        logger.info("⚠️ REMINDER: Provider Bot ko BACKUP_1 channel mein Admin banana zaroori hai!")

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
    
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("✅ Flask server started in background thread")
    
    try:
        asyncio.run(run_bots())
    except KeyboardInterrupt:
        logger.info("🛑 Shutting down...")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
