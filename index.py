import os
import io
import asyncio
import zipfile
import platform
import gc
from urllib.parse import urlparse, urljoin

import httpx
from bs4 import BeautifulSoup

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = '8538798053:AAERroO0Rhh9_g6ZxKwS1V_uAl0lUlfJLLo'  # <=== put your bot token 
REQUIRED_CHANNEL = -1002613561003  # your channel ID
ADMIN_USER_ID = 7998441787  # your Telegram ID

MAX_CONCURRENT = 5  # safe for low RAM
ZIP_PART_SIZE = 1 * 1024 * 1024  # 1MB per zip chunk

known_users = set()
tasks = {}

ALLOWED_SCHEMES = ('http', 'https')


def is_valid_url(url):
    try:
        p = urlparse(url)
        return p.scheme in ALLOWED_SCHEMES and p.netloc
    except:
        return False


async def is_user_in_channel(bot, user_id):
    try:
        m = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return m.status in ['member', 'administrator', 'creator']
    except:
        return False


def get_stats_text():
    mem = p.memory_info().rss / (1024 * 1024)
    cpu = p.cpu_percent()
    sys = platform.uname()
    text = (
        f"📊 **Bot Stats**\n"
        f"👥 Known Users: {len(known_users)}\n"
        f"🔄 Active Tasks: {len(tasks)}\n"
        f"🧠 RAM Usage: {mem:.2f} MB\n"
        f"⚙️ CPU Usage: {cpu:.1f}%\n"
        f"💻 System: {sys.system} {sys.release} ({sys.machine})\n"
    )
    return text


async def scrape_and_send(base_url, chat_id, bot):
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
        queue = [base_url]
        seen_urls = set()
        seen_paths = set()
        file_count = 0
        part_num = 1

        zip_buffer = io.BytesIO()
        zip_file = zipfile.ZipFile(zip_buffer, 'w', compression=zipfile.ZIP_DEFLATED)

        while queue and not tasks[chat_id]['cancelled']:
            url = queue.pop(0)
            if url in seen_urls:
                continue
            seen_urls.add(url)

            async with sem:
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                except Exception:
                    continue

                parsed = urlparse(url)
                path = parsed.path.lstrip('/')
                if not path or path.endswith('/'):
                    path += 'index.html'

                path = path.split('?')[0]

                if path in seen_paths:
                    continue
                seen_paths.add(path)

                zip_file.writestr(path, resp.content)
                file_count += 1

                if file_count % 5 == 0:
                    await bot.send_message(chat_id, f"✅ Downloaded {file_count} files so far...")

                if zip_buffer.tell() >= ZIP_PART_SIZE:
                    zip_file.close()
                    zip_buffer.seek(0)
                    await bot.send_document(chat_id, InputFile(zip_buffer, filename=f'website_part_{part_num}.zip'))

                    zip_buffer.close()
                    del zip_file, zip_buffer
                    gc.collect()

                    part_num += 1
                    zip_buffer = io.BytesIO()
                    zip_file = zipfile.ZipFile(zip_buffer, 'w', compression=zipfile.ZIP_DEFLATED)

                if 'text/html' in resp.headers.get('Content-Type', ''):
                    try:
                        soup = BeautifulSoup(resp.text, 'html.parser')
                        for tag in soup.find_all(['a', 'link', 'script', 'img', 'source', 'video', 'audio', 'iframe']):
                            attr = tag.get('href') or tag.get('src')
                            if attr:
                                new_url = urljoin(url, attr)
                                p_new = urlparse(new_url)
                                if p_new.scheme in ALLOWED_SCHEMES and new_url.startswith(base_url):
                                    queue.append(new_url)
                    except Exception:
                        continue

        zip_file.close()
        if zip_buffer.tell() > 0:
            zip_buffer.seek(0)
            await bot.send_document(chat_id, InputFile(zip_buffer, filename=f'website_part_{part_num}.zip'))
            zip_buffer.close()
            del zip_file, zip_buffer
            gc.collect()

    if tasks[chat_id]['cancelled']:
        await bot.send_message(chat_id, "❌ Scraping cancelled.")
    else:
        await bot.send_message(chat_id, f"🎉 Done! Total files: {file_count}")

    tasks.pop(chat_id, None)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Send a website URL — I’ll scrape ALL files & folders exactly and send in zip chunks!"
    )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    if not await is_user_in_channel(ctx.bot, user.id):
        return await update.message.reply_text("🔒 Please join our channel first to use this bot. \n https://t.me/Synaxnetwork")

    if user.id not in known_users:
        await ctx.bot.send_message(ADMIN_USER_ID, f"New user: {user.full_name} (ID: {user.id})")
        known_users.add(user.id)

    if not is_valid_url(text):
        return await update.message.reply_text("❌ Invalid URL http:// or https:// must be in URL.")

    if chat_id in tasks:
        return await update.message.reply_text("⏳ Already scraping for you. Use Cancel if needed.")

    tasks[chat_id] = {'cancelled': False}
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data='cancel')]])
    await update.message.reply_text(f"🔍 Scraping: {text}", reply_markup=kb)

    ctx.application.create_task(scrape_and_send(text, chat_id, ctx.bot))


async def cancel_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat.id
    await query.answer()
    if chat_id in tasks:
        tasks[chat_id]['cancelled'] = True
        await query.edit_message_text("🛑 Cancelled by user.")
    else:
        await query.edit_message_text("⚠️ No active scraping.")


async def broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    if not update.message.reply_to_message:
        return await update.message.reply_text("Reply to a message to broadcast.")
    sent, failed = 0, 0
    for uid in known_users:
        try:
            await update.message.reply_to_message.forward(uid)
            sent += 1
        except:
            failed += 1
    await update.message.reply_text(f"✅ Sent: {sent}, ❌ Failed: {failed}")


async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = get_stats_text()
    await update.message.reply_text(text, parse_mode='Markdown')


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(cancel_cb, pattern="cancel"))

    print("✅ Bot running (low RAM mode, advanced stats enabled)")
    app.run_polling()


if __name__ == "__main__":
    main()