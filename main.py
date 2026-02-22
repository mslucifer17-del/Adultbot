import os
import re
import json
import asyncio
import logging
import aiohttp
import psycopg2
from psycopg2 import pool
from flask import Flask, redirect
from threading import Thread
from io import BytesIO
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
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

# ⏱️ AUTO DELETE TIME (in seconds)
AUTO_DELETE_TIME = int(os.environ.get("AUTO_DELETE_TIME", "300"))

# STATES FOR UPLOAD FLOW
WAIT_TRIM, WAIT_FULL = range(2)

# STATES FOR BULK UPLOAD
BULK_WAIT_VIDEO, BULK_CONFIRM = range(2)

# ================= DATABASE SETUP =================
db_pool = None

def init_db_pool():
    """Initialize database connection pool"""
    global db_pool
    try:
        db_pool = pool.SimpleConnectionPool(1, 20, dsn=DATABASE_URL)
        logger.info("✅ Database pool created successfully")
    except Exception as e:
        logger.error(f"❌ Database pool creation failed: {e}")
        raise

def setup_db():
    """Create tables if not exist"""
    conn = None
    try:
        conn = db_pool.getconn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS adult_videos (
                vid_id SERIAL PRIMARY KEY,
                title TEXT,
                full_file_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        cur.close()
        logger.info("✅ Database tables created/verified")
    except Exception as e:
        logger.error(f"❌ Database setup failed: {e}")
    finally:
        if conn:
            db_pool.putconn(conn)

# ================= HELPER FUNCTIONS =================

# ✅ NEW: Clean Title Function
def clean_title(raw_title):
    """
    Clean the title by removing:
    - @mentions and channel names
    - File extensions (.mkv, .mp4, etc.)
    - Extra text like "1min from 0:06:51 of"
    - Special characters and extra spaces
    """
    if not raw_title:
        return "🔥 Exclusive Premium Content 🔥"
    
    title = raw_title.strip()
    
    # Remove @mentions (e.g., @UnratedHD, @ChannelName)
    title = re.sub(r'@\w+', '', title)
    
    # Remove "1min from 0:06:51 of" type text
    title = re.sub(r'\d+min\s+from\s+\d+:\d+:\d+\s+of\s+', '', title, flags=re.IGNORECASE)
    
    # Remove file extensions
    title = re.sub(r'\.(mkv|mp4|avi|mov|wmv|flv|webm|m4v)', '', title, flags=re.IGNORECASE)
    
    # Remove common unwanted words
    unwanted_patterns = [
        r'\b(Seva|HEVC|HDRip|UNRAT|UNRATED|720p|1080p|480p|4K|2160p)\b',
        r'\b(Dzyreplay|DZREPLAY|Replay)\b',
        r'\b(S\d+|Season\s*\d+|E\d+|Episode\s*\d+)\b',
    ]
    for pattern in unwanted_patterns:
        title = re.sub(pattern, '', title, flags=re.IGNORECASE)
    
    # Remove extra spaces and special characters
    title = re.sub(r'\s+', ' ', title)
    title = re.sub(r'[^\w\s\-\(\)]', '', title)
    title = title.strip()
    
    # If title becomes too short, use default
    if len(title) < 5:
        return "🔥 Exclusive Premium Content 🔥"
    
    # Add emoji for better look
    return f"🔞 {title}"

# ✅ NEW: Generate Short Title for Display
def generate_display_title(cleaned_title):
    """Generate a shorter display title for status messages"""
    if len(cleaned_title) > 50:
        return cleaned_title[:47] + "..."
    return cleaned_title

async def shorten_link(long_url):
    """Shorten URL using GPLinks API"""
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
    """Get database connection from pool"""
    if db_pool is None:
        raise Exception("Database pool not initialized")
    return db_pool.getconn()

async def extract_thumbnail_as_bytes(context, thumb_obj):
    """Extract thumbnail from video and return as bytes"""
    try:
        file_info = await context.bot.get_file(thumb_obj.file_id)
        downloaded_bytes = await file_info.download_as_bytearray()
        return bytes(downloaded_bytes)
    except Exception as e:
        logger.error(f"Thumbnail extraction error: {e}")
        return None

async def auto_delete_with_notification(context, chat_id, video_msg, delete_time=AUTO_DELETE_TIME):
    """Auto-delete video after specified time with prior notification"""
    try:
        wait_time = max(delete_time - 30, 60)
        await asyncio.sleep(wait_time)
        
        try:
            warning_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "⚠️ **IMPORTANT NOTICE**\n\n"
                    "🕒 Yeh video **30 seconds** mein auto-delete ho jayegi!\n\n"
                    "💾 **Jaldi se Saved Messages mein forward kar lo!**\n\n"
                    "🔒 _Yeh copyright protection ke liye hai._"
                ),
                parse_mode='Markdown'
            )
            await asyncio.sleep(5)
            await warning_msg.delete()
        except Exception as e:
            logger.error(f"Warning message error: {e}")
        
        await asyncio.sleep(30)
        
        try:
            await video_msg.delete()
            logger.info(f"✅ Video message deleted for chat: {chat_id}")
        except Exception as e:
            logger.error(f"Failed to delete video message: {e}")
            
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "🗑️ **Video Auto-Deleted!**\n\n"
                    "✅ Agar aapne forward kar liya hai toh saved messages mein check karein.\n"
                    "❌ Nahi kiya toh dobara link se access karein."
                ),
                parse_mode='Markdown'
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

