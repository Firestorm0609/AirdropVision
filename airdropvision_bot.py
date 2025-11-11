#!/usr/bin/env python3
"""
AirdropVision v1.0 — Fully Asynchronous & Secured Off-chain Bot
Features:
- Full Async Architecture: HTTPX context management and Uvicorn server integration.
- Security: Mandatory Chat ID authorization for all bot commands.
- Database: Column access by name (aiosqlite.Row) for readability.
- Sources: NFTCalendar, Nitter (Twitter)
- Anti-Spam: Keyword filtering (configurable via Telegram)
- Resilience: Dynamic Nitter instance health checking
- Database: Fully Async SQLite (aiosqlite)
- Interface: Inline buttons for controls
"""

import os
import json
import logging
import asyncio
import urllib.parse
from typing import Optional, List, Set
from datetime import datetime
from bs4 import BeautifulSoup
from flask import Flask # Still needed for the main app object
import uvicorn # REQUIRED: pip install uvicorn
import aiosqlite  # REQUIRED: pip install aiosqlite
import httpx # REQUIRED: pip install httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ----------------- CONFIG -----------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
# NOTE: TELEGRAM_CHAT_ID must be a string (e.g., "-123456789") for comparison
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") 
POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL", "10"))
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "25"))
BOT_NAME = os.environ.get("BOT_NAME", "AirdropVision")
VERSION = "4.0.0-Secured-ASGI"
DB_PATH = os.environ.get("DB_PATH", "airdropvision_v4.db")

# Sources Config
NFTCALENDAR_API = "https://api.nftcalendar.io/upcoming"

# Nitter Defaults
DEFAULT_NITTER_LIST = [
    "https://nitter.net",
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
    "https://nitter.lucabased.xyz",
    "https://nitter.uni-sonia.com",
]
NITTER_INSTANCES_CSV = os.environ.get("NITTER_INSTANCES_CSV")
NITTER_INSTANCES = [url.strip() for url in (NITTER_INSTANCES_CSV.split(',') if NITTER_INSTANCES_CSV else DEFAULT_NITTER_LIST) if url.strip()]

NITTER_SEARCH_QUERIES = [
    '("free mint" OR "free-mint") -filter:replies',
    '("solana airdrop" OR "sol free mint") -filter:replies',
    '("eth free mint") -filter:replies',
]

# Spam Defaults
DEFAULT_SPAM_KEYWORDS = "giveaway,retweet,follow,tag 3,like,rt,gleam.io,promo,dm me,whatsapp,telegram group"
SPAM_WORDS: Set[str] = set()  # Populated from DB at runtime

# Globals
HTTP_TIMEOUT = 15
HEALTHY_NITTER_INSTANCES = []
NITTER_CHECK_LOCK = asyncio.Lock()
# http_client removed from global scope

# ----------------- LOGGING -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(BOT_NAME)

# ----------------- ASYNC DATABASE (aiosqlite) - UPGRADED -----------------
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
        self._lock = asyncio.Lock()
        self._init_done = False

    async def init(self):
        if self._init_done: return
        async with self._lock:
            async with aiosqlite.connect(self.path) as conn:
                await conn.execute(CREATE_SEEN_SQL)
                await conn.execute(CREATE_META_SQL)
                conn.row_factory = aiosqlite.Row # UPGRADE: Enable column access by name
                await conn.commit()
        self._init_done = True

    async def seen_add(self, id: str, kind: str = "generic", meta: Optional[dict] = None) -> bool:
        if not self._init_done: await self.init()
        async with self._lock:
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
        if not self._init_done: await self.init()
        async with self._lock:
            async with aiosqlite.connect(self.path) as conn:
                async with conn.execute("SELECT COUNT(1) FROM seen") as cur:
                    row = await cur.fetchone()
                    return row[0] if row else 0 # count doesn't benefit from row_factory

    async def meta_get(self, k: str, default=None):
        if not self._init_done: await self.init()
        async with self._lock:
            async with aiosqlite.connect(self.path) as conn:
                conn.row_factory = aiosqlite.Row # Ensure row factory is set on connection
                async with conn.execute("SELECT v FROM meta WHERE k=?", (k,)) as cur:
                    row = await cur.fetchone()
                    return row['v'] if row else default # UPGRADE: Access by name 'v'

    async def meta_set(self, k: str, v: str):
        if not self._init_done: await self.init()
        async with self._lock:
            async with aiosqlite.connect(self.path) as conn:
                await conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES (?,?)", (k, v))
                await conn.commit()

    async def meta_get_json(self, k: str, default: list = []) -> list:
        val = await self.meta_get(k)
        if not val: return default
        try: return json.loads(val)
        except: return default

    async def meta_set_json(self, k: str, v: list):
        await self.meta_set(k, json.dumps(v))

