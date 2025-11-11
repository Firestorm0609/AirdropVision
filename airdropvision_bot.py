#!/usr/bin/env python3
"""
AirdropVision v1.0 — Fully Asynchronous & Secured Off-chain Bot
- File name: airdropvision_bot.py
- Adds: Poker mini-game (single-player video-poker style) and Scholarship scanner (looks for "fully funded" scholarships on free sources)
- No paid APIs required — only uses HTTP scraping with httpx and BeautifulSoup
"""

import os
import json
import logging
import asyncio
import random
import urllib.parse
from typing import Optional, List, Set, Dict
from datetime import datetime
from bs4 import BeautifulSoup
from flask import Flask
import uvicorn
import aiosqlite
import httpx
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
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL", "10"))
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "25"))
BOT_NAME = os.environ.get("BOT_NAME", "AirdropVision")
VERSION = "1.0.0-Secured-ASGI"
DB_PATH = os.environ.get("DB_PATH", "airdropvision_v4.db")

# Sources Config
NFTCALENDAR_API = "https://api.nftcalendar.io/upcoming"

# Nitter Defaults - UPDATED to include user-provided instances
DEFAULT_NITTER_LIST = [
    "https://nitter.tiekoetter.com",
    "https://nitter.space",
    "https://lightbrd.com",
    "https://xcancel.com",
    "https://nuku.trabun.org",
    "https://nitter.privacyredirect.com",
    "https://nitter.net",
    "https://nitter.poast.org",
]
NITTER_INSTANCES_CSV = os.environ.get("NITTER_INSTANCES_CSV")
NITTER_INSTANCES = [url.strip() for url in (NITTER_INSTANCES_CSV.split(',') if NITTER_INSTANCES_CSV else DEFAULT_NITTER_LIST) if url.strip()]

NITTER_SEARCH_QUERIES = [
    '("free mint" OR "free-mint") -filter:replies',
    '("solana airdrop" OR "sol free mint") -filter:replies',
    '("eth free mint") -filter:replies',
]

# Scholarship scanner default sources (free sites / pages). You can add more with /addschsource
DEFAULT_SCHOLARSHIP_SOURCES = [
    "https://www.scholarship-positions.com/",
    "https://www.scholarshipsads.com/",
    "https://www.internships.com/"  # generic fallback - results may vary
]

# Spam Defaults
DEFAULT_SPAM_KEYWORDS = "giveaway,retweet,follow,tag 3,like,rt,gleam.io,promo,dm me,whatsapp,telegram group"
SPAM_WORDS: Set[str] = set()

# Globals
HTTP_TIMEOUT = 15
HEALTHY_NITTER_INSTANCES: List[str] = []
NITTER_CHECK_LOCK = asyncio.Lock()

# ----------------- LOGGING -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(BOT_NAME)

# ----------------- ASYNC DATABASE (aiosqlite) -----------------
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
                conn.row_factory = aiosqlite.Row
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
                    return row[0] if row else 0

    async def meta_get(self, k: str, default=None):
        if not self._init_done: await self.init()
        async with self._lock:
            async with aiosqlite.connect(self.path) as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.execute("SELECT v FROM meta WHERE k=?", (k,)) as cur:
                    row = await cur.fetchone()
                    return row['v'] if row else default

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

# ----------------- HELPER: TELEGRAM SENDER -----------------
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

# ----------------- NITTER HEALTH & SCAN (unchanged behavior) -----------------
async def check_nitter_health(http_client: httpx.AsyncClient):
    global HEALTHY_NITTER_INSTANCES
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

    async with NITTER_CHECK_LOCK:
        HEALTHY_NITTER_INSTANCES = healthy
    logger.info(f"Healthy Nitter instances: {len(healthy)}")

async def nitter_health_loop(http_client: httpx.AsyncClient):
    while True:
        await check_nitter_health(http_client)
        await asyncio.sleep(1800)

# parsing helper for nitter (kept simple)
def parse_nitter(html: str, base_url: str):
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

# ----------------- NFT CALENDAR SCAN (unchanged) -----------------
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

# ----------------- SCHEDULER -----------------
async def scheduler_loop(http_client: httpx.AsyncClient):
    logger.info("Scheduler started.")
    while True:
        try:
            await scan_calendar(http_client)
            await scan_nitter(http_client)
            await scan_scholarships(http_client)
            count = await db.seen_count()
            logger.info(f"Scan cycle complete. DB Size: {count}")
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
        await asyncio.sleep(POLL_INTERVAL_MINUTES * 60)

# ----------------- TELEGRAM UI & AUTH -----------------