# ================= MAIN BOT: SINGLE UPLOAD FLOW =================
async def start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: Admin /post command deta hai"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Access Denied! Only admin can use this command.")
        return ConversationHandler.END
    
    await update.message.reply_text(
        "⚡ **Single Post Mode!**\n\n"
        "✂️ Sabse pehle choti **TRIMMED VIDEO (Preview)** bhejo.\n\n"
        "*(Agar aapke paas trim video nahi hai, toh bas `/skip` likh kar bhej do!)*", 
        parse_mode='Markdown'
    )
    return WAIT_TRIM

async def get_trim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2: Admin Trim video bhejta hai YA /skip karta hai"""
    msg = update.message
    if not msg:
        return WAIT_TRIM
    
    if msg.text:
        if msg.text.strip().lower() == '/skip':
            context.user_data['trim_file'] = 'use_thumbnail'
            context.user_data['trim_type'] = 'photo'
            await msg.reply_text(
                "⏭️ **Trim Skipped!**\n\n"
                "🔞 Ab seedha **FULL HD VIDEO** bhejo. Main uska auto-thumbnail nikal kar Free Channel par post kar dunga!"
            )
            return WAIT_FULL
        else:
            await msg.reply_text("❌ Kripya Trimmed Video bhejo ya `/skip` likho.")
            return WAIT_TRIM

    if not msg.video and not msg.document and not msg.animation:
        await msg.reply_text("❌ Error: Ye video nahi hai. Kripya Trimmed Video bhejo ya `/skip` likho.")
        return WAIT_TRIM
    
    # ✅ FIXED: Clean the title from caption
    raw_caption = msg.caption if msg.caption else ""
    cleaned_title = clean_title(raw_caption)
    
    context.user_data['title'] = cleaned_title
    
    if msg.video:
        context.user_data['trim_file'] = msg.video.file_id
        context.user_data['trim_type'] = 'video'
    elif msg.animation:
        context.user_data['trim_file'] = msg.animation.file_id
        context.user_data['trim_type'] = 'video'
    else:
        context.user_data['trim_file'] = msg.document.file_id
        context.user_data['trim_type'] = 'document'
    
    await msg.reply_text(
        f"✅ **Trim Video Saved!**\n\n"
        f"📝 Title: `{cleaned_title}`\n\n"
        "🔞 Ab **FULL HD VIDEO** bhejo.",
        parse_mode='Markdown'
    )
    return WAIT_FULL

async def get_full_and_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 3: Admin Full video bhejta hai aur Bot sab auto-process kar deta hai"""
    msg = update.message
    if not msg:
        return WAIT_FULL

    if not msg.video and not msg.document:
        await msg.reply_text("❌ Error: Ye Full Video nahi lag rahi. Kripya Video File bhejein.")
        return WAIT_FULL

    status = await msg.reply_text("⏳ **Processing Started...**")
    
    # ✅ FIXED: Clean title from full video caption if not already set
    title = context.user_data.get('title', '')
    if not title or title == "🔞 ":
        raw_caption = msg.caption if msg.caption else ""
        title = clean_title(raw_caption)
        context.user_data['title'] = title

    full_file_id = msg.video.file_id if msg.video else msg.document.file_id
    trim_type = context.user_data.get('trim_type', 'video')
    trim_file_id = context.user_data.get('trim_file')

    thumbnail_bytes = None
    if trim_file_id == 'use_thumbnail':
        await status.edit_text("⏳ Extracting Thumbnail from Video...")
        
        thumb_obj = None
        if msg.video and msg.video.thumbnail:
            thumb_obj = msg.video.thumbnail
        elif msg.document and msg.document.thumbnail:
            thumb_obj = msg.document.thumbnail
            
        if thumb_obj:
            thumbnail_bytes = await extract_thumbnail_as_bytes(context, thumb_obj)
            if thumbnail_bytes:
                trim_type = 'photo_bytes'
            else:
                trim_file_id = "https://i.imgur.com/6XK4F6K.png"
                trim_type = 'photo_url'
        else:
            trim_file_id = "https://i.imgur.com/6XK4F6K.png"
            trim_type = 'photo_url'

    # Save to Database
    conn = None
    vid_id = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO adult_videos (title, full_file_id) VALUES (%s, %s) RETURNING vid_id", (title, full_file_id))
        vid_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        logger.info(f"✅ Video saved to DB with ID: {vid_id}")
    except Exception as e:
        logger.error(f"❌ Database error: {e}")
        await status.edit_text(f"❌ Database error: {e}")
        context.user_data.clear()
        return ConversationHandler.END
    finally:
        if conn:
            db_pool.putconn(conn)

    # Upload to Backups & Paid Channel
    await status.edit_text("⏳ Uploading to Backups & Paid Channel...")
    channels_to_upload = [ch for ch in [BACKUP_1, BACKUP_2, PAID_CH] if ch != 0]
    
    for ch_id in channels_to_upload:
        try:
            await context.bot.send_video(chat_id=ch_id, video=full_file_id, caption=f"🔒 {title}")
            logger.info(f"✅ Uploaded to channel: {ch_id}")
        except Exception as e:
            logger.error(f"❌ Failed to upload to {ch_id}: {e}")

    # Generate Links
    web_link = f"{WEB_DOMAIN}/watch/{vid_id}"
    
    await status.edit_text("⏳ Generating GPLinks...")
    gplink = await shorten_link(web_link)

    # Post to Free Channel
    await status.edit_text("⏳ Posting to Free Channel...")
    caption = (
        f"🔞 <b>{title}</b>\n\n"
        f"🔥 <b>Watch Full Video & Download:</b>\n"
        f"👉 {gplink}\n\n"
        f"💎 <b>Join VIP for Direct Files:</b> https://t.me/+wcYoTQhIz-ZmOTY1"
    )

    try:
        if FREE_CH != 0:
            if trim_type == 'photo_bytes' and thumbnail_bytes:
                photo_file = BytesIO(thumbnail_bytes)
                photo_file.name = "thumbnail.jpg"
                await context.bot.send_photo(
                    chat_id=FREE_CH, 
                    photo=photo_file, 
                    caption=caption, 
                    parse_mode='HTML'
                )
            elif trim_type == 'photo_url':
                await context.bot.send_photo(
                    chat_id=FREE_CH, 
                    photo=trim_file_id, 
                    caption=caption, 
                    parse_mode='HTML'
                )
            elif trim_type in ['video', 'document']:
                await context.bot.send_video(
                    chat_id=FREE_CH, 
                    video=trim_file_id, 
                    caption=caption, 
                    parse_mode='HTML'
                )
            
            display_title = generate_display_title(title)
            await status.edit_text(
                f"✅ **ALL DONE!**\n\n"
                f"🎬 Title: `{display_title}`\n"
                f"🔗 Link: {gplink}",
                parse_mode='Markdown'
            )
        else:
            await status.edit_text(
                f"✅ **Done!** (Free Channel ID not set)\n"
                f"🔗 Link: {gplink}",
                parse_mode='Markdown'
            )
    except Exception as e:
        logger.error(f"❌ Error posting to free channel: {e}")
        await status.edit_text(f"❌ Error posting to free channel: {e}")

    context.user_data.clear()
    return ConversationHandler.END

