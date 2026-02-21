import os
import io
import json
import asyncio
import logging
import aiohttp
import psycopg2
from psycopg2 import pool
from flask import Flask, redirect
from threading import Thread
from telegram import Update, InputFile
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
WEB_DOMAIN = os.environ.get("WEB_DOMAIN")
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))

FREE_CH = int(os.environ.get("FREE_CHANNEL_ID", "0"))
PAID_CH = int(os.environ.get("PAID_CHANNEL_ID", "0"))
BACKUP_1 = int(os.environ.get("BACKUP_CHANNEL_1", "0"))
BACKUP_2 = int(os.environ.get("BACKUP_CHANNEL_2", "0"))

AUTO_DELETE_SECONDS = 300  # 5 minutes

# STATES
WAIT_TRIM, WAIT_FULL = range(2)

# ================= DATABASE SETUP =================
db_pool = psycopg2.pool.SimpleConnectionPool(1, 20, dsn=DATABASE_URL)


def setup_db():
    conn = db_pool.getconn()
    try:
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
    finally:
        db_pool.putconn(conn)


setup_db()


# ================= AUTO DELETE SYSTEM =================
async def auto_delete_message(message, delay=AUTO_DELETE_SECONDS):
    """Koi bhi message ko X seconds baad silently delete kar deta hai"""
    try:
        await asyncio.sleep(delay)
        await message.delete()
    except Exception:
        pass  # Agar already deleted hai ya permission nahi hai toh ignore


async def send_and_auto_delete(bot, chat_id, text, delay=AUTO_DELETE_SECONDS, **kwargs):
    """Text message bhejo aur auto-delete schedule karo"""
    try:
        msg = await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        asyncio.create_task(auto_delete_message(msg, delay))
        return msg
    except Exception as e:
        logger.error(f"Send & delete error: {e}")
        return None


async def send_video_and_auto_delete(bot, chat_id, video, caption, delay=AUTO_DELETE_SECONDS, **kwargs):
    """Video bhejo with countdown warning, phir auto-delete"""
    try:
        # 1. Pehle video bhejo
        vid_msg = await bot.send_video(
            chat_id=chat_id, video=video, caption=caption, **kwargs
        )

        # 2. Warning message bhejo
        minutes = delay // 60
        warning_msg = await bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ <b>Auto-Delete Warning!</b>\n\n"
                f"⏳ Ye video <b>{minutes} minute</b> baad automatically delete ho jayegi!\n"
                f"💾 Jaldi se <b>Saved Messages</b> mein forward kar lo!\n\n"
                f"🔒 <i>Ban protection ke liye ye zaroori hai.</i>"
            ),
            parse_mode='HTML'
        )

        # 3. 1 minute pehle final reminder
        async def countdown_and_delete():
            try:
                # Wait until 1 minute before deletion
                if delay > 60:
                    await asyncio.sleep(delay - 60)
                    reminder = await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "🔴 <b>LAST WARNING!</b>\n\n"
                            "⏳ <b>60 seconds</b> mein video delete ho jayegi!\n"
                            "💾 Abhi forward karo Saved Messages mein!"
                        ),
                        parse_mode='HTML'
                    )
                    await asyncio.sleep(60)
                    # Reminder delete
                    try:
                        await reminder.delete()
                    except Exception:
                        pass
                else:
                    await asyncio.sleep(delay)

                # Video delete
                try:
                    await vid_msg.delete()
                except Exception:
                    pass

                # Warning delete
                try:
                    await warning_msg.delete()
                except Exception:
                    pass

                # Final "deleted" notification (ye bhi 30 sec baad delete hoga)
                deleted_notice = await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "🗑️ <b>Video Deleted!</b>\n\n"
                        "🔒 Ban protection ke liye video hata di gayi.\n"
                        "🔄 Dubara dekhne ke liye link par click karo."
                    ),
                    parse_mode='HTML'
                )
                asyncio.create_task(auto_delete_message(deleted_notice, 30))

            except Exception as e:
                logger.error(f"Countdown delete error: {e}")

        asyncio.create_task(countdown_and_delete())
        return vid_msg

    except Exception as e:
        logger.error(f"Send video & delete error: {e}")
        return None


async def send_photo_and_auto_delete(bot, chat_id, photo, caption, delay=AUTO_DELETE_SECONDS, **kwargs):
    """Photo bhejo aur auto-delete schedule karo"""
    try:
        photo_msg = await bot.send_photo(
            chat_id=chat_id, photo=photo, caption=caption, **kwargs
        )
        asyncio.create_task(auto_delete_message(photo_msg, delay))
        return photo_msg
    except Exception as e:
        logger.error(f"Send photo & delete error: {e}")
        return None


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


async def download_telegram_file(bot, file_id):
    """Telegram se file download karke BytesIO return karta hai"""
    try:
        file_obj = await bot.get_file(file_id)
        byte_array = await file_obj.download_as_bytearray()
        buffer = io.BytesIO(bytes(byte_array))
        buffer.name = "thumbnail.jpg"
        buffer.seek(0)
        return buffer
    except Exception as e:
        logger.error(f"File download error: {e}")
        return None


