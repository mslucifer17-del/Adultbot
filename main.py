import os
import re
import json
import asyncio
import logging
import aiohttp
import psycopg2
from html import escape as html_escape  # ✅ NEW IMPORT
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

AUTO_DELETE_TIME = int(os.environ.get("AUTO_DELETE_TIME", "300"))

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
                full_file_id TEXT,
                backup_msg_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            cur.execute("ALTER TABLE adult_videos ADD COLUMN backup_msg_id INTEGER;")
        except Exception:
            conn.rollback()
            
        conn.commit()
        cur.close()
        logger.info("✅ Database tables created/verified with backup_msg_id")
    except Exception as e:
        logger.error(f"❌ Database setup failed: {e}")
    finally:
        if conn:
            db_pool.putconn(conn)

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

# ✅ NEW: Build safe HTML caption (escapes all dynamic content)
def build_free_channel_caption(title, gplink):
    safe_title = html_escape(title)
    safe_gplink = html_escape(gplink)
    return (
        f"🔞 <b>{safe_title}</b>\n\n"
        f"🔥 <b>Watch Full Video &amp; Download:</b>\n"
        f"👉 {safe_gplink}\n\n"
        f"💎 <b>Join VIP for Direct Files:</b> https://t.me/YOUR_VIP_LINK"
    )

# ✅ NEW: Build safe HTML caption for backup/paid channels
def build_backup_caption(title):
    safe_title = html_escape(title)
    return f"🔒 {safe_title}"

# ✅ NEW: Build safe Markdown caption for provider bot
def build_provider_caption(title):
    # Escape Markdown special characters: _ * [ ] ( ) ~ ` > # + - = | { } . !
    safe_title = title.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace(']', '\\]')
    safe_title = safe_title.replace('(', '\\(').replace(')', '\\)').replace('~', '\\~').replace('`', '\\`')
    return (
        f"🎬 *{safe_title}*\n\n"
        f"⏱️ *Auto\\-Delete:* 5 minutes\n"
        f"💾 *Forward to Saved Messages ASAP\\!*\n\n"
        f"⚠️ _Yeh file automatically delete ho jayegi\\._"
    )

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

# ================= MAIN BOT: SINGLE UPLOAD FLOW =================
async def start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Access Denied! Only admin can use this command.")
        return ConversationHandler.END
    
    await update.message.reply_text(
        "⚡ Single Post Mode!\n\n"
        "✂️ Sabse pehle choti TRIMMED VIDEO (Preview) bhejo.\n\n"
        "Agar aapke paas trim video nahi hai, toh bas /skip likh kar bhej do!"
    )
    return WAIT_TRIM

async def get_trim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return WAIT_TRIM
    
    if msg.text:
        if msg.text.strip().lower() == '/skip':
            context.user_data['trim_file'] = 'use_thumbnail'
            context.user_data['trim_type'] = 'photo'
            await msg.reply_text(
                "⏭️ Trim Skipped!\n\n"
                "🔞 Ab seedha FULL HD VIDEO bhejo."
            )
            return WAIT_FULL
        else:
            await msg.reply_text("❌ Kripya Trimmed Video bhejo ya /skip likho.")
            return WAIT_TRIM

    if not msg.video and not msg.document and not msg.animation:
        await msg.reply_text("❌ Error: Ye video nahi hai. Kripya Trimmed Video bhejo ya /skip likho.")
        return WAIT_TRIM
    
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
    
    context.user_data['trim_chat_id'] = msg.chat_id
    context.user_data['trim_msg_id'] = msg.message_id
    
    await msg.reply_text(
        f"✅ Trim Video Saved!\n\n"
        f"📝 Title: {cleaned_title}\n\n"
        "🔞 Ab FULL HD VIDEO bhejo."
    )
    return WAIT_FULL

