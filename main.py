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
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    filters, ContextTypes, ConversationHandler
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================= VARIABLES =================
MAIN_BOT_TOKEN = os.environ.get("MAIN_BOT_TOKEN")
PROVIDER_BOT_TOKEN = os.environ.get("PROVIDER_BOT_TOKEN")
PROVIDER_BOT_USERNAME = os.environ.get("PROVIDER_BOT_USERNAME")
GPLINKS_API_KEY = os.environ.get("GPLINKS_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")
WEB_DOMAIN = os.environ.get("WEB_DOMAIN") # e.g., https://my-bot.onrender.com
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))

FREE_CH = int(os.environ.get("FREE_CHANNEL_ID", "0"))
PAID_CH = int(os.environ.get("PAID_CHANNEL_ID", "0"))
BACKUP_1 = int(os.environ.get("BACKUP_CHANNEL_1", "0"))
BACKUP_2 = int(os.environ.get("BACKUP_CHANNEL_2", "0"))

# STATES FOR UPLOAD FLOW
WAIT_TITLE, WAIT_TRIM, WAIT_FULL = range(3)

# ================= DATABASE SETUP =================
db_pool = psycopg2.pool.SimpleConnectionPool(1, 20, dsn=DATABASE_URL)

def setup_db():
    conn = db_pool.getconn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS adult_videos (
            vid_id SERIAL PRIMARY KEY,
            title TEXT,
            full_file_id TEXT
        )
    """)
    conn.commit()
    cur.close()
    db_pool.putconn(conn)

setup_db()

# ================= HELPER FUNCTIONS =================
async def shorten_link(long_url):
    api_url = f"https://gplinks.in/api?api={GPLINKS_API_KEY}&url={long_url}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as resp:
                data = await resp.json()
                if data.get("status") == "success":
                    return data.get("shortenedUrl")
    except Exception as e:
        logger.error(f"GPLink Error: {e}")
    return long_url

# ================= WEB REDIRECTOR (FLASK) =================
# Ye system ban lagne se bachayega. Old links hamesha is Flask route par aayenge, 
# aur Flask unko CURRENT_ACTIVE provider bot par bhej dega.
app = Flask(__name__)

@app.route('/')
def home():
    return "Server is Running!"

@app.route('/watch/<int:vid_id>')
def watch_video(vid_id):
    # Agar bot ban ho gaya hai, to aap .env me PROVIDER_BOT_USERNAME change kar doge,
    # aur saari purani links yahan se naye bot par chali jayengi!
    bot_username = os.environ.get("PROVIDER_BOT_USERNAME")
    return redirect(f"tg://resolve?domain={bot_username}&start=vid_{vid_id}")

def run_flask():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

# ================= MAIN BOT: UPLOAD FLOW =================
async def start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: return
    await update.message.reply_text("🎬 **New Post Start!**\n\nSabse pehle Video ka **TITLE** bhejo:", parse_mode='Markdown')
    return WAIT_TITLE

async def get_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['title'] = update.message.text
    await update.message.reply_text("✂️ **Title Saved!**\n\nAb choti si **TRIMMED VIDEO (Preview)** bhejo ya forward karo:")
    return WAIT_TRIM

async def get_trim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.video and not update.message.document and not update.message.animation:
        await update.message.reply_text("❌ Error: Video bhejo bhai.")
        return WAIT_TRIM
    
    context.user_data['trim_file'] = update.message.video.file_id if update.message.video else (update.message.animation.file_id if update.message.animation else update.message.document.file_id)
    await update.message.reply_text("✅ **Trim Video Saved!**\n\n🔞 Ab **FULL HD VIDEO** bhejo. Jiske baad main apna jaadu karunga.")
    return WAIT_FULL

async def get_full_and_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.video and not update.message.document:
        await update.message.reply_text("❌ Error: Full Video bhejo.")
        return WAIT_FULL

    status = await update.message.reply_text("⏳ **Processing Started...**")
    
    title = context.user_data['title']
    trim_file_id = context.user_data['trim_file']
    full_file_id = update.message.video.file_id if update.message.video else update.message.document.file_id

    # 1. Database me Full Video Save karo
    conn = db_pool.getconn()
    cur = conn.cursor()
    cur.execute("INSERT INTO adult_videos (title, full_file_id) VALUES (%s, %s) RETURNING vid_id", (title, full_file_id))
    vid_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    db_pool.putconn(conn)

    # 2. Upload to 2 Backup Channels & 1 Paid Channel
    await status.edit_text("⏳ Uploading Full Video to Backups & Paid Channel...")
    for ch_id in [BACKUP_1, BACKUP_2, PAID_CH]:
        try:
            await context.bot.send_video(chat_id=ch_id, video=full_file_id, caption=f"🔒 {title}")
        except Exception as e:
            logger.error(f"Failed to upload to {ch_id}: {e}")

    # 3. Immortal Link Generation!
    # Direct telegram link nahi banayenge, apne web server ka link banayenge
    web_link = f"{WEB_DOMAIN}/watch/{vid_id}"
    
    # 4. Shorten Link via GPLinks
    await status.edit_text("⏳ Generating GPLinks...")
    gplink = await shorten_link(web_link)

    # 5. Post to Free Channel
    await status.edit_text("⏳ Posting to Free Channel...")
    caption = (
        f"🔞 <b>{title}</b>\n\n"
        f"🔥 <b>Watch Full Video & Download:</b>\n"
        f"👉 {gplink}\n\n"
        f"💎 <b>Join VIP for Direct Files:</b> https://t.me/YOUR_VIP_LINK"
    )

    try:
        await context.bot.send_video(chat_id=FREE_CH, video=trim_file_id, caption=caption, parse_mode='HTML')
        await status.edit_text(f"✅ **BAM! ALL DONE!**\n\n- Backups Done\n- Paid Channel Done\n- Free Channel Posted with GPLink\n\n🔗 Short Link: {gplink}")
    except Exception as e:
        await status.edit_text(f"❌ Error posting to free channel: {e}")

    context.user_data.clear()
    return ConversationHandler.END

async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🛑 Cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


# ================= PROVIDER BOT: GIVING THE VIDEO =================
async def provider_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ye dusra bot hai jo user ko start karne par video dega"""
    text = update.message.text
    chat_id = update.effective_chat.id

    if "vid_" in text:
        try:
            vid_id = int(text.split("vid_")[1])
            
            conn = db_pool.getconn()
            cur = conn.cursor()
            cur.execute("SELECT title, full_file_id FROM adult_videos WHERE vid_id = %s", (vid_id,))
            result = cur.fetchone()
            cur.close()
            db_pool.putconn(conn)

            if result:
                title, file_id = result
                # User ko video bhej do, aur 60 sec baad delete kar do (Optional security)
                msg = await update.message.reply_video(video=file_id, caption=f"🎬 {title}\n\n⚠️ Please forward this video to saved messages, it will be deleted!")
                
                # Auto delete after 5 minutes
                await asyncio.sleep(300)
                try: await msg.delete()
                except: pass
            else:
                await update.message.reply_text("❌ Video not found or deleted.")
        except Exception as e:
            logger.error(f"Provider Bot Error: {e}")
    else:
        await update.message.reply_text("🔞 Welcome! Please use a valid video link.")


