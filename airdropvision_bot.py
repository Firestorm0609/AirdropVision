#!/usr/bin/env python3
"""
AirdropVision v1.1 — FULL INLINE COMMANDS EDITION
Changes from v1.0:
 - Replaced sqlite DB with an async JSON DB (db.json) as requested.
 - Proper rotation across Nitter instances with persistent rotation index.
 - Added Settings submenu with rotate/view/reset rotation inline actions.
 - Added CLI commands: /nitterindex and /resetindex
"""

import logging
import asyncio
import httpx
import uvicorn
import os
import json
import urllib.parse
from typing import List, Dict, Set, Optional
from datetime import datetime

# External dependencies
import aiofiles
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
VERSION = "1.1.0"
DB_PATH = os.environ.get("DB_PATH", "db.json")  # now storing JSON DB file
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
DEFAULT_SPAM_KEYWORDS = ""  # start empty by default

# --- Global State & Locks ---
SPAM_WORDS: Set[str] = set()
HEALTHY_NITTER_INSTANCES: List[str] = []
NITTER_CHECK_LOCK = asyncio.Lock()
FILTER_LOCK = asyncio.Lock()
QUERY_LOCK = asyncio.Lock()

# rotation index (persisted in DB under meta key "nitter_rotation_index")
CURRENT_NITTER_INDEX = 0
ROTATION_PERSIST_LOCK = asyncio.Lock()

# ----------------- LOGGING SETUP -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
)
logger = logging.getLogger(BOT_NAME)


# ----------------------------------------------------------------------
## 2. 🗃️ ASYNC JSON DB LOGIC (db.json)
# ----------------------------------------------------------------------
# Structure:
# {
#   "seen": { "<id>": {"kind": "...", "meta": {...}, "created_at": "..."} },
#   "meta": { "k": "v", ... }
# }

class JSONDB:
    def __init__(self, path=DB_PATH):
        self.path = path
        self._init_done = False
        self._lock = asyncio.Lock()  # protects reads/writes

    async def init(self):
        if self._init_done:
            return
        async with self._lock:
            if self._init_done:
                return
            # ensure file exists and has minimal structure
            if not os.path.exists(self.path):
                initial = {"seen": {}, "meta": {}}
                async with aiofiles.open(self.path, "w") as f:
                    await f.write(json.dumps(initial))
            else:
                # ensure keys exist
                try:
                    async with aiofiles.open(self.path, "r") as f:
                        raw = await f.read()
                        data = json.loads(raw) if raw.strip() else {}
                except Exception:
                    data = {}
                changed = False
                if "seen" not in data:
                    data["seen"] = {}
                    changed = True
                if "meta" not in data:
                    data["meta"] = {}
                    changed = True
                if changed:
                    async with aiofiles.open(self.path, "w") as f:
                        await f.write(json.dumps(data))
            self._init_done = True
            logger.info("JSON DB initialized.")

    async def _read(self) -> dict:
        await self.init()
        async with self._lock:
            try:
                async with aiofiles.open(self.path, "r") as f:
                    raw = await f.read()
                    return json.loads(raw) if raw.strip() else {"seen": {}, "meta": {}}
            except FileNotFoundError:
                return {"seen": {}, "meta": {}}
            except Exception as e:
                logger.error(f"Failed to read DB file: {e}")
                return {"seen": {}, "meta": {}}

    async def _write(self, data: dict):
        async with self._lock:
            async with aiofiles.open(self.path, "w") as f:
                await f.write(json.dumps(data))

    # seen operations
    async def seen_add(self, id: str, kind: str = "generic", meta: Optional[dict] = None) -> bool:
        data = await self._read()
        seen = data.get("seen", {})
        if id in seen:
            return False
        seen[id] = {
            "kind": kind,
            "meta": meta or {},
            "created_at": datetime.utcnow().isoformat() + "Z"
        }
        data["seen"] = seen
        await self._write(data)
        return True

    async def seen_count(self) -> int:
        data = await self._read()
        return len(data.get("seen", {}))

    # meta operations (string)
    async def meta_get(self, k: str, default=None):
        data = await self._read()
        return data.get("meta", {}).get(k, default)

    async def meta_set(self, k: str, v: str):
        data = await self._read()
        meta = data.get("meta", {})
        meta[k] = v
        data["meta"] = meta
        await self._write(data)

    # meta json convenience
    async def meta_get_json(self, k: str, default: dict = {}):
        val = await self.meta_get(k, None)
        if val is None:
            return default
        try:
            return json.loads(val)
        except Exception:
            return default

    async def meta_set_json(self, k: str, v: dict):
        await self.meta_set(k, json.dumps(v))


