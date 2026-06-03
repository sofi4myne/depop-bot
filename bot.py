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
from pyzbar.pyzbar import decode as zbar_decode
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
                status          TEXT    NOT NULL DEFAULT 'pending',
                last_event      TEXT    NOT NULL DEFAULT '',
                usps_scanned    INTEGER NOT NULL DEFAULT 0,
                delivered       INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()
    logger.info("Database ready: %s", DB_PATH)


def db_add_package(tracking: str, category: str) -> bool:
    """Returns True if inserted, False if tracking number already exists."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO packages (tracking_number, category) VALUES (?, ?)",
                (tracking, category),
            )
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def db_get_active(category: str = None):
    with sqlite3.connect(DB_PATH) as conn:
        if category:
            return conn.execute(
                "SELECT id, tracking_number, status, last_event FROM packages "
                "WHERE delivered = 0 AND category = ? ORDER BY id DESC",
                (category,),
            ).fetchall()
        return conn.execute(
            "SELECT id, tracking_number, category, status, last_event "
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

def scan_barcode_from_image(image: Image.Image) -> str | None:
    """Try to decode a barcode from a PIL Image. Returns tracking number or None."""
    # Try original size first
    results = zbar_decode(image)
    if results:
        for r in results:
            val = r.data.decode("utf-8").strip()
            if is_usps_tracking(val):
                return val

    # Try resized larger for small/dense barcodes
    w, h = image.size
    large = image.resize((w * 2, h * 2), Image.LANCZOS)
    results = zbar_decode(large)
    if results:
        for r in results:
            val = r.data.decode("utf-8").strip()
            if is_usps_tracking(val):
                return val

    # Try grayscale
    gray = image.convert("L")
    results = zbar_decode(gray)
    if results:
        for r in results:
            val = r.data.decode("utf-8").strip()
            if is_usps_tracking(val):
                return val

    return None


def is_usps_tracking(value: str) -> bool:
    """USPS tracking numbers are 20-22 digits, or start with known prefixes."""
    clean = re.sub(r"\s", "", value)
    if re.match(r"^\d{20,22}$", clean):
        return True
    if re.match(r"^(9[2345]\d{18,20}|82\d{8})$", clean):
        return True
    return False


def extract_tracking_from_file(file_bytes: bytes, is_pdf: bool) -> str | None:
    """Extract tracking number from PNG/JPG or PDF bytes."""
    try:
        if is_pdf:
            pages = convert_from_bytes(file_bytes, dpi=200)
            for page in pages:
                result = scan_barcode_from_image(page)
                if result:
                    return result
        else:
            image = Image.open(io.BytesIO(file_bytes))
            return scan_barcode_from_image(image)
    except Exception as exc:
        logger.exception("Error scanning barcode: %s", exc)
    return None


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
    await update.message.reply_text("Loading...", reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text(
        "📦 <b>Depop Shipping Tracker</b>\n\n"
        "Send me a shipping label (PNG or PDF) and I'll scan the barcode, "
        "track it automatically, and let you know when USPS picks it up and "
        "when it's delivered 🔔\n\n"
        "Use the buttons below to view your packages by account:",
        parse_mode=ParseMode.HTML,
        reply_markup=make_menu(),
    )


async def cmd_menu(update, context) -> None:
    await update.message.reply_text("Loading...", reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text("Here's ya menu:", reply_markup=make_menu())


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo messages — treat as shipping label."""
    await update.message.reply_text("Hold up lemme scan this real quick 👀")

    photo = update.message.photo[-1]  # highest resolution
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()

    tracking = extract_tracking_from_file(bytes(file_bytes), is_pdf=False)
    await _after_scan(update, context, tracking)


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

    await update.message.reply_text("Hold up lemme scan this real quick 👀")

    file = await context.bot.get_file(doc.file_id)
    file_bytes = await file.download_as_bytearray()

    is_pdf = "pdf" in mime
    tracking = extract_tracking_from_file(bytes(file_bytes), is_pdf=is_pdf)
    await _after_scan(update, context, tracking)


async def _after_scan(update: Update, context, tracking: str | None) -> None:
    """Called after barcode scan attempt — ask for category or report failure."""
    if not tracking:
        await update.message.reply_text(
            "That label tweakin, try again 😂\n\n"
            "Make sure the barcode is clear and not cut off. "
            "Try sending it as a file instead of a photo if it keeps failing.",
            reply_markup=make_menu(),
        )
        return

    # Store tracking number temporarily in context for next step
    context.user_data["pending_tracking"] = tracking

    await update.message.reply_text(
        f"Found dat ho ✅\n\n"
        f"<b>Tracking:</b> <code>{tracking}</code>\n\n"
        "Which account?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("granny", callback_data=f"save:granny:{tracking}"),
            InlineKeyboardButton("veli0r", callback_data=f"save:veli0r:{tracking}"),
        ]]),
    )


async def handle_menu_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
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

    await update.message.reply_text(
        f"📦 <b>{category}'s packages ({len(rows)}):</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=make_menu(),
    )

    for pkg_id, tracking, status, last_event in rows:
        display_status = last_event if last_event else status
        await update.message.reply_text(
            f"<code>{tracking}</code>\n📍 {_esc(display_status)}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Where dat pack? 🗺️", callback_data=f"track:{pkg_id}"),
                InlineKeyboardButton("Remove from list 🗑️", callback_data=f"remove:{pkg_id}"),
            ]]),
        )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    # ── Save package to a category ────────────────────────────────────────
    if data.startswith("save:"):
        _, category, tracking = data.split(":", 2)
        added = db_add_package(tracking, category)
        if added:
            await query.edit_message_text(
                f"✅ Saved under <b>{category}</b>!\n\n"
                f"<b>Tracking:</b> <code>{tracking}</code>\n\n"
                f"I'll hit you up when USPS scans it and when it's delivered 🔔",
                parse_mode=ParseMode.HTML,
            )
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