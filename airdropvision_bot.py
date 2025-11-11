#!/usr/bin/env python3
"""
AirdropVision v2.0 — MONOLITHIC AWESOME EDITION (All-in-One File)
Contains all logic (Config, DB, Scrapers, Games, Main Bot) in a single script.
"""

import logging
import asyncio
import httpx
import uvicorn
import os
import json
import urllib.parse
import random
from datetime import datetime
from typing import List, Dict, Set, Optional

# External dependencies
import aiosqlite
from bs4 import BeautifulSoup
from flask import Flask

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
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
VERSION = "2.0.0-Monolithic-Awesome"
DB_PATH = os.environ.get("DB_PATH", "airdropvision_v5.db")
HTTP_TIMEOUT = 15

# --- Nitter Config ---
DEFAULT_NITTER_LIST = [
    "https://nitter.net",
    "https://nitter.tiekoetter.com",
    "https://nitter.space"
]
NITTER_INSTANCES_CSV = os.environ.get("NITTER_INSTANCES_CSV")
NITTER_INSTANCES = [url.strip() for url in (NITTER_INSTANCES_CSV.split(',') if NITTER_INSTANCES_CSV else DEFAULT_NITTER_LIST) if url.strip()]

NITTER_SEARCH_QUERIES = [
    '("free mint" OR "free-mint") -filter:replies',
    '("solana airdrop") -filter:replies'
]
POKER_TWEET_QUERIES = [
    '"poker tournament" -filter:replies',
    '"freeroll" -filter:replies'
]

# --- Scholarship Config ---
DEFAULT_SCHOLARSHIP_SOURCES = [
    "https://www.scholarship-positions.com/",
    "https://www.scholarshipsads.com/"
]
NFTCALENDAR_API = "https://api.nftcalendar.io/upcoming"

# --- Spam Config ---
DEFAULT_SPAM_KEYWORDS = "giveaway,retweet,follow,tag 3,like,rt,gleam.io,promo,dm me,whatsapp"

# --- Global State & Locks ---
SPAM_WORDS: Set[str] = set()
HEALTHY_NITTER_INSTANCES: List[str] = []
NITTER_CHECK_LOCK = asyncio.Lock()
FILTER_LOCK = asyncio.Lock()
SCHOLARSHIP_LOCK = asyncio.Lock()

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

    async def meta_get_json(self, k: str, default: list = []) -> list:
        val = await self.meta_get(k)
        if not val: return default
        try: return json.loads(val)
        except: return default

    async def meta_set_json(self, k: str, v: list):
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

# ----------------- SPAM FILTER -----------------
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

# ----------------- NITTER/TWITTER -----------------
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
        results.append({"id": tweet_id, "url": url, "text": text})
    return results

async def check_nitter_health(http_client: httpx.AsyncClient):
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

async def scan_nitter_query(http_client: httpx.AsyncClient, queries: List[str], db_kind: str, tag: str):
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
                        msg = f"{tag}\n\n{t['text']}\n\n🔗 [Link]({t['url']})"
                        await send_telegram_async(http_client, msg)
                        await asyncio.sleep(0.5)
                break 
            except Exception as e:
                logger.debug(f"Nitter error {instance}: {e}")
        await asyncio.sleep(2)

# ----------------- NFT CALENDAR -----------------
async def scan_calendar(http_client: httpx.AsyncClient):
    try:
        r = await http_client.get(NFTCALENDAR_API, timeout=HTTP_TIMEOUT)
        if r.status_code != 200: return
        data = r.json()
        items = data if isinstance(data, list) else (data.get("nfts") or data.get("data") or [])
        for nft in items[:MAX_RESULTS]:
            if not isinstance(nft, dict): continue
            nid = str(nft.get("id") or nft.get("slug") or nft.get("name"))
            name = nft.get("name", "Unknown")
            desc = nft.get("description", "")
            if is_spam(name + " " + desc): continue
            price = str(nft.get("mint_price", "")).lower()
            is_free = "free" in price or "0" == price.strip() or nft.get("is_free")
            if not is_free: continue
            
            if await db.seen_add(f"cal:{nid}", "calendar", nft):
                url = nft.get("url") or nft.get("link") or "N/A"
                date = nft.get("launch_date")
                msg = f"🗓 *NFT Calendar (Free)*\n\n*{name}*\nPrice: {price}\nDate: {date}\n\n🔗 [{url}]({url})"
                await send_telegram_async(http_client, msg)
                await asyncio.sleep(0.5)
    except Exception as e:
        logger.error(f"Calendar scan error: {e}")