# Create a single, shared instance
db = JSONDB()


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
            # simple retry/backoff
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
        # Load from DEFAULT_SPAM_KEYWORDS only if DB is empty
        stored = [s.strip().lower() for s in DEFAULT_SPAM_KEYWORDS.split(',') if s.strip()]
        if stored:
            await db.meta_set_json("spam_keywords", stored)

    global SPAM_WORDS
    SPAM_WORDS = set(stored)
    logger.info(f"Loaded {len(SPAM_WORDS)} spam filters.")

def is_spam(text: str) -> bool:
    if not text:
        return False
    text_low = text.lower()
    return any(w in text_low for w in SPAM_WORDS)

async def get_custom_nitter_queries() -> List[str]:
    """Retrieves custom queries from DB, setting defaults if none exist."""
    stored = await db.meta_get_json("custom_nitter_queries", [])
    if not stored:
        await db.meta_set_json("custom_nitter_queries", DEFAULT_CUSTOM_QUERIES)
        return DEFAULT_CUSTOM_QUERIES
    return stored

# ----------------- NITTER/TWITTER -----------------
def parse_nitter(html: str, base_url: str):
    soup = BeautifulSoup(html, "lxml")
    results = []
    items = soup.find_all("div", class_="timeline-item")
    for item in items:
        link = item.find("a", class_="tweet-link")
        if not link:
            continue
        href = link.get("href", "")
        tweet_id = href.split("/")[-1].replace("#m", "")
        url = urllib.parse.urljoin(base_url, href)
        content = item.find("div", class_="tweet-content")
        text = content.get_text(" ", strip=True) if content else ""
        results.append({"id": tweet_id, "url": url, "text": text})
    return results

async def check_nitter_health(http_client: httpx.AsyncClient):
    logger.info("Checking Nitter instances health...")
    async def check(instance):
        try:
            r = await http_client.get(f"{instance}/search?q=test", timeout=8)
            if r.status_code == 200 and ("timeline" in r.text or "tweet" in r.text):
                return instance
        except Exception:
            pass
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
        await asyncio.sleep(1800)  # 30 mins

# ----------------- Rotation persistence helpers -----------------
async def load_rotation_index():
    """Load rotation index from DB into CURRENT_NITTER_INDEX."""
    global CURRENT_NITTER_INDEX
    val = await db.meta_get("nitter_rotation_index", "0")
    try:
        CURRENT_NITTER_INDEX = int(val) if val is not None else 0
    except Exception:
        CURRENT_NITTER_INDEX = 0
    logger.info(f"Loaded Nitter rotation index: {CURRENT_NITTER_INDEX}")

async def persist_rotation_index():
    """Persist CURRENT_NITTER_INDEX into the DB."""
    global CURRENT_NITTER_INDEX
    async with ROTATION_PERSIST_LOCK:
        try:
            await db.meta_set("nitter_rotation_index", str(int(CURRENT_NITTER_INDEX)))
        except Exception as e:
            logger.warning(f"Failed to persist rotation index: {e}")

# ----------------- Fixed: rotating scan_nitter_query -----------------
async def scan_nitter_query(http_client: httpx.AsyncClient, queries: List[str], db_kind: str, tag: str):
    """
    Runs a Nitter search scan for each query, rotating between healthy instances.
    Persist rotation index after each query's attempts so restarts resume rotation.
    """
    global CURRENT_NITTER_INDEX

    if not queries:
        logger.debug(f"Skipping {tag} scan (no queries provided).")
        return

    async with NITTER_CHECK_LOCK:
        instances = HEALTHY_NITTER_INSTANCES or NITTER_INSTANCES

    if not instances:
        logger.warning(f"No Nitter instances available for {tag} scan.")
        return

    total_instances = len(instances)

    for q in queries:
        # Ensure rotation index is within bounds
        if total_instances <= 0:
            logger.warning("No instances found (post-check).")
            return

        start_idx = CURRENT_NITTER_INDEX % total_instances
        success = False

        for attempt in range(total_instances):
            instance = instances[(start_idx + attempt) % total_instances]
            try:
                url = f"{instance}/search?f=tweets&q={urllib.parse.quote(q)}"
                r = await http_client.get(url, timeout=HTTP_TIMEOUT)

                if r.status_code != 200:
                    logger.debug(f"{instance} returned {r.status_code} for {q}")
                    continue

                parsed = await asyncio.to_thread(parse_nitter, r.text, instance)
                if not parsed:
                    logger.debug(f"{instance} returned no results for query {q}")
                    continue

                found = 0
                for t in parsed[:MAX_RESULTS]:
                    if is_spam(t['text']):
                        continue
                    if await db.seen_add(f"{db_kind}:{t['id']}", db_kind, t):
                        msg = f"{tag} (Query: `{q}`)\n\n{t['text']}\n\n🔗 [Link]({t['url']})"
                        await send_telegram_async(http_client, msg)
                        found += 1
                        await asyncio.sleep(0.5)

                logger.info(f"[{tag}] {instance} — {found} new results for query: {q}")
                success = True
                break  # Success — stop trying other instances for this query

            except Exception as e:
                logger.debug(f"Nitter error ({instance}): {e}")

        # Advance the rotation index globally for next run and persist it
        CURRENT_NITTER_INDEX = (CURRENT_NITTER_INDEX + 1) % total_instances
        await persist_rotation_index()

        if not success:
            logger.warning(f"All Nitter instances failed for query: {q}")

        await asyncio.sleep(2)