def is_authorized(chat_id: int) -> bool:
    return str(chat_id) == str(TELEGRAM_CHAT_ID)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        return await update.message.reply_text("🚫 Unauthorized chat.")
    keyboard = [
        [InlineKeyboardButton("📊 Stats", callback_data="stats"), InlineKeyboardButton("⚙️ Filters", callback_data="filters")],
        [InlineKeyboardButton("🚀 Force Scan", callback_data="scan_now"), InlineKeyboardButton("🃏 Poker", callback_data="poker_start")]
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
    http_client = context.application.bot_data['http_client']

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
        asyncio.create_task(run_manual_scan(http_client, query.message.chat_id, context))

    elif data == "poker_start":
        await query.edit_message_text("🃏 Starting Poker (Video Poker - single player)...")
        asyncio.create_task(start_poker_game(context, query.message.chat_id))

    elif data == "main_menu":
        keyboard = [
            [InlineKeyboardButton("📊 Stats", callback_data="stats"), InlineKeyboardButton("⚙️ Filters", callback_data="filters")],
            [InlineKeyboardButton("🚀 Force Scan", callback_data="scan_now"), InlineKeyboardButton("🃏 Poker", callback_data="poker_start")]
        ]
        await query.edit_message_text(f"🤖 *{BOT_NAME} v{VERSION}*\nActive & Scanning...", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

# ----------------- MANUAL SCAN TRIGGER -----------------
async def run_manual_scan(http_client: httpx.AsyncClient, chat_id, context):
    await scan_calendar(http_client)
    await scan_nitter(http_client)
    await scan_scholarships(http_client)
    await context.bot.send_message(chat_id, "✅ Manual Scan Complete.")

# ----------------- FILTER COMMANDS -----------------
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

# ----------------- FLASK & UVICORN -----------------
flask_app = Flask(__name__)
@flask_app.route("/health")
def health(): return f"AirdropVision v{VERSION} OK", 200

async def run_asgi_server():
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

# ----------------- POKER MINI-GAME (Video Poker style) -----------------
RANKS = ['2','3','4','5','6','7','8','9','10','J','Q','K','A']
SUITS = ['♠','♥','♦','♣']

def make_deck():
    return [f"{r}{s}" for r in RANKS for s in SUITS]

# Simple evaluator: returns a string label for the hand (not payouts)
def evaluate_hand(cards: List[str]) -> str:
    # cards: list of 5 like ['A♠','K♣', ...]
    # Convert ranks to numbers
    ranks = [c[:-1] for c in cards]
    suits = [c[-1] for c in cards]
    rank_counts = {}
    for r in ranks:
        rank_counts[r] = rank_counts.get(r, 0) + 1
    counts = sorted(rank_counts.values(), reverse=True)
    is_flush = len(set(suits)) == 1
    # map ranks to indices for straight detection
    idxs = sorted([RANKS.index(r) for r in ranks])
    is_straight = False
    if len(set(ranks)) == 5:
        if idxs[-1] - idxs[0] == 4:
            is_straight = True
        # wheel straight A-2-3-4-5
        if set(ranks) == set(['A','2','3','4','5']):
            is_straight = True
    if is_straight and is_flush:
        return "Straight Flush"
    if counts[0] == 4:
        return "Four of a Kind"
    if counts[0] == 3 and counts[1] == 2:
        return "Full House"
    if is_flush:
        return "Flush"
    if is_straight:
        return "Straight"
    if counts[0] == 3:
        return "Three of a Kind"
    if counts[0] == 2 and counts[1] == 2:
        return "Two Pair"
    if counts[0] == 2:
        return "One Pair"
    return "High Card"

async def start_poker_game(context, chat_id):
    # Single-player: deal 5 cards, let player hold via inline buttons, then draw and evaluate
    deck = make_deck()
    random.shuffle(deck)
    hand = [deck.pop() for _ in range(5)]
    game_id = f"poker:{chat_id}:{int(datetime.utcnow().timestamp())}"
    # store in bot_data
    context.application.bot_data[game_id] = {"deck": deck, "hand": hand}

    kb = []
    for i, card in enumerate(hand):
        kb.append([InlineKeyboardButton(f"{card}", callback_data=f"poker_toggle:{game_id}:{i}")])
    kb.append([InlineKeyboardButton("Draw", callback_data=f"poker_draw:{game_id}"), InlineKeyboardButton("Cancel", callback_data=f"poker_cancel:{game_id}")])
    await context.bot.send_message(chat_id, f"🃏 *Your Hand*:\n{' '.join(hand)}\n\nTap cards to HOLD/UNHOLD, then press Draw.", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))

async def poker_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split(":")
    if parts[0] == "poker_toggle":
        _, game_id, idx = parts
        idx = int(idx)
        state = context.application.bot_data.get(game_id)
        if not state:
            return await query.edit_message_text("Game expired or not found.")
        held = state.get("held", set())
        if isinstance(held, set): held = set(held)
        if idx in held:
            held.remove(idx)
        else:
            held.add(idx)
        state['held'] = held
        hand = state['hand']
        kb = []
        for i, card in enumerate(hand):
            label = card + (" [H]" if i in held else "")
            kb.append([InlineKeyboardButton(label, callback_data=f"poker_toggle:{game_id}:{i}")])
        kb.append([InlineKeyboardButton("Draw", callback_data=f"poker_draw:{game_id}"), InlineKeyboardButton("Cancel", callback_data=f"poker_cancel:{game_id}")])
        await query.edit_message_text(f"🃏 *Your Hand*:\n{' '.join(hand)}\n\nHeld: {sorted(list(held))}", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))

    elif parts[0] == "poker_draw":
        _, game_id = parts[0], parts[1]
        state = context.application.bot_data.pop(game_id, None)
        if not state:
            return await query.edit_message_text("Game expired or not found.")
        deck = state['deck']
        hand = state['hand']
        held = state.get('held', set()) or set()
        # draw for non-held cards
        for i in range(5):
            if i not in held:
                if not deck:
                    deck = make_deck(); random.shuffle(deck)
                hand[i] = deck.pop()
        result = evaluate_hand(hand)
        await query.edit_message_text(f"🃏 *Final Hand*:\n{' '.join(hand)}\n\n*Result:* {result}", parse_mode=ParseMode.MARKDOWN)

    elif parts[0] == "poker_cancel":
        _, game_id = parts
        context.application.bot_data.pop(game_id, None)
        await query.edit_message_text("🃏 Poker game canceled.")