db = DB()

# ----------------- HELPER: SPAM FILTER -----------------
async def load_spam_words():
    global SPAM_WORDS
    stored = await db.meta_get_json("spam_keywords", [])
    if not stored:
        stored = [s.strip().lower() for s in DEFAULT_SPAM_KEYWORDS.split(',') if s.strip()]
        await db.meta_set_json("spam_keywords", stored)
    SPAM_WORDS = set(stored)
    logger.info(f"Loaded {len(SPAM_WORDS)} spam filters.")

def is_spam(text: str) -> bool:
    if not text: return False
    text = text.lower()
    return any(w in text for w in SPAM_WORDS)

# ----------------- HELPER: TELEGRAM SENDER (Pass http_client) -----------------
async def send_telegram_async(http_client: httpx.AsyncClient, text: str, parse_mode=ParseMode.MARKDOWN):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": parse_mode, "disable_web_page_preview": False}
    
    for i in range(3):
        try:
            r = await http_client.post(url, json=payload)
            if r.status_code == 200: return
            if r.status_code == 429:
                await asyncio.sleep(5)
                continue
            logger.warning(f"Telegram API failed: {r.status_code} - {r.text}")
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
        await asyncio.sleep(1)

# ----------------- SCANNER: NITTER HEALTH CHECK (Pass http_client) -----------------
async def check_nitter_health(http_client: httpx.AsyncClient):
    global HEALTHY_NITTER_INSTANCES
    logger.info("Checking Nitter instances health...")
    
    async def check(instance):
        try:
            r = await http_client.get(f"{instance}/search?q=test", timeout=8)
            # Check for 200 and relevant content in the page source
            if r.status_code == 200 and ("timeline" in r.text or "tweet" in r.text):
                return instance
        except: pass
        return None

    tasks = [check(i) for i in NITTER_INSTANCES]
    results = await asyncio.gather(*tasks)
    healthy = [r for r in results if r]
    
    async with NITTER_CHECK_LOCK:
        HEALTHY_NITTER_INSTANCES = healthy
    logger.info(f"Healthy Nitter instances: {len(healthy)}")

async def nitter_health_loop(http_client: httpx.AsyncClient):
    while True:
        await check_nitter_health(http_client)
        await asyncio.sleep(1800) # 30 mins

# ----------------- SCANNER: NITTER (Pass http_client) -----------------
def parse_nitter(html: str, base_url: str):
    # This remains synchronous and uses to_thread correctly
    soup = BeautifulSoup(html, "lxml")
    results = []
    items = soup.find_all("div", class_="timeline-item")
    for item in items:
        # ... (parsing logic remains the same)
        link = item.find("a", class_="tweet-link")
        if not link: continue
        
        href = link.get("href", "")
        tweet_id = href.split("/")[-1].replace("#m", "")
        url = urllib.parse.urljoin(base_url, href)
        
        content = item.find("div", class_="tweet-content")
        text = content.get_text(" ", strip=True) if content else ""
        
        user_a = item.find("a", class_="username")
        user = user_a.get_text(strip=True) if user_a else "???"
        
        results.append({"id": tweet_id, "url": url, "text": text, "user": user})
    return results

async def scan_nitter(http_client: httpx.AsyncClient, limit=10):
    async with NITTER_CHECK_LOCK:
        instances = HEALTHY_NITTER_INSTANCES or NITTER_INSTANCES
    
    if not instances:
        logger.warning("No Nitter instances available.")
        return

    for q in NITTER_SEARCH_QUERIES:
        for instance in instances:
            try:
                url = f"{instance}/search?f=tweets&q={urllib.parse.quote(q)}"
                r = await http_client.get(url)
                if r.status_code != 200: continue
                
                # Use asyncio.to_thread for blocking BeautifulSoup
                parsed = await asyncio.to_thread(parse_nitter, r.text, instance)
                if not parsed: continue
                
                for t in parsed[:limit]:
                    if is_spam(t['text']):
                        logger.info(f"Spam skipped: {t['id']}")
                        continue
                        
                    if await db.seen_add(f"nitter:{t['id']}", "nitter", t):
                        msg = f"🐦 *Twitter/Nitter*\n\n{t['text']}\n\n🔗 [Link]({t['url']})"
                        await send_telegram_async(http_client, msg)
                        await asyncio.sleep(0.5)
                break 
            except Exception as e:
                logger.debug(f"Nitter error {instance}: {e}")
        await asyncio.sleep(2)

