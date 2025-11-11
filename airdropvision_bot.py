#!/usr/bin/env python3
"""
AirdropVision v2.0 — RSS BRIDGE EDITION
REPLACED: Nitter scraping with RSS Bridge + TwitterV2Bridge
ADDED: Twitter API v2 integration via RSS Bridge
IMPROVED: Much more reliable and structured data
"""

import logging
import asyncio
import httpx
import uvicorn
import os
import json
import urllib.parse
import xml.etree.ElementTree as ET
from typing import List, Dict, Set, Optional
from datetime import datetime, timedelta

# External dependencies
import aiosqlite
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
POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL", "15"))
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "25"))
BOT_NAME = os.environ.get("BOT_NAME", "AirdropVision")
VERSION = "2.0.0"  # Major version update
DB_PATH = os.environ.get("DB_PATH", "airdropvision_v6.db")
HTTP_TIMEOUT = 30

# --- RSS Bridge Config ---
RSS_BRIDGE_URL = os.environ.get("RSS_BRIDGE_URL", "http://localhost")
RSS_BRIDGE_INSTANCES = [
    instance.strip() for instance in 
    os.environ.get("RSS_BRIDGE_INSTANCES", "https://rss-bridge.org/bridge.php,http://localhost:3000").split(',')
    if instance.strip()
]

# Custom Queries Defaults
DEFAULT_CUSTOM_QUERIES = [
    '("free mint" OR "free-mint") -filter:replies',
    '("solana airdrop") -filter:replies'
]

# --- Spam Config ---
DEFAULT_SPAM_KEYWORDS = ""

# --- Global State & Locks ---
SPAM_WORDS: Set[str] = set()
HEALTHY_RSS_BRIDGE_INSTANCES: List[str] = []
BRIDGE_CHECK_LOCK = asyncio.Lock()
FILTER_LOCK = asyncio.Lock()
QUERY_LOCK = asyncio.Lock()

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
## 3. 🕸️ RSS BRIDGE INTEGRATION
# ----------------------------------------------------------------------

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
            await send_telegram_async(http_client, text, parse_mode)
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
        if stored:
             await db.meta_set_json("spam_keywords", stored)
             
    global SPAM_WORDS
    SPAM_WORDS = set(stored)
    logger.info(f"Loaded {len(SPAM_WORDS)} spam filters.")

def is_spam(text: str) -> bool:
    if not text: return False
    text_low = text.lower()
    return any(w in text_low for w in SPAM_WORDS)

async def get_custom_nitter_queries() -> List[str]:
    """Retrieves custom queries from DB."""
    stored = await db.meta_get_json("custom_nitter_queries", [])
    if not stored:
        await db.meta_set_json("custom_nitter_queries", DEFAULT_CUSTOM_QUERIES)
        return DEFAULT_CUSTOM_QUERIES
    return stored

# ----------------- RSS BRIDGE INTEGRATION -----------------
def parse_rss_feed(xml_content: str) -> List[Dict]:
    """Parse RSS/Atom feed from RSS Bridge"""
    try:
        root = ET.fromstring(xml_content)
        items = []
        
        # Handle both RSS and Atom formats
        if root.tag.endswith('rss'):
            # RSS format
            for item in root.findall('.//item'):
                title_elem = item.find('title')
                link_elem = item.find('link')
                guid_elem = item.find('guid')
                description_elem = item.find('description')
                
                if guid_elem is not None and guid_elem.text:
                    tweet_id = guid_elem.text.split('/')[-1] if 'twitter.com' in guid_elem.text else guid_elem.text
                else:
                    # Extract from link if no GUID
                    link = link_elem.text if link_elem is not None else ""
                    tweet_id = link.split('/')[-1] if 'twitter.com' in link else ""
                
                items.append({
                    'id': tweet_id,
                    'url': link_elem.text if link_elem is not None else "",
                    'text': f"{title_elem.text if title_elem is not None else ''} - {description_elem.text if description_elem is not None else ''}",
                    'title': title_elem.text if title_elem is not None else "",
                    'description': description_elem.text if description_elem is not None else ""
                })
        else:
            # Atom format
            for entry in root.findall('.//{http://www.w3.org/2005/Atom}entry'):
                title_elem = entry.find('{http://www.w3.org/2005/Atom}title')
                link_elem = entry.find('{http://www.w3.org/2005/Atom}link')
                id_elem = entry.find('{http://www.w3.org/2005/Atom}id')
                content_elem = entry.find('{http://www.w3.org/2005/Atom}content')
                
                tweet_id = id_elem.text.split('/')[-1] if id_elem is not None and 'twitter.com' in id_elem.text else ""
                
                items.append({
                    'id': tweet_id,
                    'url': link_elem.get('href') if link_elem is not None else "",
                    'text': f"{title_elem.text if title_elem is not None else ''} - {content_elem.text if content_elem is not None else ''}",
                    'title': title_elem.text if title_elem is not None else "",
                    'description': content_elem.text if content_elem is not None else ""
                })
        
        return items
    except Exception as e:
        logger.error(f"Error parsing RSS feed: {e}")
        return []

