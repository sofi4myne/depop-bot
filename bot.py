"""
Depop Sales Manager Telegram Bot
=================================
Requirements:
    pip install "python-telegram-bot==20.7" beautifulsoup4
"""

import asyncio
import imaplib
import email
import logging
import os
import re
import sqlite3
import threading
import time
from email.header import decode_header

from bs4 import BeautifulSoup
from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# CONFIG — values are read from environment variables (set in Railway)
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN   = os.environ.get("8795006744:AAGoRpnKT2qEI5tVMKHxFj9MaenzvcyjUHM")
TELEGRAM_CHAT_ID     = int(os.environ.get("6821254642", 0))

GMAIL_ADDRESS        = os.environ.get("cut0ffy0urh4nds@gmail.com")
GMAIL_APP_PASSWORD   = os.environ.get("eekyk zsfp wyqu ivfe")

# Second email is optional — leave blank in Railway if not needed
GMAIL_ADDRESS_2      = os.environ.get("bingusboop@gmail.com")
GMAIL_APP_PASSWORD_2 = os.environ.get("musk vccc djrv dqqh")

EMAIL_CHECK_INTERVAL = 60
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = "sales.db"

BTN_PENDING = "📦 Check Pending Sales"
BTN_SHIPPED = "✅ View Shipped Sales"


def make_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(BTN_PENDING)],
            [KeyboardButton(BTN_SHIPPED)],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True,
        input_field_placeholder="Choose an option...",
    )


# ===========================================================================
# DATABASE
# ===========================================================================

