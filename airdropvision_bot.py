#!/usr/bin/env python3 """ AirdropVision v1.0 — Fully Asynchronous & Secured Off-chain Bot

File name: airdropvision_bot.py

Adds: Poker mini-game (single-player video-poker style) and Scholarship scanner (looks for "fully funded" scholarships on free sources)

No paid APIs required — only uses HTTP scraping with httpx and BeautifulSoup """


import os import json import logging import asyncio import random import urllib.parse from typing import Optional, List, Set, Dict from datetime import datetime from bs4 import BeautifulSoup from flask import Flask import uvicorn import aiosqlite import httpx from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup from telegram.constants import ParseMode from telegram.ext import ( Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, )

----------------- CONFIG -----------------

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN") TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL", "10")) MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "25")) BOT_NAME = os.environ.get("BOT_NAME", "AirdropVision") VERSION = "1.0.0-Secured-ASGI" DB_PATH = os.environ.get("DB_PATH", "airdropvision_v4.db")

Sources Config

NFTCALENDAR_API = "https://api.nftcalendar.io/upcoming"

Nitter Defaults - UPDATED to include user-provided instances

DEFAULT_NITTER_LIST = [ "https://nitter.tiekoetter.com", "https://nitter.space", "https://lightbrd.com", "https://xcancel.com", "https://nuku.trabun.org", "https://nitter.privacyredirect.com", "https://nitter.net", "https://nitter.poast.org", ] NITTER_INSTANCES_CSV = os.environ.get("NITTER_INSTANCES_CSV") NITTER_INSTANCES = [url.strip() for url in (NITTER_INSTANCES_CSV.split(',') if NITTER_INSTANCES_CSV else DEFAULT_NITTER_LIST) if url.strip()]

NITTER_SEARCH_QUERIES = [ '("free mint" OR "free-mint") -filter:replies', '("solana airdrop" OR "sol free mint") -filter:replies', '("eth free mint") -filter:replies', ]

Scholarship scanner default sources (free sites / pages). You can add more with /addschsource

DEFAULT_SCHOLARSHIP_SOURCES = [ "https://www.scholarship-positions.com/", "https://www.scholarshipsads.com/", "https://www.internships.com/"  # generic fallback - results may vary ]

Spam Defaults

DEFAULT_SPAM_KEYWORDS = "giveaway,retweet,follow,tag 3,like,rt,gleam.io,promo,dm me,whatsapp,telegram group" SPAM_WORDS: Set[str] = set()

Globals

HTTP_TIMEOUT = 15 HEALTHY_NITTER_INSTANCES: List[str] = [] NITTER_CHECK_LOCK = asyncio.Lock()

----------------- LOGGING -----------------

logging.basicConfig( level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler()] ) logger = logging.getLogger(BOT_NAME)

----------------- ASYNC DATABASE (aiosqlite) -----------------

CREATE_SEEN_SQL = """ CREATE TABLE IF NOT EXISTS seen ( id TEXT PRIMARY KEY, kind TEXT, meta TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ); """ CREATE_META_SQL = """ CREATE TABLE IF NOT EXISTS meta ( k TEXT PRIMARY KEY, v TEXT ); """

class DB: def init(self, path=DB_PATH): self.path = path self._lock = asyncio.Lock() self._init_done = False

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

----------------- HELPER: SPAM FILTER -----------------

async def load_spam_words(): global SPAM_WORDS stored = await db.meta_get_json("spam_keywords", []) if not stored: stored = [s.strip().lower() for s in DEFAULT_SPAM_KEYWORDS.split(',') if s.strip()] await db.meta_set_json("spam_keywords", stored) SPAM_WORDS = set(stored) logger.info(f"Loaded {len(SPAM_WORDS)} spam filters.")

def is_spam(text: str) -> bool: if not text: return False text = text.lower() return any(w in text for w in SPAM_WORDS)

----------------- HELPER: TELEGRAM SENDER -----------------

async def send_telegram_async(http_client: httpx.AsyncClient, text: str, parse_mode=ParseMode.MARKDOWN): if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage" payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": parse_mode, "disable_web_page_preview": False}

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

----------------- NITTER HEALTH & SCAN (unchanged behavior) -----------------

async def check_nitter_health(http_client: httpx.AsyncClient): global HEALTHY_NITTER_INSTANCES logger.info("Checking Nitter instances health...")

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

async def nitter_health_loop(http_client: httpx.AsyncClient): while True: await check_nitter_health(http_client) await asyncio.sleep(1800)

parsing helper for nitter (kept simple)

def parse_nitter(html: str, base_url: str): soup = BeautifulSoup(html, "lxml") results = [] items = soup.find_all("div", class_="timeline-item") for item in items: link = item.find("a", class_="tweet-link") if not link: continue href = link.get("href", "") tweet_id = href.split("/")[-1].replace("#m", "") url = urllib.parse.urljoin(base_url, href) content = item.find("div", class_="tweet-content") text = content.get_text(" ", strip=True) if content else "" user_a = item.find("a", class_="username") user = user_a.get_text(strip=True) if user_a else "???" results.append({"id": tweet_id, "url": url, "text": text, "user": user}) return results

async def scan_nitter(http_client: httpx.AsyncClient, limit=10): async with NITTER_CHECK_LOCK: instances = HEALTHY_NITTER_INSTANCES or NITTER_INSTANCES

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

----------------- NFT CALENDAR SCAN (unchanged) -----------------

async def scan_calendar(http_client: httpx.AsyncClient, limit=MAX_RESULTS): try: r = await http_client.get(NFTCALENDAR_API) if r.status_code != 200: return data = r.json() items = data if isinstance(data, list) else (data.get("nfts") or data.get("data") or []) for nft in items[:limit]: if not isinstance(nft, dict): continue nid = str(nft.get("id") or nft.get("slug") or nft.get("name")) name = nft.get("name", "Unknown") desc = nft.get("description", "") if is_spam(name + " " + desc): continue price = str(nft.get("mint_price", "")).lower() is_free = "free" in price or "0" == price.strip() or nft.get("is_free") if not is_free and "free" not in (name+desc).lower(): continue if await db.seen_add(f"cal:{nid}", "calendar", nft): url = nft.get("url") or nft.get("link") or "N/A" date = nft.get("launch_date") msg = f"🗓 NFT Calendar\n\n*{name}*\nPrice: {price}\nDate: {date}\n\n🔗 Link" await send_telegram_async(http_client, msg) except Exception as e: logger.error(f"Calendar scan error: {e}")

----------------- SCHEDULER -----------------

async def scheduler_loop(http_client: httpx.AsyncClient): logger.info("Scheduler started.") while True: try: await scan_calendar(http_client) await scan_nitter(http_client) await scan_scholarships(http_client) count = await db.seen_count() logger.info(f"Scan cycle complete. DB Size: {count}") except Exception as e: logger.error(f"Scheduler error: {e}") await asyncio.sleep(POLL_INTERVAL_MINUTES * 60)

----------------- TELEGRAM UI & AUTH -----------------

def is_authorized(chat_id: int) -> bool: return str(chat_id) == str(TELEGRAM_CHAT_ID)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): if not is_authorized(update.effective_chat.id): return await update.message.reply_text("🚫 Unauthorized chat.") keyboard = [ [InlineKeyboardButton("📊 Stats", callback_data="stats"), InlineKeyboardButton("⚙️ Filters", callback_data="filters")], [InlineKeyboardButton("🚀 Force Scan", callback_data="scan_now"), InlineKeyboardButton("🃏 Poker", callback_data="poker_start")] ] await update.message.reply_text( f"🤖 {BOT_NAME} v{VERSION}\nActive & Scanning...", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN )

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE): query = update.callback_query await query.answer() if not is_authorized(query.message.chat_id): return await query.edit_message_text("🚫 Unauthorized chat.") data = query.data http_client = context.application.bot_data['http_client']

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

----------------- MANUAL SCAN TRIGGER -----------------

async def run_manual_scan(http_client: httpx.AsyncClient, chat_id, context): await scan_calendar(http_client) await scan_nitter(http_client) await scan_scholarships(http_client) await context.bot.send_message(chat_id, "✅ Manual Scan Complete.")

----------------- FILTER COMMANDS -----------------

async def add_filter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): if not is_authorized(update.effective_chat.id): return await update.message.reply_text("🚫 Unauthorized command.") if not context.args: return await update.message.reply_text("Usage: /addfilter <word>", parse_mode=ParseMode.MARKDOWN) word = " ".join(context.args).lower().strip() await load_spam_words() current = list(SPAM_WORDS) if word not in current: current.append(word) await db.meta_set_json("spam_keywords", current) await load_spam_words() await update.message.reply_text(f"✅ Added filter: {word}", parse_mode=ParseMode.MARKDOWN) else: await update.message.reply_text(f"⚠️ Filter {word} already exists.", parse_mode=ParseMode.MARKDOWN)

async def del_filter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): if not is_authorized(update.effective_chat.id): return await update.message.reply_text("🚫 Unauthorized command.") if not context.args: return await update.message.reply_text("Usage: /delfilter <word>", parse_mode=ParseMode.MARKDOWN) word = " ".join(context.args).lower().strip() await load_spam_words() current = list(SPAM_WORDS) if word in current: current.remove(word) await db.meta_set_json("spam_keywords", current) await load_spam_words() await update.message.reply_text(f"🗑 Removed filter: {word}", parse_mode=ParseMode.MARKDOWN) else: await update.message.reply_text(f"⚠️ Filter {word} not found.", parse_mode=ParseMode.MARKDOWN)

----------------- FLASK & UVICORN -----------------

flask_app = Flask(name) @flask_app.route("/health") def health(): return f"AirdropVision v{VERSION} OK", 200

async def run_asgi_server(): port = int(os.environ.get("PORT", 8080)) config = uvicorn.Config( flask_app, host="0.0.0.0", port=port, log_level="warning", loop="asyncio" ) server = uvicorn.Server(config) logger.info(f"Starting ASGI server on port {port}...") await server.serve()

----------------- POKER TOURNAMENT SCRAPER (via Nitter)

Removed the offline poker mini-game per request. Instead we actively search Twitter/Nitter for poker tournaments

POKER_TWEET_QUERIES = [ '"poker tournament" -filter:replies', '"freeroll" -filter:replies', '"poker event" -filter:replies', '"WSOP" -filter:replies', '"poker promo" -filter:replies', ]

async def scan_poker_tweets(http_client: httpx.AsyncClient, limit=10): """Search healthy Nitter instances for poker-related tweets and relay useful ones.""" async with NITTER_CHECK_LOCK: instances = HEALTHY_NITTER_INSTANCES or NITTER_INSTANCES

if not instances:
    logger.debug("No Nitter instances available for poker scan.")
    return

for q in POKER_TWEET_QUERIES:
    for instance in instances:
        try:
            url = f"{instance}/search?f=tweets&q={urllib.parse.quote(q)}"
            r = await http_client.get(url)
            if r.status_code != 200: continue
            parsed = await asyncio.to_thread(parse_nitter, r.text, instance)
            if not parsed: continue
            for t in parsed[:limit]:
                txt = (t.get('text','') or '')
                # lightweight spam/filtering: skip if contains giveaway-like language
                if is_spam(txt):
                    logger.info(f"Poker tweet skipped as spam: {t['id']}")
                    continue
                # send poker tweets but tag them so users can distinguish
                if await db.seen_add(f"poker:{t['id']}", "poker", t):
                    msg = f"🃏 *Poker/Tournament*

{txt}

🔗 Link" await send_telegram_async(http_client, msg) await asyncio.sleep(0.5) break except Exception as e: logger.debug(f"Poker scan error {instance}: {e}") await asyncio.sleep(1)

async def poker_twitter_loop(http_client: httpx.AsyncClient): """Background loop that periodically scans for poker tweets.""" while True: try: await scan_poker_tweets(http_client) except Exception as e: logger.debug(f"Poker loop error: {e}") await asyncio.sleep(600)  # 10 minutes

----------------- MULTIPLAYER GAMES (group-friendly)

Implemented as lightweight, deterministic games that work with Telegram inline callbacks.

1) Horse Race — players pick horse numbers, then race animation via message edits

2) Trivia Battle — quick multiple-choice trivia, highest score wins

3) Emoji Football Penalty Shootout — turn-based penalty kicks

4) Dice Duel — each player rolls dice, highest total wins

5) Word Scramble Race — players race to unscramble a word

Note: these are minimal, self-contained implementations (no external APIs) suitable for group chat play.

GAMES: Dict[str, dict] = {}

async def start_horse_race(update: Update, context: ContextTypes.DEFAULT_TYPE): if not is_authorized(update.effective_chat.id): return await update.message.reply_text("🚫 Unauthorized chat.") chat_id = update.effective_chat.id gid = f"race:{chat_id}:{int(datetime.utcnow().timestamp())}" horses = ["🐎1","🐎2","🐎3","🐎4"] GAMES[gid] = {"type":"horse_race","players":{},"horses":horses} kb = [[InlineKeyboardButton(h, callback_data=f"race_join:{gid}:{i}") for i,h in enumerate(horses)]] kb.append([InlineKeyboardButton("Start Race", callback_data=f"race_start:{gid}")]) await update.message.reply_text("Horse Race! Tap a horse number to pick it (one per player). Then press Start Race.", reply_markup=InlineKeyboardMarkup(kb))

async def horse_race_callback(update: Update, context: ContextTypes.DEFAULT_TYPE): query = update.callback_query await query.answer() parts = query.data.split(":") action = parts[0] gid = parts[1] if gid not in GAMES: return await query.edit_message_text("Race expired or not found.") game = GAMES[gid] if action == "race_join": _, _, idx = parts idx = int(idx) user = query.from_user # one horse per user if any(p['uid']==user.id for p in game['players'].values()): return await query.answer("You already picked a horse.", show_alert=True) game['players'][str(idx)] = {"uid": user.id, "name": user.first_name} await query.edit_message_text(f"{user.first_name} picked horse {game['horses'][idx]}") elif action == "race_start": # simple animation: advance horses by random steps until someone reaches finish msg = await query.edit_message_text("Race starting...") positions = [0]*len(game['horses']) finish = 20 while True: for i in range(len(positions)): positions[i] += random.randint(1,4) # build display lines = [] for i,h in enumerate(game['horses']): track = "." * min(positions[i], finish) lines.append(f"{h} |{track}🏁") await msg.edit_text(" ".join(lines)) await asyncio.sleep(0.6) if any(p >= finish for p in positions): break winner = positions.index(max(positions)) # find owner if exists owner = game['players'].get(str(winner)) owner_name = owner['name'] if owner else "(no owner)" await msg.edit_text(f"🏁 Winner: {game['horses'][winner]} — {owner_name}") del GAMES[gid]

Placeholder implementations for other games — minimal but functional

async def start_trivia(update: Update, context: ContextTypes.DEFAULT_TYPE): if not is_authorized(update.effective_chat.id): return await update.message.reply_text("🚫 Unauthorized chat.") # Simple hard-coded question set (can be extended later) q = {"q":"Which planet is known as the Red Planet?","opts":["Earth","Mars","Venus","Jupiter"],"a":1} gid = f"trivia:{update.effective_chat.id}:{int(datetime.utcnow().timestamp())}" GAMES[gid] = {"type":"trivia","q":q,"scores":{}} kb = [[InlineKeyboardButton(o, callback_data=f"trivia_ans:{gid}:{i}")] for i,o in enumerate(q['opts'])] await update.message.reply_text(q['q'], reply_markup=InlineKeyboardMarkup(kb))

async def trivia_callback(update: Update, context: ContextTypes.DEFAULT_TYPE): query = update.callback_query await query.answer() _, gid, idx = query.data.split(":") idx = int(idx) game = GAMES.get(gid) if not game: return await query.edit_message_text("Game expired.") user = query.from_user score = 1 if idx == game['q']['a'] else 0 game['scores'][user.id] = game['scores'].get(user.id, 0) + score await query.edit_message_text(f"{user.first_name} answered. {'Correct' if score else 'Wrong'}.")

async def start_football(update: Update, context: ContextTypes.DEFAULT_TYPE): if not is_authorized(update.effective_chat.id): return await update.message.reply_text("🚫 Unauthorized chat.") gid = f"penalty:{update.effective_chat.id}:{int(datetime.utcnow().timestamp())}" GAMES[gid] = {"type":"football","turns":0} kb = [[InlineKeyboardButton("Shoot", callback_data=f"kick:{gid}")]] await update.message.reply_text("Penalty Shootout! Tap Shoot to take a penalty.", reply_markup=InlineKeyboardMarkup(kb))

async def football_callback(update: Update, context: ContextTypes.DEFAULT_TYPE): query = update.callback_query await query.answer() _, gid = query.data.split(":") game = GAMES.get(gid) if not game: return await query.edit_message_text("Game expired.") user = query.from_user success = random.random() < 0.7 game['turns'] += 1 await query.edit_message_text(f"{user.first_name} {'scored!' if success else 'missed.'}") if game['turns'] >= 5: await query.message.reply_text("Shootout over.") del GAMES[gid]

async def start_dice_duel(update: Update, context: ContextTypes.DEFAULT_TYPE): if not is_authorized(update.effective_chat.id): return await update.message.reply_text("🚫 Unauthorized chat.") gid = f"dice:{update.effective_chat.id}:{int(datetime.utcnow().timestamp())}" GAMES[gid] = {"type":"dice","players":{}} kb = [[InlineKeyboardButton("Join (roll)", callback_data=f"dice_roll:{gid}")]] await update.message.reply_text("Dice Duel! Tap to join and roll. Highest roll wins.", reply_markup=InlineKeyboardMarkup(kb))

async def dice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE): query = update.callback_query await query.answer() _, gid = query.data.split(":") game = GAMES.get(gid) if not game: return await query.edit_message_text("Game expired.") user = query.from_user roll = random.randint(1, 6) + random.randint(1,6) game['players'][user.id] = roll await query.edit_message_text(f"{user.first_name} rolled {roll}") if len(game['players']) >= 2: # end and declare winner winner = max(game['players'].items(), key=lambda kv: kv[1]) await query.message.reply_text(f"🎲 Winner: {winner[0]} with {winner[1]}") del GAMES[gid]

async def start_word_scramble(update: Update, context: ContextTypes.DEFAULT_TYPE): if not is_authorized(update.effective_chat.id): return await update.message.reply_text("🚫 Unauthorized chat.") words = ["planet","python","network","scholar"] word = random.choice(words) scrambled = ''.join(random.sample(list(word), len(word))) gid = f"scramble:{update.effective_chat.id}:{int(datetime.utcnow().timestamp())}" GAMES[gid] = {"type":"scramble","word":word,"solved":False} await update.message.reply_text(f"Unscramble: {scrambled} Reply with the correct word to win.")

Command shortcuts for launching games

async def game_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE): cmd = (context.args[0].lower() if context.args else "").strip() if cmd == "race": await start_horse_race(update, context) elif cmd == "trivia": await start_trivia(update, context) elif cmd == "football": await start_football(update, context) elif cmd == "dice": await start_dice_duel(update, context) elif cmd == "scramble": await start_word_scramble(update, context) else: await update.message.reply_text("Usage: /game <race|trivia|football|dice|scramble>")

Callback router for games

async def games_callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE): data = update.callback_query.data if data.startswith("race_") or data.startswith("race"): await horse_race_callback(update, context) elif data.startswith("trivia_") or data.startswith("trivia"): await trivia_callback(update, context) elif data.startswith("kick") or data.startswith("penalty"): await football_callback(update, context) elif data.startswith("dice_") or data.startswith("dice"): await dice_callback(update, context)

End of games section

----------------- SCHOLARSHIP SCANNER -----------------

async def get_scholarship_sources() -> List[str]: stored = await db.meta_get_json("scholarship_sources", []) if not stored: await db.meta_set_json("scholarship_sources", DEFAULT_SCHOLARSHIP_SOURCES) return DEFAULT_SCHOLARSHIP_SOURCES return stored

async def add_scholarship_source(url: str): url = url.strip() sources = await get_scholarship_sources() if url not in sources: sources.append(url) await db.meta_set_json("scholarship_sources", sources)

async def scan_scholarships(http_client: httpx.AsyncClient, limit_per_site=10): sources = await get_scholarship_sources() keywords = ["fully funded", "fully-funded", "fullyfunded", "scholarship"] for s in sources: try: r = await http_client.get(s, timeout=HTTP_TIMEOUT) if r.status_code != 200: continue soup = BeautifulSoup(r.text, "lxml") links = soup.find_all('a') found = 0 for a in links: if found >= limit_per_site: break text = (a.get_text(" ", strip=True) or "").lower() href = a.get('href') or '' if any(k in text for k in keywords) or any(k in href.lower() for k in keywords): url = urllib.parse.urljoin(s, href) uniq_id = f"sch:{url}" if await db.seen_add(uniq_id, "scholarship", {"title": text, "url": url, "source": s}): msg = f"🎓 Scholarship (likely fully funded)\n\n{text}\n\n🔗 Link\nSource: {s}" await send_telegram_async(http_client, msg) found += 1 except Exception as e: logger.debug(f"Scholarship scan error for {s}: {e}")

----------------- SCHOLARSHIP COMMANDS -----------------

async def add_sch_source_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): if not is_authorized(update.effective_chat.id): return await update.message.reply_text("🚫 Unauthorized command.") if not context.args: return await update.message.reply_text("Usage: /addschsource <url>", parse_mode=ParseMode.MARKDOWN) url = context.args[0] await add_scholarship_source(url) await update.message.reply_text(f"✅ Added scholarship source: {url}")

async def list_sch_sources_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): if not is_authorized(update.effective_chat.id): return await update.message.reply_text("🚫 Unauthorized command.") sources = await get_scholarship_sources() text = "🎓 Scholarship Sources:\n\n" + "\n".join(sources) await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def force_scholarships_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): if not is_authorized(update.effective_chat.id): return await update.message.reply_text("🚫 Unauthorized command.") http_client = context.application.bot_data['http_client'] await update.message.reply_text("🔎 Scanning scholarship sources now...") await scan_scholarships(http_client) await update.message.reply_text("✅ Scholarship scan complete.")

----------------- BOOTSTRAP / MAIN -----------------

async def main(): if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: logger.error("Error: TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set.") return

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

if name == "main": try: asyncio.run(main()) except KeyboardInterrupt: logger.info("Program exited gracefully.") pass