async def check_rss_bridge_health(http_client: httpx.AsyncClient):
    """Check RSS Bridge instances health"""
    logger.info("Checking RSS Bridge instances health...")
    
    async def check(instance):
        try:
            # Test with a simple TwitterV2Bridge request
            test_url = f"{instance}?action=display&bridge=TwitterV2Bridge&q=test&format=Json"
            r = await http_client.get(test_url, timeout=10)
            if r.status_code == 200:
                logger.debug(f"✅ RSS Bridge healthy: {instance}")
                return instance
            else:
                logger.warning(f"❌ RSS Bridge {instance} returned status {r.status_code}")
                return None
        except Exception as e:
            logger.debug(f"❌ RSS Bridge {instance} failed: {e}")
            return None

    tasks = [check(i) for i in RSS_BRIDGE_INSTANCES]
    results = await asyncio.gather(*tasks)
    healthy = [r for r in results if r]
    
    global HEALTHY_RSS_BRIDGE_INSTANCES
    async with BRIDGE_CHECK_LOCK:
        HEALTHY_RSS_BRIDGE_INSTANCES = healthy
        
    logger.info(f"Healthy RSS Bridge instances: {len(healthy)}")
    if healthy:
        logger.info(f"Working instances: {healthy}")
    else:
        logger.error("No healthy RSS Bridge instances available!")
    
    return healthy

async def scan_rss_bridge_query(http_client: httpx.AsyncClient, queries: List[str], db_kind: str, tag: str):
    """Scan using RSS Bridge TwitterV2Bridge"""
    if not queries:
        logger.debug(f"Skipping {tag} scan (no queries provided).")
        return

    # Get healthy instances
    async with BRIDGE_CHECK_LOCK:
        instances = HEALTHY_RSS_BRIDGE_INSTANCES or RSS_BRIDGE_INSTANCES
        
    if not instances:
        logger.warning(f"No RSS Bridge instances for {tag} scan.")
        instances = await check_rss_bridge_health(http_client)
        if not instances:
            logger.error("Still no healthy instances after refresh. Skipping scan.")
            return

    for query in queries:
        success = False
        for instance in instances:
            try:
                # Use TwitterV2Bridge with JSON format for easier parsing
                url = f"{instance}?action=display&bridge=TwitterV2Bridge&q={urllib.parse.quote(query)}&format=Json"
                logger.info(f"Scanning RSS Bridge: {query}")
                
                r = await http_client.get(url, timeout=HTTP_TIMEOUT)
                
                if r.status_code == 200:
                    try:
                        # Try to parse as JSON first
                        data = r.json()
                        if 'items' in data:
                            items = data['items']
                        else:
                            # Fallback to RSS parsing
                            items = parse_rss_feed(r.text)
                    except:
                        # Fallback to RSS parsing if JSON fails
                        items = parse_rss_feed(r.text)
                    
                    if items:
                        logger.info(f"✅ Found {len(items)} results for: {query}")
                        
                        for item in items[:MAX_RESULTS]:
                            if not item.get('id'):
                                continue
                                
                            if is_spam(item.get('text', '')):
                                continue
                                
                            if await db.seen_add(f"{db_kind}:{item['id']}", db_kind, item):
                                # Construct a clean message
                                tweet_text = item.get('text') or item.get('title') or item.get('description', '')
                                msg = f"{tag} (Query: `{query}`)\n\n{tweet_text}\n\n🔗 [Link]({item['url']})"
                                await send_telegram_async(http_client, msg)
                                await asyncio.sleep(1)
                        
                        success = True
                        break  # Success with this instance, move to next query
                    else:
                        logger.debug(f"No results from RSS Bridge for query: {query}")
                else:
                    logger.warning(f"RSS Bridge {instance} returned {r.status_code} for query: {query}")
                    
            except Exception as e:
                logger.error(f"RSS Bridge error {instance}: {e}")
        
        # Delay between queries
        await asyncio.sleep(5)