# ----------------- SCHOLARSHIP SCANNER -----------------
async def get_scholarship_sources() -> List[str]:
    stored = await db.meta_get_json("scholarship_sources", [])
    if not stored:
        await db.meta_set_json("scholarship_sources", DEFAULT_SCHOLARSHIP_SOURCES)
        return DEFAULT_SCHOLARSHIP_SOURCES
    return stored

async def add_scholarship_source(url: str):
    url = url.strip()
    sources = await get_scholarship_sources()
    if url not in sources:
        sources.append(url)
        await db.meta_set_json("scholarship_sources", sources)

async def scan_scholarships(http_client: httpx.AsyncClient, limit_per_site=10):
    sources = await get_scholarship_sources()
    keywords = ["fully funded", "fully-funded", "fullyfunded", "scholarship"]
    for s in sources:
        try:
            r = await http_client.get(s, timeout=HTTP_TIMEOUT)
            if r.status_code != 200: continue
            soup = BeautifulSoup(r.text, "lxml")
            links = soup.find_all('a')
            found = 0
            for a in links:
                if found >= limit_per_site: break
                text = (a.get_text(" ", strip=True) or "").lower()
                href = a.get('href') or ''
                if any(k in text for k in keywords) or any(k in href.lower() for k in keywords):
                    url = urllib.parse.urljoin(s, href)
                    uniq_id = f"sch:{url}"
                    if await db.seen_add(uniq_id, "scholarship", {"title": text, "url": url, "source": s}):
                        msg = f"🎓 *Scholarship (likely fully funded)*\n\n{text}\n\n🔗 [Link]({url})\nSource: {s}"
                        await send_telegram_async(http_client, msg)
                        found += 1
        except Exception as e:
            logger.debug(f"Scholarship scan error for {s}: {e}")

# ----------------- SCHOLARSHIP COMMANDS -----------------
async def add_sch_source_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        return await update.message.reply_text("🚫 Unauthorized command.")
    if not context.args: return await update.message.reply_text("Usage: `/addschsource <url>`", parse_mode=ParseMode.MARKDOWN)
    url = context.args[0]
    await add_scholarship_source(url)
    await update.message.reply_text(f"✅ Added scholarship source: {url}")

async def list_sch_sources_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        return await update.message.reply_text("🚫 Unauthorized command.")
    sources = await get_scholarship_sources()
    text = "🎓 *Scholarship Sources:*\n\n" + "\n".join(sources)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def force_scholarships_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        return await update.message.reply_text("🚫 Unauthorized command.")
    http_client = context.application.bot_data['http_client']
    await update.message.reply_text("🔎 Scanning scholarship sources now...")
    await scan_scholarships(http_client)
    await update.message.reply_text("✅ Scholarship scan complete.")

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

        await db.init()
        await load_spam_words()

        asgi_server_task = asyncio.create_task(run_asgi_server())

        app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        app.bot_data['http_client'] = http_client

        app.add_handler(CommandHandler("start", start_cmd))
        app.add_handler(CommandHandler("addfilter", add_filter_cmd))
        app.add_handler(CommandHandler("delfilter", del_filter_cmd))
        app.add_handler(CallbackQueryHandler(menu_handler))

        # Poker handlers (callback-based)
        app.add_handler(CallbackQueryHandler(poker_callback_handler, pattern=r"^poker_"))

        # Scholarship commands
        app.add_handler(CommandHandler("addschsource", add_sch_source_cmd))
        app.add_handler(CommandHandler("listschsources", list_sch_sources_cmd))
        app.add_handler(CommandHandler("scholarships", force_scholarships_cmd))

        # Background Tasks
        health_checker = asyncio.create_task(nitter_health_loop(http_client))
        scheduler = asyncio.create_task(scheduler_loop(http_client))

        logger.info(f"Bot {VERSION} initialized. Polling...")

        await app.initialize()
        await app.updater.start_polling()
        await app.start()

        try:
            await asyncio.gather(health_checker, scheduler, asgi_server_task, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Main loop error: {e}")
        finally:
            logger.info("Stopping components...")
            scheduler.cancel()
            health_checker.cancel()
            asgi_server_task.cancel()
            await app.updater.stop()
            await app.stop()
            await app.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Program exited gracefully.")
        pass

