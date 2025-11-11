#!/usr/bin/env python3
"""
AirdropVision v2.2 — BEAUTIFUL UI EDITION
REMOVED: All Scholarship logic.
REMOVED: All Game logic.
ADDED: Comprehensive, nested inline menu system for beautiful UI.
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
VERSION = "2.2.0-Beautiful-UI"
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

# --- Feature/Job Config ---
WEB3_JOBS_QUERIES = {
    "cm": '"community manager" web3 -"looking for"',
    "mod": '"community moderator" web3 -"looking for"',
    "shiller": 'shiller web3 -"looking for"',
}

# --- Spam Config ---
DEFAULT_SPAM_KEYWORDS = "giveaway,retweet,follow,tag 3,like,rt,gleam.io,promo,dm me,whatsapp"

# --- Global State & Locks ---
SPAM_WORDS: Set[str] = set()
HEALTHY_NITTER_INSTANCES: List[str] = []
NITTER_CHECK_LOCK = asyncio.Lock()
FILTER_LOCK = asyncio.Lock()
QUERY_LOCK = asyncio.Lock()
ENABLED_FEATURES: Dict[str, bool] = {}

# ----------------- LOGGING SETUP -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
)
logger = logging.getLogger(BOT_NAME)


# ----------------------------------------------------------------------
## 2. 🗃️ DATABASE LOGIC
# ----------------------------------------------------------------------

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


# ----------------------------------------------------------------------
## 3. 🕸️ SCRAPERS & BACKGROUND LOOPS
# ----------------------------------------------------------------------

# ----------------- TELEGRAM SENDER -----------------
async def send_telegram_async(http_client: httpx.AsyncClient, text: str, parse_mode="Markdown"):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: 
        logger.warning("TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set. Skipping send.")
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True
    }
    try:
        r = await http_client.post(url, json=payload, timeout=10)
        if r.status_code == 429:
            await asyncio.sleep(5)
            await send_telegram_async(http_client, text, parse_mode) # Retry
        elif r.status_code != 200:
            logger.warning(f"Telegram API failed: {r.status_code} - {r.text}")
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")

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

async def load_enabled_features():
    """Loads feature toggles from DB into the global ENABLED_FEATURES dict."""
    defaults = {
        "airdrop": True, 
        "job_cm": True, "job_mod": True, "job_shiller": True
    }
    stored = await db.meta_get_json("enabled_features", defaults)
    for k, v in defaults.items():
        if k not in stored: stored[k] = v
            
    global ENABLED_FEATURES
    ENABLED_FEATURES = stored
    logger.info(f"Loaded feature states: {ENABLED_FEATURES}")
    
async def save_enabled_features():
    await db.meta_set_json("enabled_features", ENABLED_FEATURES)

def is_feature_enabled(key: str) -> bool:
    return ENABLED_FEATURES.get(key, False)

async def get_custom_nitter_queries() -> List[str]:
    """Retrieves custom queries from DB, setting defaults if none exist."""
    stored = await db.meta_get_json("custom_nitter_queries", [])
    if not stored:
        await db.meta_set_json("custom_nitter_queries", DEFAULT_CUSTOM_QUERIES)
        return DEFAULT_CUSTOM_QUERIES
    return stored

# ----------------- NITTER/TWITTER -----------------
def parse_nitter(html: str, base_url: str):
    # (Same parsing logic as before)
    soup = BeautifulSoup(html, "lxml")
    results = []
    items = soup.find_all("div", class_="timeline-item")
    for item in items:
        link = item.find("a", class_="tweet-link")
        if not link: continue
        href = link.get("href", "")
        tweet_id = href.split("/")[-1].replace("#m", "")
        url = urllib.parse.urljoin(base_url, href)
        content = item.find("div", class_="tweet-content")
        text = content.get_text(" ", strip=True) if content else ""
        results.append({"id": tweet_id, "url": url, "text": text})
    return results

async def check_nitter_health(http_client: httpx.AsyncClient):
    # (Same health check logic as before)
    logger.info("Checking Nitter instances health...")
    async def check(instance):
        try:
            r = await http_client.get(f"{instance}/search?q=test", timeout=8)
            if r.status_code == 200 and ("timeline" in r.text or "tweet" in r.text):
                return instance
        except: pass
        return None

    tasks = [check(i) for i in NITTER_INSTANCES]
    results = await asyncio.gather(*tasks)
    healthy = [r for r in results if r]
    global HEALTHY_NITTER_INSTANCES
    async with NITTER_CHECK_LOCK:
        HEALTHY_NITTER_INSTANCES = healthy
    logger.info(f"Healthy Nitter instances: {len(healthy)}")

async def nitter_health_loop(http_client: httpx.AsyncClient):
    while True:
        await check_nitter_health(http_client)
        await asyncio.sleep(1800) # 30 mins

async def scan_nitter_query(http_client: httpx.AsyncClient, queries: List[str], db_kind: str, tag: str, feature_key: str):
    if not is_feature_enabled(feature_key):
        logger.debug(f"Skipping {feature_key} scan (disabled).")
        return

    async with NITTER_CHECK_LOCK:
        instances = HEALTHY_NITTER_INSTANCES or NITTER_INSTANCES
    if not instances:
        logger.warning(f"No Nitter instances for {tag} scan.")
        return

    for q in queries:
        for instance in instances:
            try:
                url = f"{instance}/search?f=tweets&q={urllib.parse.quote(q)}"
                r = await http_client.get(url, timeout=HTTP_TIMEOUT)
                if r.status_code != 200: continue
                
                parsed = await asyncio.to_thread(parse_nitter, r.text, instance)
                if not parsed: continue

                for t in parsed[:MAX_RESULTS]:
                    if is_spam(t['text']):
                        continue
                    if await db.seen_add(f"{db_kind}:{t['id']}", db_kind, t):
                        msg = f"{tag} (Query: `{q}`)\n\n{t['text']}\n\n🔗 [Link]({t['url']})"
                        await send_telegram_async(http_client, msg)
                        await asyncio.sleep(0.5)
                break 
            except Exception as e:
                logger.debug(f"Nitter error {instance}: {e}")
        await asyncio.sleep(2)

# ----------------- WEB3 JOBS -----------------
async def scan_web3_jobs(http_client: httpx.AsyncClient):
    job_types = {
        "job_cm": ("Community Manager", WEB3_JOBS_QUERIES["cm"]),
        "job_mod": ("Community Moderator", WEB3_JOBS_QUERIES["mod"]),
        "job_shiller": ("Shiller/Promoter", WEB3_JOBS_QUERIES["shiller"]),
    }
    
    for key, (tag_name, query) in job_types.items():
        if is_feature_enabled(key):
            await scan_nitter_query(http_client, [query], f"job_{key}", f"💼 *Web3 Job: {tag_name}*", key)
        else:
            logger.debug(f"Skipping Web3 Job: {tag_name} scan (disabled).")

# ----------------- MAIN SCHEDULERS -----------------
async def scheduler_loop(http_client: httpx.AsyncClient):
    logger.info("Scheduler started.")
    while True:
        try:
            custom_queries = await get_custom_nitter_queries()
            await scan_nitter_query(http_client, custom_queries, "custom_nitter", "🐦 *Custom Query Airdrop*", "airdrop")
            
            await scan_web3_jobs(http_client)
            
            count = await db.seen_count()
            logger.info(f"Scan cycle complete. DB Size: {count}")
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
        await asyncio.sleep(POLL_INTERVAL_MINUTES * 60)

async def run_manual_scan(http_client: httpx.AsyncClient, chat_id: int, context):
    await context.bot.send_message(chat_id, "🚀 Manual scan initiated...")
    try:
        custom_queries = await get_custom_nitter_queries()
        await scan_nitter_query(http_client, custom_queries, "custom_nitter", "🐦 *Custom Query Airdrop*", "airdrop")
        await scan_web3_jobs(http_client)
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

async def get_main_menu():
    text = f"✨ *{BOT_NAME} v{VERSION} | Main Menu*\n\nChoose an option to manage the bot's operation:"
    keyboard = [
        [InlineKeyboardButton("📊 Statistics", callback_data="stats")],
        [InlineKeyboardButton("🐦 Custom Queries", callback_data="queries_menu"), InlineKeyboardButton("🚫 Spam Filters", callback_data="filters_menu")],
        [InlineKeyboardButton("⚙️ Feature Toggles", callback_data="features_menu")],
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

FEATURE_MAP = {
    "airdrop": "Airdrops (Custom Nitter Queries)",
    "job_cm": "Web3 Job: Community Manager",
    "job_mod": "Web3 Job: Community Moderator",
    "job_shiller": "Web3 Job: Shiller/Promoter",
}

async def get_features_menu():
    await load_enabled_features()
    kb = []
    
    text = "⚙️ *Feature Toggles*\n\nSelect a scanner to enable or disable its operation."
    
    for key, name in FEATURE_MAP.items():
        status = "✅ On" if is_feature_enabled(key) else "❌ Off"
        kb.append([InlineKeyboardButton(f"{status} | {name}", callback_data=f"toggle_feature:{key}")])
        
    kb.append([InlineKeyboardButton("↩️ Back to Main Menu", callback_data="main_menu")])
    return text, InlineKeyboardMarkup(kb)

async def get_queries_menu():
    text = (
        "🐦 *Custom Nitter Queries*\n\n"
        "Manage the search queries used for the main Airdrop/Mint scan.\n\n"
        "`/addquery <query>` - Add new query\n"
        "`/delquery <query>` - Remove a query"
    )
    kb = [
        [InlineKeyboardButton("📜 View All Queries", callback_data="list_queries")],
        [InlineKeyboardButton("↩️ Back to Main Menu", callback_data="main_menu")]
    ]
    return text, InlineKeyboardMarkup(kb)

async def get_list_queries_menu():
    queries = await get_custom_nitter_queries()
    text = "📜 *Active Nitter Search Queries:*\n\n" + "\n".join([f"• `{q}`" for q in queries])
    if not queries: text = "No custom queries set. Use `/addquery` to add your first one."
    
    kb = [[InlineKeyboardButton("🔙 Back to Queries Menu", callback_data="queries_menu")]]
    return text, InlineKeyboardMarkup(kb)

async def get_filters_menu():
    text = (
        "🚫 *Spam Filters*\n\n"
        "Keywords added here will block a tweet from being posted.\n\n"
        "`/addfilter <word>` - Add a filter\n"
        "`/delfilter <word>` - Remove a filter"
    )
    kb = [
        [InlineKeyboardButton("📜 View All Filters", callback_data="list_filters")],
        [InlineKeyboardButton("↩️ Back to Main Menu", callback_data="main_menu")]
    ]
    return text, InlineKeyboardMarkup(kb)

async def get_list_filters_menu():
    await load_spam_words()
    text = "📜 *Active Spam Keywords:*\n\n" + "\n".join([f"• `{w}`" for w in sorted(list(SPAM_WORDS))])
    if not SPAM_WORDS: text = "No filters set. Use `/addfilter` to add your first one."
    
    kb = [[InlineKeyboardButton("🔙 Back to Filters Menu", callback_data="filters_menu")]]
    return text, InlineKeyboardMarkup(kb)

# ----------------- COMMAND HANDLERS -----------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update, context): return
    text, markup = await get_main_menu()
    await update.message.reply_text(
        text,
        reply_markup=markup,
        parse_mode=ParseMode.MARKDOWN
    )

# Configuration commands are kept for ease of use, but logic is simplified
async def add_filter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update, context): return
    if not context.args:
        await update.message.reply_text("Usage: `/addfilter <word>`", parse_mode=ParseMode.MARKDOWN)
        return
    
    word = " ".join(context.args).lower().strip()
    async with FILTER_LOCK:
        current = list(SPAM_WORDS)
        if word not in current:
            current.append(word)
            await db.meta_set_json("spam_keywords", current)
            await load_spam_words() # Reload global state
            await update.message.reply_text(f"✅ Added filter: `{word}`. Current count: {len(SPAM_WORDS)}", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(f"⚠️ Filter `{word}` already exists.", parse_mode=ParseMode.MARKDOWN)

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
            await load_spam_words() # Reload global state
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
    elif data == "stats":
        text, markup = await get_stats_menu()
    elif data == "features_menu":
        text, markup = await get_features_menu()
    elif data == "queries_menu":
        text, markup = await get_queries_menu()
    elif data == "list_queries":
        text, markup = await get_list_queries_menu()
    elif data == "filters_menu":
        text, markup = await get_filters_menu()
    elif data == "list_filters":
        text, markup = await get_list_filters_menu()
        
    # Actions
    elif data == "scan_now":
        await query.edit_message_text("🚀 Scanning now... this may take a moment.")
        http_client = context.application.bot_data['http_client']
        # Start scan in background to avoid callback timeout
        asyncio.create_task(run_manual_scan(http_client, query.message.chat_id, context))
        return # Do not edit message text after starting background task
        
    elif data.startswith("toggle_feature:"):
        key = data.split(":")[1]
        if key in ENABLED_FEATURES:
            ENABLED_FEATURES[key] = not ENABLED_FEATURES[key]
            await save_enabled_features()
            status_text = "Enabled" if ENABLED_FEATURES[key] else "Disabled"
            await query.answer(f"{FEATURE_MAP.get(key, key)} set to {status_text}!", show_alert=True)
        else:
            await query.answer("Unknown feature.", show_alert=True)
            
        # Refresh the features menu
        text, markup = await get_features_menu()
        
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
        await load_enabled_features()
        await get_custom_nitter_queries() 

        # Start the web server
        asgi_server_task = asyncio.create_task(run_asgi_server())

        # Setup Telegram Bot
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        app.bot_data['http_client'] = http_client

        # --- Register Handlers ---
        app.add_handler(CommandHandler("start", start_cmd))
        
        # Preserve commands for quick config, although menu is primary UI
        app.add_handler(CommandHandler("addfilter", add_filter_cmd))
        app.add_handler(CommandHandler("delfilter", del_filter_cmd))
        app.add_handler(CommandHandler("addquery", add_query_cmd))
        app.add_handler(CommandHandler("delquery", del_query_cmd))
        
        app.add_handler(CallbackQueryHandler(callback_router))

        # --- Background Tasks ---
        health_checker = asyncio.create_task(nitter_health_loop(http_client))
        scheduler = asyncio.create_task(scheduler_loop(http_client))

        logger.info(f"Bot {VERSION} initialized. Starting polling...")
        
        await app.initialize()
        await app.updater.start_polling()
        await app.start()

        # Gather tasks and run until complete/cancelled
        all_tasks = [health_checker, scheduler, asgi_server_task]
        try:
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