def db_init() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sales (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT    NOT NULL,
                status  TEXT    NOT NULL DEFAULT 'pending'
            )
        """)
        conn.commit()
    logger.info("Database ready: %s", DB_PATH)


def db_insert_sale(subject: str) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO sales (subject, status) VALUES (?, 'pending')", (subject,)
        )
        conn.commit()
        return cur.lastrowid


def db_get_pending():
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT id, subject FROM sales WHERE status = 'pending' ORDER BY id"
        ).fetchall()


def db_get_shipped():
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT id, subject FROM sales WHERE status = 'shipped' "
            "ORDER BY id DESC LIMIT 5"
        ).fetchall()


def db_mark_shipped(sale_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE sales SET status = 'shipped' WHERE id = ?", (sale_id,)
        )
        conn.commit()


# ===========================================================================
# EMAIL / IMAP
# ===========================================================================

def extract_label_url(html_body: str):
    soup = BeautifulSoup(html_body, "html.parser")
    for tag in soup.find_all("a", href=True):
        if "shipping/label/" in tag["href"]:
            return tag["href"]
    match = re.search(r'https?://[^\s"<>]+shipping/label/[^\s"<>]+', html_body)
    return match.group(0) if match else None


def get_html_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
    elif msg.get_content_type() == "text/html":
        charset = msg.get_content_charset() or "utf-8"
        return msg.get_payload(decode=True).decode(charset, errors="replace")
    return None


def decode_subject(raw: str) -> str:
    parts = decode_header(raw)
    out = []
    for part, enc in parts:
        out.append(
            part.decode(enc or "utf-8", errors="replace")
            if isinstance(part, bytes) else part
        )
    return "".join(out)


def fetch_new_sales(bot: Bot, gmail_address: str, gmail_app_password: str) -> None:
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(gmail_address, gmail_app_password)
        mail.select("inbox")

        status, data = mail.search(
            None,
            '(UNSEEN SUBJECT "Your USPS shipping label and sale confirmation for")',
        )
        if status != "OK":
            logger.warning("IMAP search failed: %s", status)
            mail.logout()
            return

        email_ids = data[0].split()
        if not email_ids:
            logger.info("No new sale emails for %s.", gmail_address)
            mail.logout()
            return

        logger.info("Found %d new sale email(s) for %s.", len(email_ids), gmail_address)

        for eid in email_ids:
            _, msg_data = mail.fetch(eid, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            subject = decode_subject(msg.get("Subject", "Unknown Item"))

            html_body = get_html_body(msg)
            label_url = extract_label_url(html_body) if html_body else None

            sale_id = db_insert_sale(subject)
            logger.info("Saved sale id=%d: %s", sale_id, subject)

            text = (
                "💰 <b>YO YOU JUST MADE A SALE GANG!</b>\n\n"
                f"📦 <b>Item:</b> {_esc(subject)}\n\n"
                "✅ Added to your pending list, go get that bread!"
            )
            buttons = []
            if label_url:
                buttons.append(
                    [InlineKeyboardButton("🖨️ Open Shipping Label", url=label_url)]
                )

            asyncio.run(
                bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
                )
            )
            mail.store(eid, "+FLAGS", "\\Seen")

        mail.logout()

    except imaplib.IMAP4.error as exc:
        logger.error("IMAP error for %s: %s", gmail_address, exc)
    except Exception as exc:
        logger.exception("Unexpected error for %s: %s", gmail_address, exc)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def email_polling_loop(bot: Bot) -> None:
    logger.info("Email polling started (every %ds).", EMAIL_CHECK_INTERVAL)
    while True:
        # Always check the first email
        fetch_new_sales(bot, GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        # Check second email only if it's configured
        if GMAIL_ADDRESS_2 and GMAIL_APP_PASSWORD_2:
            fetch_new_sales(bot, GMAIL_ADDRESS_2, GMAIL_APP_PASSWORD_2)
        time.sleep(EMAIL_CHECK_INTERVAL)


# ===========================================================================
# TELEGRAM HANDLERS
# ===========================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Loading...",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text(
        "❓ <b>Aye here's the rundown gang:</b>\n\n"
        "📦 <b>Check Pending Sales</b> — Shows everything you gotta ship. "
        "Hit <i>Mark as Shipped</i> when you drop it off 🚚\n\n"
        "✅ <b>View Shipped Sales</b> — Yo last 5 shipped orders 💰\n\n"
        "🔔 Every time someone buys yo shi on Depop, Imma hit you with a "
        "notification and a link to send the label to fortune 🎯",
        parse_mode=ParseMode.HTML,
        reply_markup=make_menu(),
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Loading...",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text(
        "🔥 Here's ya menu gang:",
        reply_markup=make_menu(),
    )


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text

    if text == BTN_PENDING:
        await show_pending(update, context)
    elif text == BTN_SHIPPED:
        await show_shipped(update, context)
    else:
        await update.message.reply_text(
            "Use the buttons below gang, or send /menu to bring em back 👇",
            reply_markup=make_menu(),
        )


async def show_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db_get_pending()

    if not rows:
        await update.message.reply_text(
            "✅ No pending sales rn, go get some more listings up gang! 📈",
            reply_markup=make_menu(),
        )
        return

    count = len(rows)
    await update.message.reply_text(
        f"📦 <b>Pending Sales ({count} item{'s' if count != 1 else ''}) — time to ship gang! 🚀</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=make_menu(),
    )
    for sale_id, subject in rows:
        await update.message.reply_text(
            f"📦 {_esc(subject)}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🚚 Mark as Shipped", callback_data=f"ship:{sale_id}")
            ]]),
        )


async def show_shipped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db_get_shipped()

    if not rows:
        await update.message.reply_text(
            "Ain't shipped nothing yet gang, get to work! 😤",
            reply_markup=make_menu(),
        )
        return

    lines = "\n".join(f"✅ {_esc(s)}" for _, s in rows)
    await update.message.reply_text(
        f"💨 <b>Recently Shipped (last {len(rows)}) — facts you been busy! 💰</b>\n\n{lines}",
        parse_mode=ParseMode.HTML,
        reply_markup=make_menu(),
    )


async def callback_mark_shipped(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    sale_id = int(query.data.split(":")[1])
    db_mark_shipped(sale_id)
    logger.info("Sale id=%d marked as shipped.", sale_id)
    await query.edit_message_text("✅ Shipped! Go count that bread gang 💰")


# ===========================================================================
# STARTUP HOOK
# ===========================================================================

async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand("start", "Open the sales manager menu"),
        BotCommand("menu",  "Bring back the menu buttons"),
    ])
    logger.info("Bot commands registered.")


# ===========================================================================
# MAIN
# ===========================================================================

def main() -> None:
    db_init()

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu",  cmd_menu))
    app.add_handler(CallbackQueryHandler(callback_mark_shipped, pattern=r"^ship:\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))

    threading.Thread(
        target=email_polling_loop, args=(app.bot,), daemon=True
    ).start()

    logger.info("Bot running. Send /start in Telegram.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()