# ----------------- MAIN SCHEDULERS -----------------
async def scheduler_loop(http_client: httpx.AsyncClient):
    logger.info("RSS Bridge scheduler started.")
    while True:
        try:
            custom_queries = await get_custom_nitter_queries()
            await scan_rss_bridge_query(http_client, custom_queries, "rss_bridge", "🐦 *RSS Bridge Airdrop*")
            
            count = await db.seen_count()
            logger.info(f"RSS Bridge scan cycle complete. DB Size: {count}")
        except Exception as e:
            logger.error(f"RSS Bridge scheduler error: {e}")
        await asyncio.sleep(POLL_INTERVAL_MINUTES * 60)

async def run_manual_scan(http_client: httpx.AsyncClient, chat_id: int, context):
    await context.bot.send_message(chat_id, "🚀 RSS Bridge manual scan initiated...")
    try:
        custom_queries = await get_custom_nitter_queries()
        await scan_rss_bridge_query(http_client, custom_queries, "rss_bridge", "🐦 *RSS Bridge Airdrop*")
        
        await context.bot.send_message(chat_id, "✅ RSS Bridge Manual Scan Complete.")
    except Exception as e:
        logger.error(f"RSS Bridge manual scan error: {e}")
        await context.bot.send_message(chat_id, f"⚠️ RSS Bridge Manual Scan Failed: {e}")

async def rss_bridge_health_loop(http_client: httpx.AsyncClient):
    while True:
        await check_rss_bridge_health(http_client)
        await asyncio.sleep(600)  # Check every 10 minutes

# ----------------------------------------------------------------------
## 4. 🤖 TELEGRAM BOT & UI LOGIC (UNCHANGED)
# ----------------------------------------------------------------------

# [The rest of your Telegram bot code remains exactly the same - only the backend scanning changed]
# Including: auth_guard, menu generators, command handlers, callback router, etc.

def is_authorized(chat_id: int) -> bool:
    return str(chat_id) == str(TELEGRAM_CHAT_ID)

async def auth_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        logger.warning(f"Unauthorized access attempt from chat_id: {chat_id}")
        if update.message:
            await update.message.reply_text("🚫 Unauthorized chat.")
        elif update.callback_query:
            await update.callback_query.answer("🚫 Unauthorized chat.", show_alert=True)
        return False
    return True

async def get_main_menu():
    text = f"✨ *{BOT_NAME} v{VERSION} | RSS Bridge Edition*\n\nChoose an option to manage the bot's operation:"
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
    async with BRIDGE_CHECK_LOCK:
        healthy_count = len(HEALTHY_RSS_BRIDGE_INSTANCES)
    
    text = (
        "📊 *Bot Statistics*\n\n"
        f"➡️ **Items Tracked:** `{count}`\n"
        f"➡️ **Custom Queries:** `{queries_count}`\n"
        f"➡️ **Spam Filters:** `{spam_count}`\n"
        f"➡️ **Healthy RSS Bridge Nodes:** `{healthy_count}`\n"
        f"➡️ **Version:** `{VERSION} (RSS Bridge Edition)`"
    )
    keyboard = [[InlineKeyboardButton("↩️ Back to Main Menu", callback_data="main_menu")]]
    return text, InlineKeyboardMarkup(keyboard)

