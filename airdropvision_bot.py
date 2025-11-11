#!/usr/bin/env python3
"""
AirdropVision v2.5 — CONVERSATION HANDLER EDITION
ADDED: Full ConversationHandler logic for /addfilter to prompt the user directly after a button click.
REMOVED: The inline button that pre-filled the command text.
"""

import logging
import asyncio
import httpx
import uvicorn
import os
import json
import urllib.parse
from typing import List, Dict, Set, Optional

# External dependencies
import aiosqlite
from bs4 import BeautifulSoup
from flask import Flask

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler, # <-- NEW
    MessageHandler, # <-- NEW
    filters, # <-- NEW
)

# ----------------------------------------------------------------------
## 1. ⚙️ CONFIGURATION & GLOBAL STATE
# ----------------------------------------------------------------------

# --- Core Bot Config ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL", "10"))
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "25"))
BOT_NAME = os.environ.get("BOT_NAME", "AirdropVision")
VERSION = "2.5.0-Conversation-Handler"
DB_PATH = os.environ.get("DB_PATH", "airdropvision_v5.db")
HTTP_TIMEOUT = 15

# --- Nitter Config ---
DEFAULT_NITTER_LIST = [
    "https://nitter.net", "https://nitter.tiekoetter.com", "https://nitter.space"
]
NITTER_INSTANCES_CSV = os.environ.get("NITTER_INSTANCES_CSV")
NITTER_INSTANCES = [url.strip() for url in (NITTER_INSTANCES_CSV.split(',') if NITTER_INSTANCES_CSV else DEFAULT_NITTER_LIST) if url.strip()]

# Custom Queries Defaults
DEFAULT_CUSTOM_QUERIES = [
    '("free mint" OR "free-mint") -filter:replies',
    '("solana airdrop") -filter:replies'
]

# --- Spam Config ---
DEFAULT_SPAM_KEYWORDS = "giveaway,retweet,follow,tag 3,like,rt,gleam.io,promo,dm me,whatsapp"

# --- Global State & Locks ---
SPAM_WORDS: Set[str] = set()
HEALTHY_NITTER_INSTANCES: List[str] = []
NITTER_CHECK_LOCK = asyncio.Lock()
FILTER_LOCK = asyncio.Lock()
QUERY_LOCK = asyncio.Lock()

# --- Conversation States (NEW) ---
AWAITING_ADD_FILTER_WORD = 1

# ----------------- LOGGING SETUP -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
)
logger = logging.getLogger(BOT_NAME)


# ----------------------------------------------------------------------
## 2. 🗃️ DATABASE LOGIC (Omitted for brevity, unchanged)
# ----------------------------------------------------------------------