# ================= RUN MULTIPLE BOTS =================
async def run_bots():
    # 1. Main Admin Bot
    main_app = Application.builder().token(MAIN_BOT_TOKEN).build()
    
    upload_conv = ConversationHandler(
        entry_points=[CommandHandler('post', start_upload)],
        states={
            WAIT_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_title)],
            WAIT_TRIM: [MessageHandler(filters.VIDEO | filters.Document.ALL | filters.ANIMATION, get_trim)],
            WAIT_FULL: [MessageHandler(filters.VIDEO | filters.Document.ALL, get_full_and_process)]
        },
        fallbacks=[CommandHandler('cancel', cancel_flow)]
    )
    main_app.add_handler(upload_conv)

    # 2. Provider Bot (Client Facing)
    provider_app = Application.builder().token(PROVIDER_BOT_TOKEN).build()
    provider_app.add_handler(CommandHandler('start', provider_start))

    # Initialize and Start Both
    await main_app.initialize()
    await main_app.start()
    await main_app.updater.start_polling()

    await provider_app.initialize()
    await provider_app.start()
    await provider_app.updater.start_polling()

    logger.info("✅ Both Telegram Bots Started Successfully!")

    # Keep alive
    stop_signal = asyncio.Event()
    await stop_signal.wait()

if __name__ == '__main__':
    # Flask ko alag thread me start karo
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    # Telegram bots ko asyncio me start karo
    asyncio.run(run_bots())