async def get_full_and_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return WAIT_FULL

    if not msg.video and not msg.document:
        await msg.reply_text("❌ Error: Ye Full Video nahi lag rahi. Kripya Video File bhejein.")
        return WAIT_FULL

    status = await msg.reply_text("⏳ Processing Started...")
    
    title = context.user_data.get('title', '')
    if not title:
        raw_caption = msg.caption if msg.caption else ""
        title = clean_title(raw_caption)
        context.user_data['title'] = title

    full_file_id = msg.video.file_id if msg.video else msg.document.file_id
    trim_type = context.user_data.get('trim_type', 'video')
    trim_file_id = context.user_data.get('trim_file')

    # Handle thumbnail for free channel when trim is skipped
    thumbnail_bytes = None
    if trim_file_id == 'use_thumbnail':
        thumb_obj = None
        if msg.video and msg.video.thumbnail:
            thumb_obj = msg.video.thumbnail
        elif msg.document and msg.document.thumbnail:
            thumb_obj = msg.document.thumbnail
        
        if thumb_obj:
            try:
                file_info = await context.bot.get_file(thumb_obj.file_id)
                downloaded_bytes = await file_info.download_as_bytearray()
                thumbnail_bytes = bytes(downloaded_bytes)
                trim_type = 'photo_bytes'
            except Exception as e:
                logger.error(f"Thumbnail extraction error: {e}")
                trim_file_id = "https://i.imgur.com/6XK4F6K.png"
                trim_type = 'photo_url'
        else:
            trim_file_id = "https://i.imgur.com/6XK4F6K.png"
            trim_type = 'photo_url'

    # ✅ STEP 1: BACKUP_1 par copy_message se upload (thumbnail preserved)
    await status.edit_text("⏳ Uploading to Backup Channel...")
    backup_msg_id = None
    backup_caption = build_backup_caption(title)
    
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
            logger.info(f"✅ Uploaded to Backup 1, Msg ID: {backup_msg_id}")
        except Exception as e:
            logger.error(f"❌ Failed to copy to Backup 1: {e}")

    # ✅ STEP 2: Database mein save karo
    conn = None
    vid_id = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO adult_videos (title, full_file_id, backup_msg_id) VALUES (%s, %s, %s) RETURNING vid_id", 
            (title, full_file_id, backup_msg_id)
        )
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

    # ✅ STEP 3: BACKUP_2 & PAID_CH par copy_message
    await status.edit_text("⏳ Uploading to Paid Channel & Backup 2...")
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
            logger.info(f"✅ Uploaded to channel: {ch_id}")
        except Exception as e:
            logger.error(f"❌ Failed to upload to {ch_id}: {e}")

    # Generate Links
    web_link = f"{WEB_DOMAIN}/watch/{vid_id}"
    await status.edit_text("⏳ Generating GPLinks...")
    gplink = await shorten_link(web_link)

    # ✅ STEP 4: Post to Free Channel with SAFE HTML caption
    await status.edit_text("⏳ Posting to Free Channel...")
    caption = build_free_channel_caption(title, gplink)

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
            
            display_title = generate_display_title(title)
            await status.edit_text(
                f"✅ ALL DONE!\n\n"
                f"🎬 Title: {display_title}\n"
                f"🔗 Link: {gplink}\n"
                f"🖼️ Thumbnail: Preserved via copy_message\n"
                f"📦 Backup Msg ID: {backup_msg_id}"
            )
        else:
            await status.edit_text(
                f"✅ Done! (Free Channel ID not set)\n"
                f"🔗 Link: {gplink}"
            )
    except Exception as e:
        logger.error(f"❌ Error posting to free channel: {e}")
        await status.edit_text(f"❌ Error posting to free channel: {e}")

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
        "🎬 Ab aap 10-15 videos ek saath forward kar sakte ho.\n\n"
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
        
        # ✅ STEP 1: BACKUP_1 par copy_message
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
        
        # ✅ STEP 2: Database mein save karo
        conn = None
        vid_id = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO adult_videos (title, full_file_id, backup_msg_id) VALUES (%s, %s, %s) RETURNING vid_id",
                (title, full_file_id, backup_msg_id)
            )
            vid_id = cur.fetchone()[0]
            conn.commit()
            cur.close()
        finally:
            if conn:
                db_pool.putconn(conn)
        
        # ✅ STEP 3: BACKUP_2 & PAID_CH par copy_message
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
        
        # ✅ STEP 4: Free channel par SAFE HTML caption ke saath post
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
                try:
                    await context.bot.send_video(
                        chat_id=FREE_CH,
                        video=full_file_id,
                        caption=caption,
                        parse_mode='HTML',
                        supports_streaming=True
                    )
                except Exception as e2:
                    logger.error(f"Fallback send_video also failed: {e2}")
        
        display_title = generate_display_title(title)
        await status.edit_text(
            f"✅ Video #{bulk_count} Done!\n\n"
            f"📝 Title: {display_title}\n"
            f"🔗 Link: {gplink}\n"
            f"🖼️ Thumbnail: Preserved via copy_message\n\n"
            f"📥 Aur videos forward karo ya /done likho!"
        )
        
    except Exception as e:
        logger.error(f"Bulk upload error: {e}")
        await status.edit_text(f"❌ Error processing video #{bulk_count}: {e}\n\nContinue with next video or /done to finish.")
    
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
            
            conn = None
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("SELECT title, full_file_id, backup_msg_id FROM adult_videos WHERE vid_id = %s", (vid_id,))
                result = cur.fetchone()
                cur.close()
            finally:
                if conn:
                    db_pool.putconn(conn)

            if result:
                title, file_id, backup_msg_id = result
                
                warning_msg = await update.message.reply_text(
                    text=(
                        f"👋 Hello {user_name}!\n\n"
                        "⚠️ IMPORTANT NOTICE\n\n"
                        "🕒 Yeh video 5 minutes baad auto-delete ho jayegi.\n\n"
                        "💾 Saved Messages mein forward zaroor kar lena!\n\n"
                        "🔒 Yeh copyright protection ke liye hai.\n\n"
                        "⏳ Video bhej rahe hain..."
                    )
                )
                
                await asyncio.sleep(2)
                try:
                    await warning_msg.delete()
                except:
                    pass
                
                # ✅ SAFE caption without parse_mode issues
                caption_text = (
                    f"🎬 {title}\n\n"
                    f"⏱️ Auto-Delete: 5 minutes\n"
                    f"💾 Forward to Saved Messages ASAP!\n\n"
                    f"⚠️ Yeh file automatically delete ho jayegi."
                )

                sent_msg_id = None
                
                # ✅ PRIMARY: copy_message from Backup Channel (thumbnail preserved!)
                if backup_msg_id and BACKUP_1 != 0:
                    try:
                        copied_msg = await context.bot.copy_message(
                            chat_id=chat_id,
                            from_chat_id=BACKUP_1,
                            message_id=backup_msg_id,
                            caption=caption_text
                        )
                        sent_msg_id = copied_msg.message_id
                        logger.info(f"✅ Sent video to user {chat_id} using copy_message")
                    except Exception as e:
                        logger.error(f"copy_message failed, falling back: {e}")
                
                # ✅ FALLBACK: file_id se bhejo
                if not sent_msg_id:
                    try:
                        fallback_msg = await update.message.reply_video(
                            video=file_id, 
                            caption=caption_text,
                            supports_streaming=True
                        )
                        sent_msg_id = fallback_msg.message_id
                        logger.info(f"✅ Sent video to user {chat_id} using fallback file_id")
                    except Exception as e:
                        logger.error(f"Fallback send_video also failed: {e}")
                        await update.message.reply_text("❌ Video bhejne mein error aaya. Please try again.")
                        return
                
                # Auto delete schedule
                asyncio.create_task(
                    auto_delete_with_notification(
                        context=context,
                        chat_id=chat_id,
                        message_id_to_delete=sent_msg_id,
                        delete_time=AUTO_DELETE_TIME
                    )
                )
                
                logger.info(f"✅ Auto-delete scheduled for user {chat_id}")
                
            else:
                await update.message.reply_text(
                    "❌ Video Not Found!\n\n"
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
            "🔞 Welcome!\n\n"
            "Please use a valid video link to access content.\n"
            "Example: Click on a video link from our free channel.\n\n"
            "⚠️ Note: All videos auto-delete after 5 minutes for security."
        )

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
        
