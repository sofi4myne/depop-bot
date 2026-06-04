"""
Depop Shipping Tracker Bot
===========================
Requirements:
    pip install "python-telegram-bot==20.7" pyzbar pillow pdf2image requests

System dependencies (for barcode scanning):
    Mac:     brew install zbar poppler
    Railway: add to nixpacks.toml (see below)

nixpacks.toml contents for Railway:
    [phases.setup]
    nixPkgs = ["zbar", "poppler_utils"]
"""

import io
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time

import requests
from PIL import Image
from pdf2image import convert_from_bytes
import pytesseract
from telegram import (
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
# CONFIG
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = "8795006744:AAGPD0YkwckE7hNrtF13ZdlzUFwCybOUnTs"
TELEGRAM_CHAT_ID   = 6821254642

TRACKING_CHECK_INTERVAL = 3600  # check every hour
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = "tracker.db"

BTN_GRANNY  = "granny"
BTN_VELI0R  = "veli0r"
BTN_GRANNY_LIST = "📦 granny's packages"
BTN_VELI0R_LIST = "📦 veli0r's packages"


def make_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(BTN_GRANNY_LIST)],
            [KeyboardButton(BTN_VELI0R_LIST)],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True,
        input_field_placeholder="Send a label or pick a list...",
    )


# ===========================================================================
# DATABASE
# ===========================================================================

def db_init() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS packages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                tracking_number TEXT    NOT NULL UNIQUE,
                category        TEXT    NOT NULL,
                recipient       TEXT    NOT NULL DEFAULT '',
                amount          TEXT    NOT NULL DEFAULT '',
                status          TEXT    NOT NULL DEFAULT 'pending',
                last_event      TEXT    NOT NULL DEFAULT '',
                usps_scanned    INTEGER NOT NULL DEFAULT 0,
                delivered       INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()
    logger.info("Database ready: %s", DB_PATH)