async def download_url_to_bytes(url):
    """URL se image download karke BytesIO return karta hai"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    buffer = io.BytesIO(data)
                    buffer.name = "default_thumb.jpg"
                    buffer.seek(0)
                    return buffer
    except Exception as e:
        logger.error(f"URL download error: {e}")
    return None


# ================= WEB REDIRECTOR (FLASK) =================
app = Flask(__name__)


@app.route('/')
def home():
    return "Server is Running!"


@app.route('/watch/<int:vid_id>')
def watch_video(vid_id):
    bot_username = os.environ.get("PROVIDER_BOT_USERNAME")
    return redirect(f"tg://resolve?domain={bot_username}&start=vid_{vid_id}")


def run_flask():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)


# ================= MAIN BOT: CONVERSATION HANDLERS =================

async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ **Upload cancelled.**", parse_mode='Markdown')
    return ConversationHandler.END


async def start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END

    await update.message.reply_text(
        "⚡ **Super-Fast Post Mode!**\n\n"
        "✂️ Sabse pehle choti **TRIMMED VIDEO (Preview)** bhejo.\n\n"
        "*(Agar trim video nahi hai, toh /skip likh do — "
        "main Full video ka Thumbnail use karunga!)*",
        parse_mode='Markdown'
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
            context.user_data['title'] = "🔥 Exclusive Premium Leak 🔥"
            await msg.reply_text(
                "⏭️ **Trim Skipped!**\n\n"
                "🔞 Ab seedha **FULL HD VIDEO** bhejo.",
                parse_mode='Markdown'
            )
            return WAIT_FULL
        else:
            await msg.reply_text("❌ Kripya Trimmed Video bhejo ya /skip likho.")
            return WAIT_TRIM

    if not msg.video and not msg.document and not msg.animation:
        await msg.reply_text("❌ Ye video nahi hai. Kripya Trimmed Video bhejo ya /skip likho.")
        return WAIT_TRIM

    context.user_data['title'] = msg.caption if msg.caption else "🔥 Exclusive Premium Leak 🔥"

    if msg.video:
        context.user_data['trim_file'] = msg.video.file_id
    elif msg.animation:
        context.user_data['trim_file'] = msg.animation.file_id
    else:
        context.user_data['trim_file'] = msg.document.file_id

    context.user_data['trim_type'] = 'video'

    await msg.reply_text(
        "✅ **Trim Video Saved!**\n\n"
        "🔞 Ab **FULL HD VIDEO** bhejo.",
        parse_mode='Markdown'
    )
    return WAIT_FULL


async def get_full_and_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return WAIT_FULL

    if not msg.video and not msg.document:
        await msg.reply_text("❌ Ye Full Video nahi lag rahi. Kripya Video File bhejein.")
        return WAIT_FULL

    status = await msg.reply_text("⏳ **Processing...**", parse_mode='Markdown')

    title = context.user_data.get('title', "🔥 Exclusive Premium Leak 🔥")
    if title == "🔥 Exclusive Premium Leak 🔥" and msg.caption:
        title = msg.caption

    full_file_id = msg.video.file_id if msg.video else msg.document.file_id
    trim_type = context.user_data.get('trim_type', 'video')
    trim_file = context.user_data.get('trim_file')

    # ===== THUMBNAIL EXTRACTION =====
    thumbnail_bytes = None

    if trim_file == 'use_thumbnail':
        trim_type = 'photo'
        await status.edit_text("⏳ Extracting Thumbnail...")

        thumb_file_id = None
        if msg.video and msg.video.thumbnail:
            thumb_file_id = msg.video.thumbnail.file_id
        elif msg.document and msg.document.thumbnail:
            thumb_file_id = msg.document.thumbnail.file_id

        if thumb_file_id:
            thumbnail_bytes = await download_telegram_file(context.bot, thumb_file_id)

        if thumbnail_bytes is None:
            DEFAULT_THUMB_URL = "https://i.imgur.com/6XK4F6K.png"
            thumbnail_bytes = await download_url_to_bytes(DEFAULT_THUMB_URL)

        if thumbnail_bytes is None:
            logger.warning("Thumbnail failed, falling back to video post")
            trim_type = 'video'
            trim_file = full_file_id

    # ===== 1. DATABASE SAVE =====
    conn = db_pool.getconn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO adult_videos (title, full_file_id) VALUES (%s, %s) RETURNING vid_id",
            (title, full_file_id)
        )
        vid_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
    finally:
        db_pool.putconn(conn)

    # ===== 2. UPLOAD TO BACKUPS & PAID CHANNEL =====
    await status.edit_text("⏳ Uploading to Backups & Paid Channel...")
    channels_to_upload = [ch for ch in [BACKUP_1, BACKUP_2, PAID_CH] if ch != 0]

    for ch_id in channels_to_upload:
        try:
            await context.bot.send_video(
                chat_id=ch_id, video=full_file_id, caption=f"🔒 {title}"
            )
        except Exception as e:
            logger.error(f"Failed to upload to {ch_id}: {e}")

    # ===== 3. LINK GENERATION =====
    web_link = f"{WEB_DOMAIN}/watch/{vid_id}"

    await status.edit_text("⏳ Generating GPLinks...")
    gplink = await shorten_link(web_link)

    # ===== 4. POST TO FREE CHANNEL =====
    await status.edit_text("⏳ Posting to Free Channel...")
    caption = (
        f"🔞 <b>{title}</b>\n\n"
        f"🔥 <b>Watch Full Video & Download:</b>\n"
        f"👉 {gplink}\n\n"
        f"💎 <b>Join VIP for Direct Files:</b> https://t.me/YOUR_VIP_LINK"
    )

    try:
        if FREE_CH != 0:
            if trim_type == 'photo' and thumbnail_bytes is not None:
                thumbnail_bytes.seek(0)
                await context.bot.send_photo(
                    chat_id=FREE_CH,
                    photo=InputFile(thumbnail_bytes, filename="preview.jpg"),
                    caption=caption,
                    parse_mode='HTML'
                )
            else:
                await context.bot.send_video(
                    chat_id=FREE_CH,
                    video=trim_file,
                    caption=caption,
                    parse_mode='HTML'
                )

            await status.edit_text(
                f"✅ **ALL DONE!**\n\n🎬 Title: {title}\n🔗 Link: {gplink}",
                parse_mode='Markdown'
            )
        else:
            await status.edit_text(
                f"✅ **Done!** (Free Channel ID set nahi hai)\n🔗 Link: {gplink}",
                parse_mode='Markdown'
            )
    except Exception as e:
        logger.error(f"Free channel post error: {e}")
        await status.edit_text(f"❌ Error posting to free channel: {e}")

    context.user_data.clear()
    return ConversationHandler.END


# ================= PROVIDER BOT (WITH AUTO-DELETE) =================
async def provider_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id
    if not text:
        return

    if "vid_" in text:
        try:
            vid_id = int(text.split("vid_")[1])

            conn = db_pool.getconn()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT title, full_file_id FROM adult_videos WHERE vid_id = %s",
                    (vid_id,)
                )
                result = cur.fetchone()
                cur.close()
            finally:
                db_pool.putconn(conn)

            if result:
                title, file_id = result

                # ✅ Auto-delete video with countdown warnings
                await send_video_and_auto_delete(
                    bot=context.bot,
                    chat_id=chat_id,
                    video=file_id,
                    caption=(
                        f"🎬 <b>{title}</b>\n\n"
                        f"⏳ <b>Ye video {AUTO_DELETE_SECONDS // 60} min baad auto-delete ho jayegi!</b>\n"
                        f"💾 Jaldi forward karo Saved Messages mein!\n\n"
                        f"🔒 <i>Ban protection active hai</i>"
                    ),
                    delay=AUTO_DELETE_SECONDS,
                    parse_mode='HTML'
                )

                # User ka /start message bhi delete karo (clean chat)
                asyncio.create_task(auto_delete_message(update.message, 5))

            else:
                no_vid = await update.message.reply_text("❌ Video not found or deleted.")
                asyncio.create_task(auto_delete_message(no_vid, 30))
                asyncio.create_task(auto_delete_message(update.message, 5))

        except ValueError:
            err = await update.message.reply_text("❌ Invalid video ID.")
            asyncio.create_task(auto_delete_message(err, 30))
        except Exception as e:
            logger.error(f"Provider Bot Error: {e}")
            err = await update.message.reply_text("❌ Something went wrong.")
            asyncio.create_task(auto_delete_message(err, 30))
    else:
        welcome = await update.message.reply_text(
            "🔞 <b>Welcome!</b>\n\n"
            "📎 Please use a valid video link to watch.\n"
            "🔒 All messages auto-delete for safety.",
            parse_mode='HTML'
        )
        asyncio.create_task(auto_delete_message(welcome, 60))
        asyncio.create_task(auto_delete_message(update.message, 5))


# ================= RUN BOTH BOTS =================
async def run_bots():
    # Main Admin Bot
    main_app = Application.builder().token(MAIN_BOT_TOKEN).build()

    upload_conv = ConversationHandler(
        entry_points=[CommandHandler('post', start_upload)],
        states={
            WAIT_TRIM: [
                CommandHandler('skip', get_trim),
                MessageHandler(
                    filters.VIDEO | filters.Document.ALL | filters.ANIMATION | filters.TEXT,
                    get_trim
                )
            ],
            WAIT_FULL: [
                MessageHandler(
                    filters.VIDEO | filters.Document.ALL,
                    get_full_and_process
                )
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel_flow)]
    )
    main_app.add_handler(upload_conv)

    # Provider Bot
    provider_app = Application.builder().token(PROVIDER_BOT_TOKEN).build()
    provider_app.add_handler(CommandHandler('start', provider_start))

    await main_app.initialize()
    await main_app.start()
    await main_app.updater.start_polling()

    await provider_app.initialize()
    await provider_app.start()
    await provider_app.updater.start_polling()

    logger.info("✅ Both Telegram Bots Started Successfully!")

    stop_signal = asyncio.Event()
    await stop_signal.wait()


if __name__ == '__main__':
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    asyncio.run(run_bots())