# ================= BULK UPLOAD FEATURE =================
async def start_bulk_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start bulk upload mode - forward 10-15 videos at once"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Access Denied! Only admin can use this command.")
        return ConversationHandler.END
    
    context.user_data['bulk_videos'] = []
    context.user_data['bulk_count'] = 0
    
    await update.message.reply_text(
        "📦 **BULK UPLOAD MODE ACTIVATED!**\n\n"
        "🎬 Ab aap **10-15 videos ek saath forward** kar sakte ho.\n\n"
        "📝 **Instructions:**\n"
        "1️⃣ Videos ek-ek karke forward karo\n"
        "2️⃣ Har video automatically process hogi\n"
        "3️⃣ `/done` likho jab saari videos bhej do\n"
        "4️⃣ `/cancel` se bulk upload cancel kar sakte ho\n\n"
        "⚡ **Chalo shuru karte hain! Pehli video bhejo...**",
        parse_mode='Markdown'
    )
    return BULK_WAIT_VIDEO

async def process_bulk_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process each video in bulk upload"""
    msg = update.message
    
    # Check for /done command
    if msg.text and msg.text.strip().lower() == '/done':
        bulk_count = context.user_data.get('bulk_count', 0)
        await msg.reply_text(
            f"✅ **BULK UPLOAD COMPLETED!**\n\n"
            f"📊 Total Videos Processed: `{bulk_count}`\n\n"
            f"🎉 Saari videos channel par post ho gayi hain!",
            parse_mode='Markdown'
        )
        context.user_data.clear()
        return ConversationHandler.END
    
    # Check for /cancel command
    if msg.text and msg.text.strip().lower() == '/cancel':
        context.user_data.clear()
        await msg.reply_text("❌ Bulk upload cancelled.")
        return ConversationHandler.END
    
    # Check if it's a video
    if not msg.video and not msg.document:
        await msg.reply_text("❌ Please forward a video file. Or type `/done` to finish.")
        return BULK_WAIT_VIDEO
    
    # Process the video
    bulk_count = context.user_data.get('bulk_count', 0) + 1
    context.user_data['bulk_count'] = bulk_count
    
    status = await msg.reply_text(f"⏳ **Processing Video #{bulk_count}...**")
    
    try:
        # Extract and clean title
        raw_caption = msg.caption if msg.caption else ""
        title = clean_title(raw_caption)
        
        full_file_id = msg.video.file_id if msg.video else msg.document.file_id
        
        # Save to database
        conn = None
        vid_id = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("INSERT INTO adult_videos (title, full_file_id) VALUES (%s, %s) RETURNING vid_id", (title, full_file_id))
            vid_id = cur.fetchone()[0]
            conn.commit()
            cur.close()
        finally:
            if conn:
                db_pool.putconn(conn)
        
        # Upload to backup channels
        channels_to_upload = [ch for ch in [BACKUP_1, BACKUP_2, PAID_CH] if ch != 0]
        for ch_id in channels_to_upload:
            try:
                await context.bot.send_video(chat_id=ch_id, video=full_file_id, caption=f"🔒 {title}")
            except Exception as e:
                logger.error(f"Failed to upload to {ch_id}: {e}")
        
        # Generate link
        web_link = f"{WEB_DOMAIN}/watch/{vid_id}"
        gplink = await shorten_link(web_link)
        
        # Post to free channel
        caption = (
            f"🔞 <b>{title}</b>\n\n"
            f"🔥 <b>Watch Full Video & Download:</b>\n"
            f"👉 {gplink}\n\n"
            f"💎 <b>Join VIP for Direct Files:</b> https://t.me/+wcYoTQhIz-ZmOTY1"
        )
        
        if FREE_CH != 0:
            # Extract thumbnail for free channel
            thumbnail_bytes = None
            thumb_obj = None
            if msg.video and msg.video.thumbnail:
                thumb_obj = msg.video.thumbnail
            elif msg.document and msg.document.thumbnail:
                thumb_obj = msg.document.thumbnail
            
            if thumb_obj:
                thumbnail_bytes = await extract_thumbnail_as_bytes(context, thumb_obj)
            
            if thumbnail_bytes:
                photo_file = BytesIO(thumbnail_bytes)
                photo_file.name = "thumbnail.jpg"
                await context.bot.send_photo(
                    chat_id=FREE_CH, 
                    photo=photo_file, 
                    caption=caption, 
                    parse_mode='HTML'
                )
            elif msg.video:
                await context.bot.send_video(
                    chat_id=FREE_CH, 
                    video=full_file_id, 
                    caption=caption, 
                    parse_mode='HTML'
                )
        
        display_title = generate_display_title(title)
        await status.edit_text(
            f"✅ **Video #{bulk_count} Done!**\n\n"
            f"📝 Title: `{display_title}`\n"
            f"🔗 Link: {gplink}\n\n"
            f"📥 **Aur videos forward karo ya `/done` likho!**",
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Bulk upload error: {e}")
        await status.edit_text(f"❌ Error processing video #{bulk_count}: {e}\n\nContinue with next video or `/done` to finish.")
    
    return BULK_WAIT_VIDEO

async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the current conversation"""
    context.user_data.clear()
    await update.message.reply_text("❌ Upload process cancelled.")
    return ConversationHandler.END