# ----------------- SCANNER: NFT CALENDAR (Pass http_client) -----------------
async def scan_calendar(http_client: httpx.AsyncClient, limit=MAX_RESULTS):
    try:
        r = await http_client.get(NFTCALENDAR_API)
        if r.status_code != 200: return
        data = r.json()
        items = data if isinstance(data, list) else (data.get("nfts") or data.get("data") or [])
        
        for nft in items[:limit]:
            if not isinstance(nft, dict): continue
            
            nid = str(nft.get("id") or nft.get("slug") or nft.get("name"))
            name = nft.get("name", "Unknown")
            desc = nft.get("description", "")
            
            if is_spam(name + " " + desc): continue
            
            price = str(nft.get("mint_price", "")).lower()
            is_free = "free" in price or "0" == price.strip() or nft.get("is_free")
            
            if not is_free and "free" not in (name+desc).lower():
                continue 
                
            if await db.seen_add(f"cal:{nid}", "calendar", nft):
                url = nft.get("url") or nft.get("link") or "N/A"
                date = nft.get("launch_date")
                msg = f"🗓 *NFT Calendar*\n\n*{name}*\nPrice: {price}\nDate: {date}\n\n🔗 [Link]({url})"
                await send_telegram_async(http_client, msg)
    except Exception as e:
        logger.error(f"Calendar scan error: {e}")

# ----------------- MAIN SCHEDULER (Pass http_client) -----------------
async def scheduler_loop(http_client: httpx.AsyncClient):
    logger.info("Scheduler started.")
    while True:
        try:
            await scan_calendar(http_client)
            await scan_nitter(http_client)
            count = await db.seen_count()
            logger.info(f"Scan cycle complete. DB Size: {count}")
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
        await asyncio.sleep(POLL_INTERVAL_MINUTES * 60)

# ----------------- TELEGRAM UI & SECURITY - UPGRADED -----------------

def is_authorized(chat_id: int) -> bool:
    """UPGRADE: Check if the sender's chat ID matches the configured ID."""
    return str(chat_id) == str(TELEGRAM_CHAT_ID)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id): 
        return await update.message.reply_text("🚫 Unauthorized chat.")
    
    keyboard = [
        [InlineKeyboardButton("📊 Stats", callback_data="stats"), InlineKeyboardButton("⚙️ Filters", callback_data="filters")],
        [InlineKeyboardButton("🚀 Force Scan", callback_data="scan_now")]
    ]
    await update.message.reply_text(
        f"🤖 *{BOT_NAME} v{VERSION}*\nActive & Scanning...", 
        reply_markup=InlineKeyboardMarkup(keyboard), 
        parse_mode=ParseMode.MARKDOWN
    )

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_authorized(query.message.chat_id): 
        return await query.edit_message_text("🚫 Unauthorized chat.")

    data = query.data
    http_client = context.application.bot_data['http_client'] # Retrieve client from context

    # ... (rest of the menu_handler logic remains similar, but now implicitly authorized)

    if data == "stats":
        count = await db.seen_count()
        spam_count = len(SPAM_WORDS)
        healthy_count = 0
        async with NITTER_CHECK_LOCK:
            healthy_count = len(HEALTHY_NITTER_INSTANCES)
            
        msg = f"📊 *Statistics*\n\nItems Tracked: `{count}`\nSpam Filters: `{spam_count}`\nNitter Nodes: `{healthy_count}`"
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

    elif data == "filters":
        kb = [
            [InlineKeyboardButton("📜 List Filters", callback_data="list_filters")],
            [InlineKeyboardButton("➕ Add Filter", callback_data="add_filter_info"), InlineKeyboardButton("➖ Del Filter", callback_data="del_filter_info")],
            [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
        ]
        await query.edit_message_text("⚙️ *Filter Settings*\nManage your anti-spam keywords here.", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

    elif data == "list_filters":
        await load_spam_words()
        text = "🚫 *Blocked Keywords:*\n\n" + "\n".join([f"- `{w}`" for w in sorted(list(SPAM_WORDS))])
        if not SPAM_WORDS: text = "No filters set."
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="filters")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

    elif data == "add_filter_info":
        text = "To *ADD* a filter, type the command below in the chat:\n\n`/addfilter <word>`\n\nExample: `/addfilter giveaway`"
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="filters")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

    elif data == "del_filter_info":
        text = "To *REMOVE* a filter, type the command below in the chat:\n\n`/delfilter <word>`\n\nExample: `/delfilter giveaway`"
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="filters")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

    elif data == "scan_now":
        await query.edit_message_text("🚀 Scanning now... please wait.")
        # Pass http_client to manual scan
        asyncio.create_task(run_manual_scan(http_client, query.message.chat_id, context))
    
    elif data == "main_menu":
        keyboard = [
            [InlineKeyboardButton("📊 Stats", callback_data="stats"), InlineKeyboardButton("⚙️ Filters", callback_data="filters")],
            [InlineKeyboardButton("🚀 Force Scan", callback_data="scan_now")]
        ]
        await query.edit_message_text(f"🤖 *{BOT_NAME} v{VERSION}*\nActive & Scanning...", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)