# ----------------- MAIN SCHEDULERS -----------------
async def scheduler_loop(http_client: httpx.AsyncClient):
    logger.info("Scheduler started.")
    while True:
        try:
            custom_queries = await get_custom_nitter_queries()
            await scan_nitter_query(http_client, custom_queries, "custom_nitter", "🐦 *Custom Query Airdrop*")

            count = await db.seen_count()
            logger.info(f"Scan cycle complete. DB Size: {count}")
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
        await asyncio.sleep(POLL_INTERVAL_MINUTES * 60)

async def run_manual_scan(http_client: httpx.AsyncClient, chat_id: int, context):
    await context.bot.send_message(chat_id, "🚀 Manual scan initiated...")
    try:
        custom_queries = await get_custom_nitter_queries()
        await scan_nitter_query(http_client, custom_queries, "custom_nitter", "🐦 *Custom Query Airdrop*")

        await context.bot.send_message(chat_id, "✅ Manual Scan Complete.")
    except Exception as e:
        logger.error(f"Manual scan error: {e}")
        await context.bot.send_message(chat_id, f"⚠️ Manual Scan Failed: {e}")


# ----------------------------------------------------------------------
## 4. 🤖 TELEGRAM BOT & UI LOGIC (with Settings submenu)
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
        [InlineKeyboardButton("🐦 Custom Queries", callback_data="queries_menu")],
        [InlineKeyboardButton("🚫 Spam Filters", callback_data="filters_menu")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings_menu")],
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
    if not queries:
        text = "No custom queries set."

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
    if not SPAM_WORDS:
        text = "No filters set."

    kb = [[InlineKeyboardButton("🔙 Back to Filters Menu", callback_data="filters_menu")]]
    return text, InlineKeyboardMarkup(kb)

async def get_settings_menu():
    """
    Settings submenu: rotate, view, reset rotation index (inline).
    """
    async with NITTER_CHECK_LOCK:
        instances = HEALTHY_NITTER_INSTANCES or NITTER_INSTANCES
    instance_count = len(instances)

    # Determine currently indexed instance for display
    global CURRENT_NITTER_INDEX
    current_instance = instances[CURRENT_NITTER_INDEX % instance_count] if instance_count > 0 else "N/A"

    text = (
        "⚙️ *Settings*\n\n"
        f"➡️ **Rotation Index:** `{CURRENT_NITTER_INDEX}`\n"
        f"➡️ **Current Instance (based on index):** `{current_instance}`\n\n"
        "Use the buttons below to rotate or reset the Nitter rotation index."
    )
    kb = [
        [InlineKeyboardButton("🔄 Rotate Nitter Instance", callback_data="rotate_nitter")],
        [InlineKeyboardButton("🔍 View Rotation Index", callback_data="view_nitter_index"), InlineKeyboardButton("♻️ Reset Rotation Index", callback_data="reset_nitter_index")],
        [InlineKeyboardButton("↩️ Back to Main Menu", callback_data="main_menu")]
    ]
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

# ----------------- COMMAND HANDLERS -----------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update, context):
        return
    text, markup = await get_main_menu()
    await update.message.reply_text(
        text,
        reply_markup=markup,
        parse_mode=ParseMode.MARKDOWN
    )

# Configuration commands are kept for ease of use and are the required input method
async def add_filter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update, context):
        return
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
    if not await auth_guard(update, context):
        return
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
    if not await auth_guard(update, context):
        return
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
    if not await auth_guard(update, context):
        return
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