# ----------------- SCHOLARSHIPS -----------------
async def get_scholarship_sources() -> List[str]:
    stored = await db.meta_get_json("scholarship_sources", [])
    if not stored:
        await db.meta_set_json("scholarship_sources", DEFAULT_SCHOLARSHIP_SOURCES)
        return DEFAULT_SCHOLARSHIP_SOURCES
    return stored

async def scan_scholarships(http_client: httpx.AsyncClient, limit_per_site=5):
    sources = await get_scholarship_sources()
    keywords = ["fully funded", "fully-funded", "full scholarship"]
    
    for s in sources:
        try:
            r = await http_client.get(s, timeout=HTTP_TIMEOUT)
            if r.status_code != 200: continue
            
            soup = await asyncio.to_thread(BeautifulSoup, r.text, "lxml")
            links = soup.find_all('a')
            found = 0
            
            for a in links:
                if found >= limit_per_site: break
                text = (a.get_text(" ", strip=True) or "").lower()
                href = a.get('href') or ''
                
                if any(k in text for k in keywords):
                    url = urllib.parse.urljoin(s, href)
                    uniq_id = f"sch:{url}"
                    if await db.seen_add(uniq_id, "scholarship", {"title": text, "url": url}):
                        msg = f"🎓 *Scholarship Scan*\n\n*{a.get_text(' ', strip=True)}*\n\n🔗 [{url}]({url})\nSource: {s}"
                        await send_telegram_async(http_client, msg)
                        found += 1
                        await asyncio.sleep(0.5)
        except Exception as e:
            logger.debug(f"Scholarship scan error for {s}: {e}")

# ----------------- MAIN SCHEDULERS -----------------
async def scheduler_loop(http_client: httpx.AsyncClient):
    logger.info("Scheduler started.")
    while True:
        try:
            await scan_calendar(http_client)
            await scan_nitter_query(http_client, NITTER_SEARCH_QUERIES, "nitter", "🐦 *Twitter/Nitter*")
            await scan_scholarships(http_client)
            
            count = await db.seen_count()
            logger.info(f"Scan cycle complete. DB Size: {count}")
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
        await asyncio.sleep(POLL_INTERVAL_MINUTES * 60)

async def poker_twitter_loop(http_client: httpx.AsyncClient):
    logger.info("Poker scanner started.")
    while True:
        try:
            await scan_nitter_query(http_client, POKER_TWEET_QUERIES, "poker", "🃏 *Poker Tweet*")
        except Exception as e:
            logger.debug(f"Poker loop error: {e}")
        await asyncio.sleep(600) # Scan for poker every 10 mins

async def run_manual_scan(http_client: httpx.AsyncClient, chat_id: int, context):
    await context.bot.send_message(chat_id, "🚀 Manual scan initiated...")
    try:
        await scan_calendar(http_client)
        await scan_nitter_query(http_client, NITTER_SEARCH_QUERIES, "nitter", "🐦 *Twitter/Nitter*")
        await scan_scholarships(http_client)
        await scan_nitter_query(http_client, POKER_TWEET_QUERIES, "poker", "🃏 *Poker Tweet*")
        await context.bot.send_message(chat_id, "✅ Manual Scan Complete.")
    except Exception as e:
        logger.error(f"Manual scan error: {e}")
        await context.bot.send_message(chat_id, f"⚠️ Manual Scan Failed: {e}")


# ----------------------------------------------------------------------
## 4. 🎮 GAMES LOGIC
# ----------------------------------------------------------------------