# [Keep all your existing menu functions, command handlers, and callback router exactly as they are]
# Only replace the Nitter-specific functions with RSS Bridge equivalents

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update, context): return
    text, markup = await get_main_menu()
    await update.message.reply_text(
        text,
        reply_markup=markup,
        parse_mode=ParseMode.MARKDOWN
    )

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
            await load_spam_words()
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

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update, context): return
    
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data == "main_menu":
        text, markup = await get_main_menu()
    elif data == "stats":
        text, markup = await get_stats_menu()
    elif data == "queries_menu":
        text, markup = await get_queries_menu()
    elif data == "list_queries":
        text, markup = await get_list_queries_menu()
    elif data == "filters_menu":
        text, markup = await get_filters_menu()
    elif data == "list_filters":
        text, markup = await get_list_filters_menu()
    elif data == "add_query_input":
        text, markup = await get_command_input_menu("add_query")
    elif data == "del_query_input":
        text, markup = await get_command_input_menu("del_query")
    elif data == "add_filter_input":
        text, markup = await get_command_input_menu("add_filter")
    elif data == "del_filter_input":
        text, markup = await get_command_input_menu("del_filter")
    elif data == "scan_now":
        await query.edit_message_text("🚀 RSS Bridge scanning now... this may take a moment.")
        http_client = context.application.bot_data['http_client']
        asyncio.create_task(run_manual_scan(http_client, query.message.chat_id, context))
        return
    else:
        logger.warning(f"Unknown callback data: {data}")
        await query.answer("Unknown action.", show_alert=True)
        return

    await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)

async def get_queries_menu():
    text = "🐦 *Custom Twitter Queries*\n\nManage the search queries used for RSS Bridge scanning."
    kb = [
        [InlineKeyboardButton("📜 View All Queries", callback_data="list_queries")],
        [InlineKeyboardButton("➕ Add Query", callback_data="add_query_input"), InlineKeyboardButton("➖ Delete Query", callback_data="del_query_input")],
        [InlineKeyboardButton("↩️ Back to Main Menu", callback_data="main_menu")]
    ]
    return text, InlineKeyboardMarkup(kb)

async def get_list_queries_menu():
    queries = await get_custom_nitter_queries()
    text = "📜 *Active Twitter Search Queries:*\n\n" + "\n".join([f"• `{q}`" for q in queries])
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

async def get_command_input_menu(action: str):
    if action == "add_query":
        text = "➕ *Add Query*\n\nTo add a query, **type the command** below:\n\n`/addquery <full_twitter_search_query>`"
        back_data = "queries_menu"
    elif action == "del_query":
        text = "➖ *Delete Query*\n\nTo remove a query, **type the command** below:\n\n`/delquery <full_twitter_search_query>`"
        back_data = "queries_menu"
    elif action == "add_filter":
        text = "➕ *Add Filter*\n\nTo add a filter, **type the command** below:\n\n`/addfilter <word>`"
        back_data = "filters_menu"
    elif action == "del_filter":
        text = "➖ *Delete Filter*\n\nTo remove a filter, **type the command** below:\n\n`/delfilter <word>`"
        back_data = "filters_menu"
    else:
        return "Error: Unknown action.", InlineKeyboardMarkup([])

    kb = [[InlineKeyboardButton("🔙 Back to Menu", callback_data=back_data)]]
    return text, InlineKeyboardMarkup(kb)

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

        # Initial RSS Bridge health check
        await check_rss_bridge_health(http_client)

        # Start the web server
        asgi_server_task = asyncio.create_task(run_asgi_server())

        # Setup Telegram Bot
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        app.bot_data['http_client'] = http_client

        # --- Register Command Handlers ---
        app.add_handler(CommandHandler("start", start_cmd))
        app.add_handler(CommandHandler("addfilter", add_filter_cmd))
        app.add_handler(CommandHandler("delfilter", del_filter_cmd))
        app.add_handler(CommandHandler("addquery", add_query_cmd))
        app.add_handler(CommandHandler("delquery", del_query_cmd))
        app.add_handler(CallbackQueryHandler(callback_router))

        # --- Background Tasks ---
        health_checker = asyncio.create_task(rss_bridge_health_loop(http_client))
        scheduler = asyncio.create_task(scheduler_loop(http_client))

        logger.info(f"Bot {VERSION} (RSS Bridge Edition) initialized. Starting polling...")
        
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