async def run_manual_scan(http_client: httpx.AsyncClient, chat_id, context):
    await scan_calendar(http_client)
    await scan_nitter(http_client)
    await context.bot.send_message(chat_id, "✅ Manual Scan Complete.")

# --- Command Handlers for Filters ---
async def add_filter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id): 
        return await update.message.reply_text("🚫 Unauthorized command.")
    
    if not context.args: return await update.message.reply_text("Usage: `/addfilter <word>`", parse_mode=ParseMode.MARKDOWN)
    word = " ".join(context.args).lower().strip()
    await load_spam_words()
    current = list(SPAM_WORDS)
    if word not in current:
        current.append(word)
        await db.meta_set_json("spam_keywords", current)
        await load_spam_words()
        await update.message.reply_text(f"✅ Added filter: `{word}`", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"⚠️ Filter `{word}` already exists.", parse_mode=ParseMode.MARKDOWN)

async def del_filter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id): 
        return await update.message.reply_text("🚫 Unauthorized command.")

    if not context.args: return await update.message.reply_text("Usage: `/delfilter <word>`", parse_mode=ParseMode.MARKDOWN)
    word = " ".join(context.args).lower().strip()
    await load_spam_words()
    current = list(SPAM_WORDS)
    if word in current:
        current.remove(word)
        await db.meta_set_json("spam_keywords", current)
        await load_spam_words()
        await update.message.reply_text(f"🗑 Removed filter: `{word}`", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"⚠️ Filter `{word}` not found.", parse_mode=ParseMode.MARKDOWN)

# ----------------- FLASK & UVICORN (ASGI) HEALTHCHECK - UPGRADED -----------------
flask_app = Flask(__name__)
@flask_app.route("/health")
def health(): return f"AirdropVision v{VERSION} OK", 200

# Function to run Uvicorn server in the asyncio loop
async def run_asgi_server():
    """Runs the Flask app using Uvicorn as an ASGI server."""
    port = int(os.environ.get("PORT", 8080))
    config = uvicorn.Config(
        flask_app, 
        host="0.0.0.0", 
        port=port, 
        log_level="warning", 
        loop="asyncio"
    )
    server = uvicorn.Server(config)
    
    logger.info(f"Starting ASGI server on port {port}...")
    await server.serve()

# ----------------- BOOTSTRAP -----------------
async def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Error: TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set.")
        return

    # UPGRADE: httpx.AsyncClient as a context manager
    async with httpx.AsyncClient(
        headers={"User-Agent": f"Mozilla/5.0 (compatible; {BOT_NAME}/{VERSION})"},
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
    ) as http_client:

        # Init DB and config
        await db.init()
        await load_spam_words()

        # UPGRADE: ASGI Server as an asyncio Task
        asgi_server_task = asyncio.create_task(run_asgi_server())

        # Telegram App Setup
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        # UPGRADE: Store http_client in bot_data for use in handlers (e.g., scan_now)
        app.bot_data['http_client'] = http_client

        app.add_handler(CommandHandler("start", start_cmd))
        app.add_handler(CommandHandler("addfilter", add_filter_cmd))
        app.add_handler(CommandHandler("delfilter", del_filter_cmd))
        app.add_handler(CallbackQueryHandler(menu_handler))

        # Background Tasks (Pass http_client explicitly)
        health_checker = asyncio.create_task(nitter_health_loop(http_client))
        scheduler = asyncio.create_task(scheduler_loop(http_client))

        # Start Bot
        logger.info(f"Bot {VERSION} initialized. Polling...")
        
        await app.initialize()
        await app.updater.start_polling()
        await app.start()
        
        # Keep main alive until interrupted
        try:
            await asyncio.gather(health_checker, scheduler, asgi_server_task, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Main loop error: {e}")
            
        finally:
            logger.info("Stopping components...")
            # Cleanup
            scheduler.cancel()
            health_checker.cancel()
            asgi_server_task.cancel()
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
            # httpx.AsyncClient is closed by the `async with` block

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Program exited gracefully.")
        pass