# ================= PROVIDER BOT: GIVING THE VIDEO =================
async def provider_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Provider bot sends video to user with auto-delete"""
    text = update.message.text
    chat_id = update.effective_chat.id
    user_name = update.effective_user.first_name

    if text and "vid_" in text:
        try:
            vid_id = int(text.split("vid_")[1])
            
            conn = None
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("SELECT title, full_file_id FROM adult_videos WHERE vid_id = %s", (vid_id,))
                result = cur.fetchone()
                cur.close()
            finally:
                if conn:
                    db_pool.putconn(conn)

            if result:
                title, file_id = result
                
                warning_msg = await update.message.reply_text(
                    text=(
                        f"👋 Hello **{user_name}**!\n\n"
                        "⚠️ **IMPORTANT NOTICE**\n\n"
                        "🕒 Yeh video **5 minutes** baad auto-delete ho jayegi.\n\n"
                        "💾 **Saved Messages mein forward zaroor kar lena!**\n\n"
                        "🔒 _Yeh copyright protection ke liye hai._\n\n"
                        "⏳ Video bhej rahe hain..."
                    ),
                    parse_mode='Markdown'
                )
                
                await asyncio.sleep(3)
                try:
                    await warning_msg.delete()
                except:
                    pass
                
                msg = await update.message.reply_video(
                    video=file_id, 
                    caption=(
                        f"🎬 **{title}**\n\n"
                        f"⏱️ **Auto-Delete:** 5 minutes\n"
                        f"💾 **Forward to Saved Messages ASAP!**\n\n"
                        f"⚠️ _Yeh file automatically delete ho jayegi._"
                    ),
                    parse_mode='Markdown',
                    supports_streaming=True
                )
                
                asyncio.create_task(
                    auto_delete_with_notification(
                        context=context,
                        chat_id=chat_id,
                        video_msg=msg,
                        delete_time=AUTO_DELETE_TIME
                    )
                )
                
                logger.info(f"✅ Video sent to user {chat_id}, auto-delete scheduled")
                
            else:
                await update.message.reply_text(
                    "❌ **Video Not Found!**\n\n"
                    "Yeh video delete ho chuki hai ya invalid link hai.\n"
                    "Naya link free channel se lein."
                )
        except ValueError:
            await update.message.reply_text("❌ Invalid video ID.")
        except Exception as e:
            logger.error(f"Provider Bot Error: {e}")
            await update.message.reply_text("❌ Something went wrong. Please try again.")
    else:
        await update.message.reply_text(
            "🔞 **Welcome!**\n\n"
            "Please use a valid video link to access content.\n"
            "Example: Click on a video link from our free channel.\n\n"
            "⚠️ **Note:** All videos auto-delete after 5 minutes for security."
        )

async def periodic_cleanup(context):
    """Periodic cleanup of old database entries"""
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
    """Run both Main and Provider bots"""
    if not MAIN_BOT_TOKEN:
        logger.error("❌ MAIN_BOT_TOKEN not found!")
        return
    
    main_app = Application.builder().token(MAIN_BOT_TOKEN).build()
    
    # Single Upload Conversation
    upload_conv = ConversationHandler(
        entry_points=[CommandHandler('post', start_upload)],
        states={
            WAIT_TRIM: [MessageHandler(filters.ALL & ~filters.COMMAND, get_trim),
                       MessageHandler(filters.COMMAND, get_trim)],
            WAIT_FULL: [MessageHandler(filters.ALL & ~filters.COMMAND, get_full_and_process),
                       MessageHandler(filters.COMMAND, get_full_and_process)]
        },
        fallbacks=[CommandHandler('cancel', cancel_flow)]
    )
    main_app.add_handler(upload_conv)
    
    # ✅ BULK UPLOAD Conversation
    bulk_conv = ConversationHandler(
        entry_points=[CommandHandler('bulk', start_bulk_upload)],
        states={
            BULK_WAIT_VIDEO: [MessageHandler(filters.ALL & ~filters.COMMAND, process_bulk_video),
                             MessageHandler(filters.COMMAND, process_bulk_video)]
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