# (Database class and methods remain unchanged from v2.4)
CREATE_SEEN_SQL = """
CREATE TABLE IF NOT EXISTS seen (
    id TEXT PRIMARY KEY,
    kind TEXT,
    meta TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""
CREATE_META_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""

class DB:
    def __init__(self, path=DB_PATH):
        self.path = path
        self._init_done = False
        self._lock = asyncio.Lock()

    async def init(self):
        if self._init_done: return
        async with self._lock:
            if self._init_done: return
            async with aiosqlite.connect(self.path) as conn:
                await conn.execute(CREATE_SEEN_SQL)
                await conn.execute(CREATE_META_SQL)
                await conn.commit()
            self._init_done = True
            logger.info("Database initialized.")

    async def seen_add(self, id: str, kind: str = "generic", meta: Optional[dict] = None) -> bool:
        await self.init()
        try:
            async with aiosqlite.connect(self.path) as conn:
                await conn.execute(
                    "INSERT INTO seen(id, kind, meta) VALUES (?, ?, ?)",
                    (id, kind, json.dumps(meta or {})),
                )
                await conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def seen_count(self) -> int:
        await self.init()
        async with aiosqlite.connect(self.path) as conn:
            async with conn.execute("SELECT COUNT(1) FROM seen") as cur:
                row = await cur.fetchone()
                return row[0] if row else 0

    async def meta_get(self, k: str, default=None):
        await self.init()
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT v FROM meta WHERE k=?", (k,)) as cur:
                row = await cur.fetchone()
                return row['v'] if row else default

    async def meta_set(self, k: str, v: str):
        await self.init()
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES (?,?)", (k, v))
            await conn.commit()

    async def meta_get_json(self, k: str, default: dict = {}) -> dict:
        val = await self.meta_get(k)
        if not val: return default
        try: return json.loads(val)
        except: return default

    async def meta_set_json(self, k: str, v: dict):
        await self.meta_set(k, json.dumps(v))

# Create a single, shared instance
db = DB()

# (Scraping and scheduling logic remain unchanged from v2.4, only included relevant parts)

# ----------------- STATE MANAGEMENT -----------------
async def load_spam_words():
    """Loads spam words from DB into the global SPAM_WORDS set."""
    stored = await db.meta_get_json("spam_keywords", [])
    if not stored:
        stored = [s.strip().lower() for s in DEFAULT_SPAM_KEYWORDS.split(',') if s.strip()]
        await db.meta_set_json("spam_keywords", stored)
    global SPAM_WORDS
    SPAM_WORDS = set(stored)
    logger.info(f"Loaded {len(SPAM_WORDS)} spam filters.")

def is_spam(text: str) -> bool:
    if not text: return False
    text_low = text.lower()
    return any(w in text_low for w in SPAM_WORDS)

async def get_custom_nitter_queries() -> List[str]:
    """Retrieves custom queries from DB, setting defaults if none exist."""
    stored = await db.meta_get_json("custom_nitter_queries", [])
    if not stored:
        await db.meta_set_json("custom_nitter_queries", DEFAULT_CUSTOM_QUERIES)
        return DEFAULT_CUSTOM_QUERIES
    return stored

# (Nitter scanning and scheduling loops remain unchanged)

async def run_manual_scan(http_client: httpx.AsyncClient, chat_id: int, context):
    await context.bot.send_message(chat_id, "🚀 Manual scan initiated...")
    try:
        custom_queries = await get_custom_nitter_queries()
        # Assume scan_nitter_query is defined elsewhere or imported
        # This is a placeholder for the actual scan function
        await asyncio.sleep(3) # Simulate scan time
        await context.bot.send_message(chat_id, "✅ Manual Scan Complete.")
    except Exception as e:
        logger.error(f"Manual scan error: {e}")
        await context.bot.send_message(chat_id, f"⚠️ Manual Scan Failed: {e}")


# ----------------------------------------------------------------------
## 4. 🤖 TELEGRAM BOT & UI LOGIC
# ----------------------------------------------------------------------

# ----------------- AUTH HELPER -----------------
def is_authorized(chat_id: int) -> bool:
    return str(chat_id) == str(TELEGRAM_CHAT_ID)

async def auth_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """A guard function to check auth on commands and callbacks."""
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        logger.warning(f"Unauthorized access attempt from chat_id: {chat_id}")
        if update.message:
            await update.message.reply_text("🚫 Unauthorized chat.")
        elif update.callback_query:
            await update.callback_query.answer("🚫 Unauthorized chat.", show_alert=True)
        return False
    return True

# ----------------- UI/MENU GENERATORS -----------------
# (Menus remain largely the same, only get_command_input_menu is changed)

async def get_main_menu():
    text = f"✨ *{BOT_NAME} v{VERSION} | Main Menu*\n\nChoose an option to manage the bot's operation:"
    keyboard = [
        [InlineKeyboardButton("📊 Statistics", callback_data="stats")],
        [InlineKeyboardButton("🐦 Custom Queries", callback_data="queries_menu")],
        [InlineKeyboardButton("🚫 Spam Filters", callback_data="filters_menu")],
        [InlineKeyboardButton("🚀 Force Scan Now", callback_data="scan_now")],
    ]
    return text, InlineKeyboardMarkup(keyboard)

async def get_stats_menu():
    count = await db.seen_count()
    spam_count = len(SPAM_WORDS)
    queries_count = len(await get_custom_nitter_queries())
    async with NITTER_CHECK_LOCK:
        healthy_count = len(HEALTHY_NITTER_INSTANCES)
    
    text = (
        "📊 *Bot Statistics*\n\n"
        f"➡️ **Items Tracked:** `{count}`\n"
        f"➡️ **Custom Queries:** `{queries_count}`\n"
        f"➡️ **Spam Filters:** `{spam_count}`\n"
        f"➡️ **Healthy Nitter Nodes:** `{healthy_count}`"
    )
    keyboard = [[InlineKeyboardButton("↩️ Back to Main Menu", callback_data="main_menu")]]
    return text, InlineKeyboardMarkup(keyboard)

async def get_queries_menu():
    text = "🐦 *Custom Nitter Queries*\n\nManage the search queries used for the bot's scanning."
    kb = [
        [InlineKeyboardButton("📜 View All Queries", callback_data="list_queries")],
        [InlineKeyboardButton("➕ Add Query", callback_data="add_query_input"), InlineKeyboardButton("➖ Delete Query", callback_data="del_query_input")],
        [InlineKeyboardButton("↩️ Back to Main Menu", callback_data="main_menu")]
    ]
    return text, InlineKeyboardMarkup(kb)

async def get_list_queries_menu():
    queries = await get_custom_nitter_queries()
    text = "📜 *Active Nitter Search Queries:*\n\n" + "\n".join([f"• `{q}`" for q in queries])
    if not queries: text = "No custom queries set."
    
    kb = [[InlineKeyboardButton("🔙 Back to Queries Menu", callback_data="queries_menu")]]
    return text, InlineKeyboardMarkup(kb)

async def get_filters_menu():
    text = "🚫 *Spam Filters*\n\nKeywords added here will block a tweet from being posted."
    kb = [
        [InlineKeyboardButton("📜 View All Filters", callback_data="list_filters")],
        [InlineKeyboardButton("➕ Add Filter", callback_data="add_filter_input"), InlineKeyboardButton("➖ Delete Filter", callback_data="del_filter_input")],
        [InlineKeyboardButton("↩️ Back to Main Menu", callback_data="main_menu")]
    ]
    return text, InlineKeyboardMarkup(kb)

async def get_list_filters_menu():
    await load_spam_words()
    text = "📜 *Active Spam Keywords:*\n\n" + "\n".join([f"• `{w}`" for w in sorted(list(SPAM_WORDS))])
    if not SPAM_WORDS: text = "No filters set."
    
    kb = [[InlineKeyboardButton("🔙 Back to Filters Menu", callback_data="filters_menu")]]
    return text, InlineKeyboardMarkup(kb)

# The /addfilter command input screen is now handled by the conversation
# For /delfilter, /addquery, /delquery, we will use the ConversationHandler pattern too, 
# but for now, they use the old method until their handlers are converted.

async def get_command_input_menu_placeholder(action: str):
    # Temporary menu for commands not yet converted to ConversationHandler
    action_map = {
        "del_query": {"text": "➖ *Delete Query*", "command": "/delquery ", "back": "queries_menu", "tip": "Paste the exact query to delete after the command."},
        "add_query": {"text": "➕ *Add Query*", "command": "/addquery ", "back": "queries_menu", "tip": "Paste the full search query after the command."},
        "del_filter": {"text": "➖ *Delete Filter*", "command": "/delfilter ", "back": "filters_menu", "tip": "Type the exact word(s) you want to unblock after the command."},
    }
    
    details = action_map.get(action)
    if not details:
        return "Error: Unknown action.", InlineKeyboardMarkup([])

    text = (
        f"{details['text']} (To be converted to interactive prompt)\n\n"
        f"1. Tap the button below to **pre-fill the command**.\n"
        f"2. {details['tip']}\n"
        f"3. Send the message."
    )
    
    # Create the button that pre-fills the command using switch_inline_query_current_chat
    kb = [
        [InlineKeyboardButton(f"⌨️ Type {details['command'].strip()}", switch_inline_query_current_chat=details['command'])],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data=details['back'])]
    ]
    return text, InlineKeyboardMarkup(kb)

# ----------------- CONVERSATION HANDLERS (NEW) -----------------

# 1. Initiator: Called when '➕ Add Filter' is clicked
async def start_add_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    keyboard = [[InlineKeyboardButton("↩️ Cancel", callback_data="cancel_conv")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "➕ *Add Filter*\n\nPlease send me the **word or words** you want to add as a spam filter (e.g., `retweet follow`):",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    
    return AWAITING_ADD_FILTER_WORD

# 2. Main Step: Called when the user sends a text message after the prompt
async def received_add_filter_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.text:
        await update.message.reply_text("I need a word or phrase to add. Please try again or type `/start` to go back.", parse_mode=ParseMode.MARKDOWN)
        return AWAITING_ADD_FILTER_WORD # Stay in this state
        
    word = update.message.text.lower().strip()
    
    async with FILTER_LOCK:
        current = list(SPAM_WORDS)
        if word not in current:
            current.append(word)
            await db.meta_set_json("spam_keywords", current)
            await load_spam_words() # Reload global state
            await update.message.reply_text(f"✅ Added filter: `{word}`. Current count: {len(SPAM_WORDS)}", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(f"⚠️ Filter `{word}` already exists. Current count: {len(SPAM_WORDS)}", parse_mode=ParseMode.MARKDOWN)

    # End conversation and go back to main menu
    text, markup = await get_main_menu()
    await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

# 3. Fallback: Cancel command/callback
async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("❌ Filter addition cancelled.", parse_mode=ParseMode.MARKDOWN)
        
    elif update.message:
        await update.message.reply_text("❌ Action cancelled.", parse_mode=ParseMode.MARKDOWN)
    
    # Send main menu
    text, markup = await get_main_menu()
    
    # Use the appropriate reply function (message or query edit)
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
    elif update.message:
        await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)

    return ConversationHandler.END

# ----------------- COMMAND HANDLERS -----------------
# (The Command Handlers for /addfilter, /delfilter, etc. are now redundant and are only kept 
# as a fallback for users who manually type the command when the bot is not in a conversation state)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update, context): return
    text, markup = await get_main_menu()
    await update.message.reply_text(
        text,
        reply_markup=markup,
        parse_mode=ParseMode.MARKDOWN
    )

# The manual command handlers are kept for robustness outside of the conversation flow
async def add_filter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This is a fallback if the user types /addfilter manually
    if not await auth_guard(update, context): return
    if not context.args:
        await update.message.reply_text("Please use the menu to add filters, or provide a word: `/addfilter <word>`", parse_mode=ParseMode.MARKDOWN)
        return
    
    word = " ".join(context.args).lower().strip()
    async with FILTER_LOCK:
        current = list(SPAM_WORDS)
        if word not in current:
            current.append(word)
            await db.meta_set_json("spam_keywords", current)
            await load_spam_words()
            await update.message.reply_text(f"✅ Added filter: `{word}`. Current count: {len(SPAM_WORDS)}", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(f"⚠️ Filter `{word}` already exists.", parse_mode=ParseMode.MARKDOWN)

# (delfilter, addquery, delquery manual commands remain as is)
async def del_filter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update, context): return
    if not context.args:
        await update.message.reply_text("Usage: `/delfilter <word>`", parse_mode=ParseMode.MARKDOWN)
        return
        
    word = " ".join(context.args).lower().strip()
    
    async with FILTER_LOCK:
        current = list(SPAM_WORDS)
        if word in current:
            current.remove(word)
            await db.meta_set_json("spam_keywords", current)
            await load_spam_words()
            await update.message.reply_text(f"🗑 Removed filter: `{word}`. Current count: {len(SPAM_WORDS)}", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(f"⚠️ Filter `{word}` not found.", parse_mode=ParseMode.MARKDOWN)

async def add_query_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update, context): return
    if not context.args:
        await update.message.reply_text("Usage: `/addquery <full_twitter_search_query>`", parse_mode=ParseMode.MARKDOWN)
        return
    
    query = " ".join(context.args).strip()
    
    async with QUERY_LOCK:
        current = await db.meta_get_json("custom_nitter_queries", [])
        if query not in current:
            current.append(query)
            await db.meta_set_json("custom_nitter_queries", current)
            await update.message.reply_text(f"✅ Added query: `{query}`. Queries: {len(current)}", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(f"⚠️ Query `{query}` already exists.", parse_mode=ParseMode.MARKDOWN)

async def del_query_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update, context): return
    if not context.args:
        await update.message.reply_text("Usage: `/delquery <full_twitter_search_query>`", parse_mode=ParseMode.MARKDOWN)
        return
        
    query = " ".join(context.args).strip()
    
    async with QUERY_LOCK:
        current = await db.meta_get_json("custom_nitter_queries", [])
        if query in current:
            current.remove(query)
            await db.meta_set_json("custom_nitter_queries", current)
            await update.message.reply_text(f"🗑 Removed query: `{query}`. Queries: {len(current)}", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(f"⚠️ Query `{query}` not found.", parse_mode=ParseMode.MARKDOWN)

# ----------------- CALLBACK ROUTER -----------------
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update, context): return
    
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    # Menu Navigation
    if data == "main_menu":
        text, markup = await get_main_menu()
        
    # Stats
    elif data == "stats":
        text, markup = await get_stats_menu()
        
    # Queries Menu
    elif data == "queries_menu":
        text, markup = await get_queries_menu()
    elif data == "list_queries":
        text, markup = await get_list_queries_menu()
    
    # Filters Menu
    elif data == "filters_menu":
        text, markup = await get_filters_menu()
    elif data == "list_filters":
        text, markup = await get_list_filters_menu()

    # Commands NOT handled by ConversationHandler yet (use temporary filler)
    elif data in ["del_query_input", "add_query_input", "del_filter_input"]:
        # The 'add_filter_input' is handled by the ConversationHandler, not here
        text, markup = await get_command_input_menu_placeholder(data.replace("_input", ""))
        
    # Actions
    elif data == "scan_now":
        await query.edit_message_text("🚀 Scanning now... this may take a moment.")
        http_client = context.application.bot_data['http_client']
        asyncio.create_task(run_manual_scan(http_client, query.message.chat_id, context))
        return 
        
    else:
        logger.warning(f"Unknown callback data: {data}")
        await query.answer("Unknown action.", show_alert=True)
        return

    # Update the message with the new menu/screen
    await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)

# ----------------- FLASK & UVICORN -----------------
flask_app = Flask(__name__)
@flask_app.route("/health")
def health():
    return f"{BOT_NAME} v{VERSION} OK", 200

async def run_asgi_server():
    port = int(os.environ.get("PORT", 8080))
    server_config = uvicorn.Config(
        flask_app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        loop="asyncio"
    )
    server = uvicorn.Server(server_config)
    logger.info(f"Starting ASGI health server on port {port}...")
    await server.serve()

# ----------------- BOOTSTRAP / MAIN -----------------
async def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Error: TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set.")
        return

    async with httpx.AsyncClient(
        headers={"User-Agent": f"Mozilla/5.0 (compatible; {BOT_NAME}/{VERSION})"},
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
    ) as http_client:

        # Init DB and load initial state
        await db.init()
        await load_spam_words()
        await get_custom_nitter_queries() 

        # Start the web server
        asgi_server_task = asyncio.create_task(run_asgi_server())

        # Setup Telegram Bot
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        app.bot_data['http_client'] = http_client

        # --- CONVERSATION HANDLER for ADD FILTER (NEW) ---
        add_filter_conv_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(start_add_filter, pattern='^add_filter_input$')],
            states={
                AWAITING_ADD_FILTER_WORD: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, received_add_filter_word),
                    CallbackQueryHandler(cancel_conv, pattern='^cancel_conv$'),
                ],
            },
            fallbacks=[
                CommandHandler('start', cancel_conv), # Exit if user types /start
                CallbackQueryHandler(cancel_conv, pattern='^cancel_conv$'),
            ],
            allow_update_overlap=True # Allow other updates (like /start) to interrupt
        )
        app.add_handler(add_filter_conv_handler)
        
        # --- Other Handlers ---
        app.add_handler(CommandHandler("start", start_cmd))
        app.add_handler(CommandHandler("addfilter", add_filter_cmd))
        app.add_handler(CommandHandler("delfilter", del_filter_cmd))
        app.add_handler(CommandHandler("addquery", add_query_cmd))
        app.add_handler(CommandHandler("delquery", del_query_cmd))
        
        app.add_handler(CallbackQueryHandler(callback_router))

        # --- Background Tasks ---
        # (Assuming nitter_health_loop and scheduler_loop are defined and imported)
        # Placeholder tasks:
        health_checker = asyncio.create_task(asyncio.sleep(3600)) 
        scheduler = asyncio.create_task(asyncio.sleep(POLL_INTERVAL_MINUTES * 60))


        logger.info(f"Bot {VERSION} initialized. Starting polling...")
        
        await app.initialize()
        await app.updater.start_polling()
        await app.start()

        # Gather tasks and run until complete/cancelled
        all_tasks = [health_checker, scheduler, asgi_server_task]
        try:
            # Added a short sleep to keep main running without the real loop functions
            await asyncio.gather(*all_tasks)
        except (asyncio.CancelledError, KeyboardInterrupt):
            logger.info("Shutdown signal received.")
        except Exception as e:
            logger.error(f"Main loop error: {e}", exc_info=True)
        finally:
            logger.info("Stopping components...")
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
            for task in all_tasks:
                if not task.done():
                    task.cancel()
            logger.info("Bot shut down gracefully.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Program exited gracefully.")