# ----------------- GAME COMMAND ROUTER -----------------
async def game_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Router for all /game <name> commands."""
    if not context.args:
        await update.message.reply_text("Usage: /game <race|scramble|dice>")
        return
        
    cmd = context.args[0].lower().strip()
    
    if cmd == "race":
        await start_horse_race(update, context)
    elif cmd == "scramble":
        await start_word_scramble(update, context)
    elif cmd == "dice":
        await start_dice_duel(update, context)
    else:
        await update.message.reply_text(f"Unknown game: {cmd}")

# ----------------- GAME CALLBACK ROUTER -----------------
async def handle_game_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data_parts: List[str]):
    """Router for all callbacks starting with 'game:'"""
    query = update.callback_query
    
    action = data_parts[1]
    
    # Handle New Game Starts from the Menu (no gid yet)
    if action == "start_race":
        await start_horse_race(update, context)
        return
    elif action == "start_dice":
        await start_dice_duel(update, context)
        return
    elif action == "start_scramble":
        await start_word_scramble(update, context)
        return
    
    # --- Existing Game Actions (Requires gid) ---
    try:
        gid = data_parts[2]
    except IndexError:
        await query.answer("Internal game error (Missing GID).", show_alert=True)
        return

    if gid not in context.chat_data:
        await query.answer("Game expired or not found.", show_alert=True)
        await query.edit_message_text("This game has expired. Please start a new one.")
        return
        
    game = context.chat_data[gid]
    
    if action == "race_join":
        await handle_race_join(query, context, game, gid, data_parts)
    elif action == "race_start":
        await handle_race_start(query, context, game, gid)
    elif action == "dice_roll":
        await handle_dice_roll(query, context, game, gid)
    elif action == "dice_end":
        await handle_dice_end(query, context, game, gid)

# ----------------- WORD SCRAMBLE (REPLY HANDLER) -----------------
async def handle_scramble_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles replies to scramble messages."""
    if not update.message.reply_to_message:
        return
        
    reply_to_msg_id = update.message.reply_to_message.message_id
    game_key = f"scramble_ans_{reply_to_msg_id}"
    
    if game_key in context.chat_data:
        game = context.chat_data[game_key]
        
        if game["solved"]:
            await update.message.reply_text("This scramble was already solved!")
            return
            
        answer = game["word"]
        guess = update.message.text.strip().lower()
        
        if guess == answer:
            game["solved"] = True 
            user_name = update.message.from_user.first_name
            await update.message.reply_text(f"🎉 Correct! *{user_name}* solved it!\nThe word was *{answer.upper()}*.", parse_mode=ParseMode.MARKDOWN)
            del context.chat_data[game_key]
        else:
            await update.message.reply_text("Nope, try again!", quote=True)