# ----------------- Rotation CLI Commands -----------------
async def nitterindex_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update, context):
        return
    async with NITTER_CHECK_LOCK:
        instances = HEALTHY_NITTER_INSTANCES or NITTER_INSTANCES
    total = len(instances)
    global CURRENT_NITTER_INDEX
    cur_idx = CURRENT_NITTER_INDEX % total if total > 0 else -1
    cur_inst = instances[cur_idx] if total > 0 else "N/A"
    await update.message.reply_text(f"🔢 Rotation Index: `{CURRENT_NITTER_INDEX}`\n\nCurrent instance: `{cur_inst}`", parse_mode=ParseMode.MARKDOWN)

async def resetindex_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update, context):
        return
    global CURRENT_NITTER_INDEX
    CURRENT_NITTER_INDEX = 0
    await persist_rotation_index()
    await update.message.reply_text("♻️ Rotation index reset to `0`.", parse_mode=ParseMode.MARKDOWN)

# ----------------- CALLBACK ROUTER (handles Settings actions) -----------------
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update, context):
        return

    query = update.callback_query
    await query.answer()

    data = query.data

    # Menu Navigation
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
    elif data == "settings_menu":
        text, markup = await get_settings_menu()

    # Command Input Screens (NEW)
    elif data == "add_query_input":
        text, markup = await get_command_input_menu("add_query")
    elif data == "del_query_input":
        text, markup = await get_command_input_menu("del_query")
    elif data == "add_filter_input":
        text, markup = await get_command_input_menu("add_filter")
    elif data == "del_filter_input":
        text, markup = await get_command_input_menu("del_filter")

    # Settings actions
    elif data == "rotate_nitter":
        global CURRENT_NITTER_INDEX
        # rotate by advancing index, persist, and show the selected instance
        async with NITTER_CHECK_LOCK:
            instances = HEALTHY_NITTER_INSTANCES or NITTER_INSTANCES
        if not instances:
            await query.edit_message_text("⚠️ No Nitter instances available to rotate.", parse_mode=ParseMode.MARKDOWN)
            return
        CURRENT_NITTER_INDEX = (CURRENT_NITTER_INDEX + 1) % len(instances)
        await persist_rotation_index()
        current_instance = instances[CURRENT_NITTER_INDEX]
        text = f"🔄 Rotated Nitter index to `{CURRENT_NITTER_INDEX}`\n\nCurrent instance: `{current_instance}`"
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back to Settings", callback_data="settings_menu")]])
        await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
        return

    elif data == "view_nitter_index":
        global CURRENT_NITTER_INDEX
        async with NITTER_CHECK_LOCK:
            instances = HEALTHY_NITTER_INSTANCES or NITTER_INSTANCES
        if not instances:
            await query.edit_message_text("⚠️ No Nitter instances available.", parse_mode=ParseMode.MARKDOWN)
            return
        cur_idx = CURRENT_NITTER_INDEX % len(instances)
        current_instance = instances[cur_idx]
        text = f"🔢 Rotation Index: `{CURRENT_NITTER_INDEX}`\n\nCurrent instance: `{current_instance}`"
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back to Settings", callback_data="settings_menu")]])
        await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
        return

    elif data == "reset_nitter_index":
        global CURRENT_NITTER_INDEX
        CURRENT_NITTER_INDEX = 0
        await persist_rotation_index()
        text = "♻️ Rotation index reset to `0`."
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back to Settings", callback_data="settings_menu")]])
        await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
        return

    # Actions
    elif data == "scan_now":
        # keep the edit message quick and spawn the manual scan
        await query.edit_message_text("🚀 Scanning now... this may take a moment.")
        http_client = context.application.bot_data['http_client']
        # spawn manual scan as a background task so callback returns quickly
        asyncio.create_task(run_manual_scan(http_client, query.message.chat_id, context))
        return  # Do not edit message text after starting background task

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
        await load_spam_words()  # Will load from DB or an empty list (since default is empty)
        await get_custom_nitter_queries()

        # Load persisted rotation index (so restarts resume rotation)
        await load_rotation_index()

        # Start the web server
        asgi_server_task = asyncio.create_task(run_asgi_server())

        # Setup Telegram Bot
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        app.bot_data['http_client'] = http_client

        # --- Register Command Handlers (Input Required) ---
        app.add_handler(CommandHandler("start", start_cmd))
        app.add_handler(CommandHandler("addfilter", add_filter_cmd))
        app.add_handler(CommandHandler("delfilter", del_filter_cmd))
        app.add_handler(CommandHandler("addquery", add_query_cmd))
        app.add_handler(CommandHandler("delquery", del_query_cmd))

        # rotation commands
        app.add_handler(CommandHandler("nitterindex", nitterindex_cmd))
        app.add_handler(CommandHandler("resetindex", resetindex_cmd))

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
