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

# STATES FOR UPLOAD FLOW
WAIT_TRIM, WAIT_FULL = range(2)

# ================= MAIN BOT: UPLOAD FLOW (SUPER FAST) =================
async def start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: Admin /post command deta hai"""
    if update.effective_user.id != ADMIN_USER_ID: return
    
    await update.message.reply_text(
        "⚡ **Super-Fast Post Mode!**\n\n"
        "✂️ Sabse pehle choti **TRIMMED VIDEO (Preview)** bhejo ya forward karo.\n\n"
        "*(Tip: Agar aap trim video ke caption mein naam likhoge, toh main usko Title bana lunga!)*", 
        parse_mode='Markdown'
    )
    return WAIT_TRIM

async def get_trim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2: Admin Trim video bhejta hai"""
    msg = update.message
    if not msg.video and not msg.document and not msg.animation:
        await msg.reply_text("❌ Error: Ye video nahi hai. Kripya Trimmed Video bhejo.")
        return WAIT_TRIM
    
    # Title ko caption se nikalna (Agar caption nahi hai, to default naam lagayega)
    context.user_data['title'] = msg.caption if msg.caption else "🔥 Exclusive Premium Leak 🔥"
    
    # File ID save karna
    context.user_data['trim_file'] = msg.video.file_id if msg.video else (msg.animation.file_id if msg.animation else msg.document.file_id)
    
    await msg.reply_text(
        "✅ **Trim Video Saved!**\n\n"
        "🔞 Ab **FULL HD VIDEO** bhejo.\n"
        "*(Jaise hi aap full video bhejoge, main auto-process karke channel par daal dunga!)*",
        parse_mode='Markdown'
    )
    return WAIT_FULL

async def get_full_and_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 3: Admin Full video bhejta hai aur Bot sab auto-process kar deta hai"""
    msg = update.message
    if not msg.video and not msg.document:
        await msg.reply_text("❌ Error: Full Video bhejo.")
        return WAIT_FULL

    status = await msg.reply_text("⏳ **Processing Started... (Koi aur command mat dena)**")
    
    # Agar Trim me caption nahi tha, par Full video me hai, to usko Title bana lo
    title = context.user_data.get('title')
    if title == "🔥 Exclusive Premium Leak 🔥" and msg.caption:
        title = msg.caption

    trim_file_id = context.user_data['trim_file']
    full_file_id = msg.video.file_id if msg.video else msg.document.file_id

    # 1. Database me Full Video Save karo
    conn = db_pool.getconn()
    cur = conn.cursor()
    cur.execute("INSERT INTO adult_videos (title, full_file_id) VALUES (%s, %s) RETURNING vid_id", (title, full_file_id))
    vid_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    db_pool.putconn(conn)

    # 2. Upload to Backups & Paid Channel
    await status.edit_text("⏳ Uploading Full Video to Backups & Paid Channel...")
    
    # Yahan apne channel IDs check kar lena
    channels_to_upload = [ch for ch in [BACKUP_1, BACKUP_2, PAID_CH] if ch != 0]
    
    for ch_id in channels_to_upload:
        try:
            await context.bot.send_video(chat_id=ch_id, video=full_file_id, caption=f"🔒 {title}")
        except Exception as e:
            logger.error(f"Failed to upload to {ch_id}: {e}")

    # 3. Immortal Link Generation (Web Redirector)
    web_link = f"{WEB_DOMAIN}/watch/{vid_id}"
    
    # 4. Shorten Link via GPLinks
    await status.edit_text("⏳ Generating GPLinks...")
    gplink = await shorten_link(web_link)

    # 5. Post to Free Channel
    await status.edit_text("⏳ Posting to Free Channel...")
    
    # Style kiya hua caption
    caption = (
        f"🔞 <b>{title}</b>\n\n"
        f"🔥 <b>Watch Full Video & Download:</b>\n"
        f"👉 {gplink}\n\n"
        f"💎 <b>Join VIP for Direct Files:</b> https://t.me/YOUR_VIP_LINK"
    )

    try:
        if FREE_CH != 0:
            await context.bot.send_video(chat_id=FREE_CH, video=trim_file_id, caption=caption, parse_mode='HTML')
            await status.edit_text(f"✅ **BAM! ALL DONE!**\n\n🎬 Title: {title}\n🔗 Short Link: {gplink}")
        else:
            await status.edit_text(f"✅ **Done!** (Free Channel ID .env me set nahi hai)\n🔗 Link: {gplink}")
    except Exception as e:
        await status.edit_text(f"❌ Error posting to free channel: {e}")

    # Memory saaf karo taaki agle post ke liye ready rahe
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🛑 Post Cancelled.")
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