# ----------------- HORSE RACE -----------------
async def start_horse_race(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = f"race:{int(datetime.now().timestamp())}"
    horses = ["🐎1","🐎2","🐎3","🐎4"]
    
    context.chat_data[gid] = {
        "type": "horse_race", "players": {}, "horses": horses, "message_id": None
    }
    
    kb = [[InlineKeyboardButton(h, callback_data=f"game:race_join:{gid}:{i}") for i, h in enumerate(horses)]]
    kb.append([InlineKeyboardButton("🏁 Start Race", callback_data=f"game:race_start:{gid}")])
    
    source_message = update.callback_query.message if update.callback_query else update.message
    
    msg = await source_message.reply_text(
        "🐴 *Horse Race!*\nPick a horse. One per player.",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN
    )
    context.chat_data[gid]["message_id"] = msg.message_id

async def handle_race_join(query, context, game, gid, data_parts):
    user = query.from_user
    horse_idx = int(data_parts[3])
    
    if user.id in game['players']:
        await query.answer("You already picked a horse!", show_alert=True)
        return
        
    game['players'][user.id] = horse_idx
    await query.answer(f"You picked {game['horses'][horse_idx]}!")
    
    player_list = []
    for uid, h_idx in game['players'].items():
        try:
            chat_member = await context.bot.get_chat_member(query.message.chat_id, uid)
            name = chat_member.user.first_name
        except:
            name = f"Player {uid}"
        player_list.append(f"{game['horses'][h_idx]} ({name})")
    
    await query.edit_message_text(
        f"🐴 *Horse Race!*\nPick a horse.\n\n*Selections:*\n" + "\n".join(player_list),
        reply_markup=query.message.reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_race_start(query, context, game, gid):
    if not game['players']:
        await query.answer("Need at least one player to start!", show_alert=True)
        return
        
    await query.edit_message_text("🏁 *AND THEY'RE OFF!*", reply_markup=None, parse_mode=ParseMode.MARKDOWN)
    
    asyncio.create_task(run_race_animation(
        context, query.message, game['horses'], game['players'], gid
    ))

async def run_race_animation(context: ContextTypes.DEFAULT_TYPE, message: Message, horses: List[str], players: Dict, gid: str):
    positions = [0] * len(horses)
    finish_line = 20
    
    while True:
        await asyncio.sleep(1.0) 
        race_text = "🏁 *Race in Progress!*\n\n"
        winners = []
        
        for i in range(len(positions)):
            if positions[i] < finish_line:
                positions[i] += random.randint(1, 3)
            
            track = "─" * positions[i]
            track = track.ljust(finish_line, " ")
            race_text += f"`{horses[i]} |{track}|`\n"
            
            if positions[i] >= finish_line:
                winners.append(i)
        
        try:
            await message.edit_text(race_text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass 
            
        if winners:
            break
            
    winner_idx = random.choice(winners)
    winner_horse = horses[winner_idx]
    
    winning_players = []
    for uid, h_idx in players.items():
        if h_idx == winner_idx:
            try:
                chat_member = await context.bot.get_chat_member(message.chat_id, uid)
                winning_players.append(chat_member.user.first_name)
            except:
                winning_players.append(f"A Player (ID: {uid})")
            
    winner_text = ", ".join(winning_players) if winning_players else "No one picked the winner."
    
    await message.edit_text(
        f"🎉 *Race Over!*\n\n🏆 Winning Horse: {winner_horse}\n\n👤 Winners: *{winner_text}*", 
        parse_mode=ParseMode.MARKDOWN
    )
    
    # Cleanup game state after the race is 100% complete
    if gid in context.chat_data:
        del context.chat_data[gid]

# ----------------- DICE DUEL -----------------
async def start_dice_duel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = f"dice:{int(datetime.now().timestamp())}"
    context.chat_data[gid] = {
        "type": "dice", "players": {}, "message_id": None
    }
    
    kb = [
        [InlineKeyboardButton("🎲 Roll Dice", callback_data=f"game:dice_roll:{gid}")],
        [InlineKeyboardButton("🏁 End Game", callback_data=f"game:dice_end:{gid}")],
    ]
    
    source_message = update.callback_query.message if update.callback_query else update.message

    msg = await source_message.reply_text(
        "🎲 *Dice Duel!*\nTap to roll. When done, tap End Game.",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN
    )
    context.chat_data[gid]["message_id"] = msg.message_id

async def handle_dice_roll(query, context, game, gid):
    user = query.from_user
    roll = random.randint(1, 6) + random.randint(1, 6)
    game['players'][user.id] = {"name": user.first_name, "roll": roll}
    
    await query.answer(f"You rolled a {roll}!")
    
    player_list = "\n".join([f"- {p['name']}: {p['roll']}" for p in game['players'].values()])
    await query.edit_message_text(
        f"🎲 *Dice Duel!*\nTap to roll.\n\n*Rolls:*\n{player_list}",
        reply_markup=query.message.reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_dice_end(query, context, game, gid):
    if not game['players']:
        await query.edit_message_text("Game ended with no players.")
    else:
        winner = max(game['players'].values(), key=lambda p: p['roll'])
        await query.edit_message_text(
            f"🎲 *Dice Duel Over!*\n\n🏆 Winner: *{winner['name']}* with a roll of *{winner['roll']}*!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=None
        )
    del context.chat_data[gid]

# ----------------- WORD SCRAMBLE (START) -----------------
async def start_word_scramble(update: Update, context: ContextTypes.DEFAULT_TYPE):
    words = ["planet", "python", "network", "scholar", "airdrop", "crypto"]
    word = random.choice(words)
    scrambled = "".join(random.sample(list(word), len(word)))
    
    source_message = update.callback_query.message if update.callback_query else update.message

    msg = await source_message.reply_text(
        f"🧠 *Word Scramble*\n\nUnscramble this word:\n\n`{scrambled.upper()}`\n\nReply to this message with your answer!",
        parse_mode=ParseMode.MARKDOWN
    )
    
    context.chat_data[f"scramble_ans_{msg.message_id}"] = {
        "word": word,
        "solved": False
    }


# ----------------------------------------------------------------------
## 5. 🤖 TELEGRAM BOT & MAIN COORDINATOR
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

# ----------------- MAIN MENU & COMMANDS -----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update, context): return
    keyboard = [
        [InlineKeyboardButton("📊 Stats", callback_data="stats"), InlineKeyboardButton("⚙️ Filters", callback_data="filters")],
        [InlineKeyboardButton("🎓 Scholarships", callback_data="scholarships_menu"), InlineKeyboardButton("🎮 Games", callback_data="games_menu")],
        [InlineKeyboardButton("🚀 Force Scan", callback_data="scan_now")],
    ]
    await update.message.reply_text(
        f"🤖 *{BOT_NAME} v{VERSION}*\nActive & Scanning...",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def get_main_menu():
    keyboard = [
        [InlineKeyboardButton("📊 Stats", callback_data="stats"), InlineKeyboardButton("⚙️ Filters", callback_data="filters")],
        [InlineKeyboardButton("🎓 Scholarships", callback_data="scholarships_menu"), InlineKeyboardButton("🎮 Games", callback_data="games_menu")],
        [InlineKeyboardButton("🚀 Force Scan", callback_data="scan_now")],
    ]
    return "🤖 *Main Menu*\nActive & Scanning...", InlineKeyboardMarkup(keyboard)

# ----------------- FILTER COMMANDS -----------------
async def add_filter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update, context): return
    if not context.args:
        await update.message.reply_text("Usage: `/addfilter <word>`", parse_mode=ParseMode.MARKDOWN)
        return
    
    word = " ".join(context.args).lower().strip()
    
    async with FILTER_LOCK:
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
    if not await auth_guard(update, context): return
    if not context.args:
        await update.message.reply_text("Usage: `/delfilter <word>`", parse_mode=ParseMode.MARKDOWN)
        return
        
    word = " ".join(context.args).lower().strip()
    
    async with FILTER_LOCK:
        await load_spam_words()
        current = list(SPAM_WORDS)
        if word in current:
            current.remove(word)
            await db.meta_set_json("spam_keywords", current)
            await load_spam_words()
            await update.message.reply_text(f"🗑 Removed filter: `{word}`", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(f"⚠️ Filter `{word}` not found.", parse_mode=ParseMode.MARKDOWN)

# ----------------- SCHOLARSHIP COMMANDS -----------------
async def add_sch_source_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update, context): return
    if not context.args:
        await update.message.reply_text("Usage: `/addschsource <url>`", parse_mode=ParseMode.MARKDOWN)
        return
    
    url = context.args[0].strip()
    
    async with SCHOLARSHIP_LOCK:
        sources = await get_scholarship_sources()
        if url not in sources:
            sources.append(url)
            await db.meta_set_json("scholarship_sources", sources)
            await update.message.reply_text(f"✅ Added source: {url}")
        else:
            await update.message.reply_text(f"⚠️ Source already exists.")

async def list_sch_sources_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update, context): return
    sources = await get_scholarship_sources()
    text = "🎓 *Scholarship Sources:*\n\n" + "\n".join([f"- `{s}`" for s in sources])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def force_scholarships_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update, context): return
    http_client = context.application.bot_data['http_client']
    await update.message.reply_text("🔎 Scanning scholarship sources now...")
    await scan_scholarships(http_client)
    await update.message.reply_text("✅ Scholarship scan complete.")

# ----------------- CALLBACK ROUTER -----------------
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update, context): return
    
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data == "main_menu":
        text, markup = await get_main_menu()
        await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
    
    elif data == "stats":
        count = await db.seen_count()
        spam_count = len(SPAM_WORDS)
        async with NITTER_CHECK_LOCK:
            healthy_count = len(HEALTHY_NITTER_INSTANCES)
        msg = f"📊 *Statistics*\n\nItems Tracked: `{count}`\nSpam Filters: `{spam_count}`\nNitter Nodes: `{healthy_count}`"
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
        
    elif data == "scan_now":
        await query.edit_message_text("🚀 Scanning now... this may take a moment.")
        http_client = context.application.bot_data['http_client']
        asyncio.create_task(run_manual_scan(http_client, query.message.chat_id, context))

    # --- Filter Routes ---
    elif data == "filters":
        kb = [
            [InlineKeyboardButton("📜 List Filters", callback_data="list_filters")],
            [InlineKeyboardButton("➕ Add (/addfilter)", callback_data="add_filter_info")],
            [InlineKeyboardButton("➖ Del (/delfilter)", callback_data="del_filter_info")],
            [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
        ]
        await query.edit_message_text("⚙️ *Filter Settings*\nManage anti-spam keywords.", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    
    elif data == "list_filters":
        await load_spam_words()
        text = "🚫 *Blocked Keywords:*\n\n" + "\n".join([f"- `{w}`" for w in sorted(list(SPAM_WORDS))])
        if not SPAM_WORDS: text = "No filters set."
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="filters")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    
    elif data == "add_filter_info":
        text = "To *ADD* a filter, type:\n`/addfilter <word>`"
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="filters")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

    elif data == "del_filter_info":
        text = "To *REMOVE* a filter, type:\n`/delfilter <word>`"
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="filters")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

    # --- Scholarship Routes ---
    elif data == "scholarships_menu":
        kb = [
            [InlineKeyboardButton("📜 List Sources", callback_data="list_sch_sources")],
            [InlineKeyboardButton("➕ Add Source (/addschsource)", callback_data="add_sch_info")],
            [InlineKeyboardButton("🔎 Force Scan", callback_data="scan_sch_now")],
            [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
        ]
        await query.edit_message_text("🎓 *Scholarship Settings*\nManage free scholarship sources.", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

    elif data == "list_sch_sources":
        sources = await get_scholarship_sources()
        text = "🎓 *Scholarship Sources:*\n\n" + "\n".join([f"- `{s}`" for s in sources])
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="scholarships_menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
        
    elif data == "add_sch_info":
        text = "To *ADD* a source, type:\n`/addschsource <url>`"
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="scholarships_menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

    elif data == "scan_sch_now":
        await query.edit_message_text("🔎 Scanning scholarship sources now...")
        http_client = context.application.bot_data['http_client']
        await scan_scholarships(http_client)
        await query.message.reply_text("✅ Scholarship scan complete.")

    # --- Game Menu Route ---
    elif data == "games_menu":
        kb = [
            [InlineKeyboardButton("🐴 Horse Race", callback_data="game:start_race")],
            [InlineKeyboardButton("🎲 Dice Duel", callback_data="game:start_dice")],
            [InlineKeyboardButton("🧠 Word Scramble", callback_data="game:start_scramble")],
            [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
        ]
        await query.edit_message_text(
            "🎮 *Mini-Games*\nPick a game to start. (Only playable in this chat.)",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=ParseMode.MARKDOWN
        )

    # --- Game Routes (Delegated to games.py logic) ---
    elif data.startswith("game:"):
        await handle_game_callback(update, context, data.split(":"))

    else:
        logger.warning(f"Unknown callback data: {data}")

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

        # Init DB and spam words
        await db.init()
        await load_spam_words()

        # Start the web server
        asgi_server_task = asyncio.create_task(run_asgi_server())

        # Setup Telegram Bot
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        app.bot_data['http_client'] = http_client

        # --- Register Handlers ---
        app.add_handler(CommandHandler("start", start_cmd))
        app.add_handler(CommandHandler("addfilter", add_filter_cmd))
        app.add_handler(CommandHandler("delfilter", del_filter_cmd))
        app.add_handler(CommandHandler("addschsource", add_sch_source_cmd))
        app.add_handler(CommandHandler("listschsources", list_sch_sources_cmd))
        app.add_handler(CommandHandler("scholarships", force_scholarships_cmd))
        app.add_handler(CommandHandler("game", game_command_handler))

        app.add_handler(CallbackQueryHandler(callback_router))
        app.add_handler(MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, handle_scramble_reply))

        # --- Background Tasks ---
        health_checker = asyncio.create_task(nitter_health_loop(http_client))
        scheduler = asyncio.create_task(scheduler_loop(http_client))
        poker_loop = asyncio.create_task(poker_twitter_loop(http_client))

        logger.info(f"Bot {VERSION} initialized. Starting polling...")
        
        await app.initialize()
        await app.updater.start_polling()
        await app.start()

        all_tasks = [health_checker, scheduler, poker_loop, asgi_server_task]
        try:
            await asyncio.gather(*all_tasks)
        except (asyncio.CancelledError, KeyboardInterrupt):
            logger.info("Shutdown signal received.")
        except Exception as e:
            logger.error(f"Main loop error: {e}", exc_info=True)
        finally:
            logger.info("Stopping components...")
            for task in all_tasks:
                task.cancel()
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
            logger.info("Bot shut down gracefully.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Program exited gracefully.")