def db_add_package(tracking: str, category: str, recipient: str = "", amount: str = "") -> bool:
    """Returns True if inserted, False if tracking number already exists."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO packages (tracking_number, category, recipient, amount) VALUES (?, ?, ?, ?)",
                (tracking, category, recipient, amount),
            )
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def db_get_active(category: str = None):
    with sqlite3.connect(DB_PATH) as conn:
        if category:
            return conn.execute(
                "SELECT id, tracking_number, status, last_event, recipient, amount FROM packages "
                "WHERE delivered = 0 AND category = ? ORDER BY id DESC",
                (category,),
            ).fetchall()
        return conn.execute(
            "SELECT id, tracking_number, category, status, last_event, recipient, amount "
            "FROM packages WHERE delivered = 0 ORDER BY id DESC"
        ).fetchall()


def db_get_all_active():
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT id, tracking_number, category, status, last_event, "
            "usps_scanned, delivered FROM packages WHERE delivered = 0"
        ).fetchall()


def db_update_package(tracking: str, status: str, last_event: str,
                      usps_scanned: int, delivered: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE packages SET status=?, last_event=?, usps_scanned=?, "
            "delivered=? WHERE tracking_number=?",
            (status, last_event, usps_scanned, delivered, tracking),
        )
        conn.commit()


def db_remove_package(pkg_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM packages WHERE id = ?", (pkg_id,))
        conn.commit()


def db_get_by_id(pkg_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT id, tracking_number, category, status, last_event "
            "FROM packages WHERE id = ?",
            (pkg_id,),
        ).fetchone()


# ===========================================================================
# BARCODE SCANNING
# ===========================================================================

def is_usps_tracking(value: str) -> bool:
    clean = re.sub(r"\s", "", value)
    return bool(re.match(r"^\d{20,22}$", clean))


def extract_recipient_name(text: str) -> str | None:
    """
    Pull the recipient name from OCR text.
    Labels have: sender name/address block, then recipient name/address block.
    We skip the first all-caps name (sender) and return the second one.
    """
    skip_words = {"USPS", "GROUND", "ADVANTAGE", "CUBIC", "TRACKING",
                  "SHIP", "RDC", "PAID", "POSTAGE", "DATE", "WEIGHT"}
    caps_names = []
    for line in text.split("\n"):
        # Strip garbled OCR prefix chars (e.g. "aegea HUMZA" → "HUMZA")
        clean = re.sub(r'^[^A-Z]+', '', line.strip()).strip()
        if (re.match(r'^[A-Z][A-Z\s]{3,}$', clean)
                and not any(w in clean for w in skip_words)
                and len(clean.split()) >= 2):
            caps_names.append(clean.title())
    # Index 0 = sender, index 1 = recipient
    if len(caps_names) >= 2:
        return caps_names[1]
    elif len(caps_names) == 1:
        return caps_names[0]
    return None


def scan_label_image(image) -> tuple:
    """OCR the image and return (tracking_number, recipient_name)."""
    try:
        text = pytesseract.image_to_string(image)
        # Find tracking number
        tracking = None
        for match in re.findall(r'\d[\d\s]{18,25}\d', text):
            clean = re.sub(r"\s", "", match)
            if is_usps_tracking(clean):
                tracking = clean
                break
        name = extract_recipient_name(text)
        return tracking, name
    except Exception as exc:
        logger.error("OCR error: %s", exc)
        return None, None


def extract_tracking_from_file(file_bytes: bytes, is_pdf: bool) -> tuple:
    """Extract (tracking_number, recipient_name) from PNG/JPG or PDF bytes."""
    try:
        if is_pdf:
            pages = convert_from_bytes(file_bytes, dpi=400)
            for page in pages:
                tracking, name = scan_label_image(page)
                if tracking:
                    return tracking, name
        else:
            image = Image.open(io.BytesIO(file_bytes))
            return scan_label_image(image)
    except Exception as exc:
        logger.exception("Error scanning label: %s", exc)
    return None, None


# ===========================================================================
# USPS TRACKING (scraping — no API key needed)
# ===========================================================================

USPS_TRACK_URL = "https://tools.usps.com/go/TrackConfirmAction"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def get_tracking_status(tracking_number: str) -> dict:
    """
    Scrape USPS tracking page and return:
    {
        "status": str,       human-readable latest status
        "delivered": bool,
        "usps_scanned": bool,  True once USPS has touched it
        "raw": str            full latest event line
    }
    """
    try:
        resp = requests.get(
            USPS_TRACK_URL,
            params={"tLabels": tracking_number},
            headers=HEADERS,
            timeout=15,
        )
        html = resp.text

        # Pull the primary status text
        status_match = re.search(
            r'class="tb-status[^"]*"[^>]*>\s*<p[^>]*>\s*([^<]+)',
            html,
        )
        status = status_match.group(1).strip() if status_match else ""

        # Pull latest event detail
        event_match = re.search(
            r'class="tb-step".*?<p[^>]*class="[^"]*tb-date[^"]*"[^>]*>([^<]+)',
            html, re.DOTALL,
        )
        event = event_match.group(1).strip() if event_match else ""

        delivered = bool(re.search(r"delivered", html, re.IGNORECASE) and
                         re.search(r"class=\"tb-status", html))
        usps_scanned = bool(status) and "pre-shipment" not in status.lower()

        return {
            "status": status or "No update yet",
            "delivered": delivered,
            "usps_scanned": usps_scanned,
            "raw": f"{status} — {event}".strip(" —"),
        }

    except Exception as exc:
        logger.error("Tracking fetch error for %s: %s", tracking_number, exc)
        return {
            "status": "Could not fetch status",
            "delivered": False,
            "usps_scanned": False,
            "raw": "Could not fetch status",
        }


# ===========================================================================
# BACKGROUND TRACKING LOOP
# ===========================================================================

def tracking_loop(bot) -> None:
    import asyncio
    logger.info("Tracking loop started (every %ds).", TRACKING_CHECK_INTERVAL)
    while True:
        time.sleep(TRACKING_CHECK_INTERVAL)
        rows = db_get_all_active()
        for pkg_id, tracking, category, status, last_event, was_scanned, was_delivered in rows:
            info = get_tracking_status(tracking)
            new_event = info["raw"]
            now_scanned = int(info["usps_scanned"])
            now_delivered = int(info["delivered"])

            # Notify on first USPS scan
            if now_scanned and not was_scanned:
                asyncio.run(bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=f"Aye USPS scanned yo package 🔔\n\n"
                         f"<b>Tracking:</b> <code>{tracking}</code>\n"
                         f"<b>Account:</b> {category}\n"
                         f"<b>Status:</b> {info['status']}",
                    parse_mode=ParseMode.HTML,
                ))

            # Notify on delivery
            if now_delivered and not was_delivered:
                asyncio.run(bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=f"IT'S THERE GANG 📦🔥\n\n"
                         f"<b>Tracking:</b> <code>{tracking}</code>\n"
                         f"<b>Account:</b> {category}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "Remove from list 🗑️",
                            callback_data=f"remove:{pkg_id}"
                        )
                    ]]),
                ))

            db_update_package(tracking, info["status"], new_event, now_scanned, now_delivered)
            logger.info("Updated %s: %s", tracking, info["status"])


# ===========================================================================
# TELEGRAM HANDLERS
# ===========================================================================

async def cmd_start(update, context) -> None:
    msg = await update.message.reply_text("Loading...", reply_markup=ReplyKeyboardRemove())
    await msg.edit_text(
        "📦 <b>Depop Shipping Tracker</b>\n\n"
        "Send me a shipping label (PNG or PDF) and I'll scan it, "
        "track it automatically, and hit you when USPS picks it up "
        "and when it's delivered 🔔\n\n"
        "Use the buttons below to view your packages by account:",
        parse_mode=ParseMode.HTML,
        reply_markup=make_menu(),
    )


async def cmd_menu(update, context) -> None:
    msg = await update.message.reply_text("Loading...", reply_markup=ReplyKeyboardRemove())
    await msg.edit_text("Here's ya menu:", reply_markup=make_menu())


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo messages — treat as shipping label."""
    msg = await update.message.reply_text("Hold up lemme scan this real quick 👀")
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()
    tracking, recipient = extract_tracking_from_file(bytes(file_bytes), is_pdf=False)
    await _after_scan(msg, context, tracking, recipient)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle document messages (PDF or image file)."""
    doc = update.message.document
    mime = doc.mime_type or ""

    if "pdf" not in mime and "image" not in mime:
        await update.message.reply_text(
            "Send the label as a PNG image or PDF file gang 📄",
            reply_markup=make_menu(),
        )
        return

    msg = await update.message.reply_text("Hold up lemme scan this real quick 👀")
    file = await context.bot.get_file(doc.file_id)
    file_bytes = await file.download_as_bytearray()
    is_pdf = "pdf" in mime
    tracking, recipient = extract_tracking_from_file(bytes(file_bytes), is_pdf=is_pdf)
    await _after_scan(msg, context, tracking, recipient)


async def _after_scan(msg, context, tracking: str | None, recipient: str | None) -> None:
    """Edit the scanning message with the result — stays in same bubble."""
    if not tracking:
        await msg.edit_text(
            "That label tweakin, try again 😂\n\n"
            "Make sure the label is clear and not cut off.",
        )
        return

    # Store in context for the next steps
    context.user_data["pending_tracking"] = tracking
    context.user_data["pending_recipient"] = recipient or "Unknown"

    name_line = f"\n<b>To:</b> {_esc(recipient)}" if recipient else ""
    await msg.edit_text(
        f"Found dat ho ✅\n\n"
        f"<b>Tracking:</b> <code>{tracking}</code>{name_line}\n\n"
        f"How much u finna get? 💰\n"
        f"<i>Reply with the amount (e.g. 25 or 25.99)</i>",
        parse_mode=ParseMode.HTML,
    )
    context.user_data["awaiting_amount"] = True


async def handle_menu_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text

    # Handle sale amount input
    if context.user_data.get("awaiting_amount"):
        amount = text.strip().replace("$", "").replace(",", "")
        # Validate it looks like a number
        try:
            float(amount)
        except ValueError:
            await update.message.reply_text(
                "Just send the amount as a number gang, like 25 or 25.99 💰"
            )
            return

        context.user_data["pending_amount"] = amount
        context.user_data["awaiting_amount"] = False

        tracking = context.user_data.get("pending_tracking")
        recipient = context.user_data.get("pending_recipient", "")

        await update.message.reply_text(
            f"💰 <b>${amount}</b> locked in!\n\n"
            f"Which account?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("granny", callback_data=f"save:granny:{tracking}"),
                InlineKeyboardButton("veli0r", callback_data=f"save:veli0r:{tracking}"),
            ]]),
        )
        return

    if text == BTN_GRANNY_LIST:
        await show_packages(update, context, "granny")
    elif text == BTN_VELI0R_LIST:
        await show_packages(update, context, "veli0r")
    else:
        await update.message.reply_text(
            "Send me a shipping label (PNG or PDF) to track it 📦",
            reply_markup=make_menu(),
        )


async def show_packages(update: Update, context, category: str) -> None:
    rows = db_get_active(category)
    if not rows:
        await update.message.reply_text(
            f"No active packages under <b>{category}</b> rn 👀",
            parse_mode=ParseMode.HTML,
            reply_markup=make_menu(),
        )
        return

    lines = []
    buttons = []
    for pkg_id, tracking, status, last_event, recipient, amount in rows:
        display_status = last_event if last_event else status
        line = f"<code>{tracking}</code>"
        if recipient:
            line += f"\n👤 {_esc(recipient)}"
        if amount:
            line += f"  💰 ${_esc(amount)}"
        line += f"\n📍 {_esc(display_status)}"
        lines.append(line)
        buttons.append([
            InlineKeyboardButton("Where dat pack? 🗺️", callback_data=f"track:{pkg_id}"),
            InlineKeyboardButton("Remove from list 🗑️", callback_data=f"remove:{pkg_id}"),
        ])

    await update.message.reply_text(
        f"📦 <b>{category}'s packages ({len(rows)}):</b>\n\n" + "\n\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    # ── Save package to a category ────────────────────────────────────────
    if data.startswith("save:"):
        _, category, tracking = data.split(":", 2)
        recipient = context.user_data.get("pending_recipient", "")
        amount = context.user_data.get("pending_amount", "")
        added = db_add_package(tracking, category, recipient, amount)
        if added:
            recipient_line = f"\n👤 <b>To:</b> {_esc(recipient)}" if recipient else ""
            amount_line = f"\n💰 <b>Sale:</b> ${_esc(amount)}" if amount else ""
            await query.edit_message_text(
                f"✅ Saved under <b>{category}</b>!{recipient_line}{amount_line}\n\n"
                f"<b>Tracking:</b> <code>{tracking}</code>\n\n"
                f"I'll hit you up when USPS scans it and when it's delivered 🔔",
                parse_mode=ParseMode.HTML,
            )
            # Clear context
            context.user_data.pop("pending_tracking", None)
            context.user_data.pop("pending_recipient", None)
            context.user_data.pop("pending_amount", None)
        else:
            await query.edit_message_text(
                f"That tracking number is already in your list gang 👀\n"
                f"<code>{tracking}</code>",
                parse_mode=ParseMode.HTML,
            )

    # ── Check tracking status ─────────────────────────────────────────────
    elif data.startswith("track:"):
        pkg_id = int(data.split(":")[1])
        row = db_get_by_id(pkg_id)
        if not row:
            await query.edit_message_text("Couldn't find that package 🤔")
            return

        _, tracking, category, status, last_event = row
        await query.edit_message_text(
            f"🔍 Checking on it...",
        )
        info = get_tracking_status(tracking)
        db_update_package(
            tracking, info["status"], info["raw"],
            int(info["usps_scanned"]), int(info["delivered"])
        )
        await query.edit_message_text(
            f"📍 <b>Latest update:</b>\n\n"
            f"<code>{tracking}</code>\n"
            f"{_esc(info['status'])}\n\n"
            f"<i>{_esc(info['raw'])}</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Where dat pack? 🗺️", callback_data=f"track:{pkg_id}"),
                InlineKeyboardButton("Remove from list 🗑️", callback_data=f"remove:{pkg_id}"),
            ]]),
        )

    # ── Remove package ────────────────────────────────────────────────────
    elif data.startswith("remove:"):
        pkg_id = int(data.split(":")[1])
        db_remove_package(pkg_id)
        await query.edit_message_text("Gone 👨‍💻")


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ===========================================================================
# STARTUP HOOK
# ===========================================================================

async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand("start", "Open the tracker menu"),
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
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_buttons))

    threading.Thread(
        target=tracking_loop, args=(app.bot,), daemon=True
    ).start()

    logger.info("Bot running. Send /start in Telegram.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()