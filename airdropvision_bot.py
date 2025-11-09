#!/usr/bin/env python3 """ AirdropVision — Off-chain bot (NFTCalendar + Nitter)

Features:

Scans nftcalendar.io upcoming API for free-mints and upcoming drops

Scrapes public Nitter instances for tweets matching free-mint / airdrop queries (no X API)

SQLite persistence (seen items + meta)

Flask health endpoint (for hosting healthchecks)

Telegram alerts (python-telegram-bot PTB v13)

Background scheduler thread


Usage:

Configure environment variables (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, etc.) or use a .env loader

Run: python airdropvision_offchain.py


Notes:

This file intentionally contains NO on-chain scanners (Ethereum/Polygon/Solana) per your request.

Heuristics are best-effort. Tweak search queries and nftcalendar logic to taste. """


import os import json import sqlite3 import time import logging import threading import requests import urllib.parse from typing import Optional, List from datetime import datetime from bs4 import BeautifulSoup from flask import Flask from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, CallbackContext

----------------- CONFIG -----------------

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN") TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL", "1"))  # minutes MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "25")) BOT_NAME = os.environ.get("BOT_NAME", "AirdropVision") VERSION = os.environ.get("VERSION", "1.0.0") DB_PATH = os.environ.get("DB_PATH", "airdropvision_offchain.db")

NFTCALENDAR_API = os.environ.get("NFTCALENDAR_API", "https://api.nftcalendar.io/upcoming")

NITTER_INSTANCES = [ os.environ.get("NITTER_PRIMARY", "https://nitter.net"), os.environ.get("NITTER_FALLBACK1", "https://nitter.snopyta.org"), os.environ.get("NITTER_FALLBACK2", "https://nitter.1d4.us"), ]

NITTER_SEARCH_QUERIES = [ '("free mint" OR "free-mint" OR "free mint nft") lang:en', '("airdrop" OR "air drop") lang:en', '("solana free mint" OR "sol mint") lang:en', '("eth free mint") lang:en', ]

TELEGRAM_SEND_DELAY_SEC = float(os.environ.get("TELEGRAM_SEND_DELAY_SEC", "0.8")) TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

----------------- LOGGING -----------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s") logger = logging.getLogger(BOT_NAME)

----------------- DB -----------------

CREATE_SEEN_SQL = """ CREATE TABLE IF NOT EXISTS seen ( id TEXT PRIMARY KEY, kind TEXT, meta TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ); """

CREATE_META_SQL = """ CREATE TABLE IF NOT EXISTS meta ( k TEXT PRIMARY KEY, v TEXT ); """ = """ CREATE TABLE IF NOT EXISTS meta ( k TEXT PRIMARY KEY, v TEXT ); """ CREATE TABLE IF NOT EXISTS meta ( k TEXT PRIMARY KEY, v TEXT ); """

class DB: def init(self, path=DB_PATH): self.conn = sqlite3.connect(path, check_same_thread=False) self.conn.execute(CREATE_SEEN_SQL) self.conn.execute(CREATE_META_SQL) self.conn.commit() self._lock = threading.Lock()

def seen_add(self, id: str, kind: str = "generic", meta: Optional[dict] = None) -> bool:
    with self._lock:
        try:
            self.conn.execute("INSERT INTO seen(id, kind, meta) VALUES (?, ?, ?)",
                              (id, kind, json.dumps(meta or {})))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

def seen_count(self) -> int:
    with self._lock:
        cur = self.conn.execute("SELECT COUNT(1) FROM seen")
        return cur.fetchone()[0]

def meta_get(self, k: str, default=None):
    with self._lock:
        cur = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,))
        row = cur.fetchone()
        return row[0] if row else default

def meta_set(self, k: str, v: str):
    with self._lock:
        self.conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES (?,?)", (k, v))
        self.conn.commit()

db = DB()

----------------- SESSION -----------------

session = requests.Session() session.headers.update({"User-Agent": "AirdropVision/Offchain (+https://github.com)"})

----------------- TELEGRAM SENDER -----------------

def send_telegram_sync(text: str, parse_mode: str = "Markdown") -> bool: if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: logger.warning("Telegram not configured: missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID") return False

url = f"{TELEGRAM_API_BASE}/sendMessage"
payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": parse_mode}
backoff = 1
while True:
    try:
        r = session.post(url, json=payload, timeout=10)
    except requests.RequestException as e:
        logger.warning("Telegram request exception: %s", e)
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)
        continue

    if r.status_code == 200:
        time.sleep(TELEGRAM_SEND_DELAY_SEC)
        return True

    if r.status_code == 429:
        try:
            body = r.json()
            retry_after = int(body.get("parameters", {}).get("retry_after", 5))
        except Exception:
            retry_after = backoff
        logger.warning("Telegram rate-limited. retry_after=%s", retry_after)
        time.sleep(retry_after)
        backoff = min(backoff * 2, 60)
        continue

    logger.error("Telegram send failed: %s %s", r.status_code, r.text)
    return False

----------------- NFTCalendar scanner -----------------

def scan_nftcalendar(limit: int = MAX_RESULTS): logger.info("Scanning NFTCalendar: %s (limit=%s)", NFTCALENDAR_API, limit) try: r = session.get(NFTCALENDAR_API, timeout=12) if r.status_code != 200: logger.warning("nftcalendar API returned %s", r.status_code) return data = r.json() items = data.get("nfts") or data.get("data") or data if not isinstance(items, list): logger.debug("Unexpected nftcalendar response shape: %s", type(items)) return except Exception as e: logger.warning("nftcalendar request failed: %s", e) return

count = 0
for nft in items:
    if count >= limit:
        break
    nft_id = None
    if isinstance(nft, dict):
        nft_id = str(nft.get("id") or nft.get("slug") or (nft.get("name") or "") + "|" + str(nft.get("launch_date") or ""))
    else:
        nft_id = str(nft)
    if not nft_id:
        continue

    name = (nft.get("name") or "") if isinstance(nft, dict) else ""
    desc = (nft.get("description") or "") if isinstance(nft, dict) else ""
    mint_price = nft.get("mint_price") if isinstance(nft, dict) else None
    is_free_flag = nft.get("is_free") if isinstance(nft, dict) else None

    likely_free = False
    if is_free_flag in (True, "true", "True"):
        likely_free = True
    if "free" in (name or "").lower() or "free" in (desc or "").lower():
        likely_free = True
    if str(mint_price).strip() in ("0", "0.0", "0eth", "0.0eth", "0 wei"):
        likely_free = True

    seen_key = f"nftcal:{nft_id}"
    if db.seen_add(seen_key, kind="nftcalendar", meta=nft):
        url = nft.get("url") if isinstance(nft, dict) else None
        date = nft.get("launch_date") if isinstance(nft, dict) else None
        tag = "FREE MINT" if likely_free else "Upcoming"
        msg = f"🎨 {tag}: *{nft.get('name') or nft_id}*\nDate: {date}\nLink: {url or 'N/A'}"
        send_telegram_sync(msg)
        count += 1

----------------- Nitter scraper -----------------

def parse_nitter_search_html(html: str, base_url: str) -> List[dict]: soup = BeautifulSoup(html, "lxml") results = [] for a in soup.find_all("a", href=True): href = a["href"] parts = href.split("/") if len(parts) >= 4 and parts[2] in ("status", "statuses"): try: tweet_id = parts[3] except Exception: continue user = parts[1] if len(parts) > 1 else None tweet_url = urllib.parse.urljoin(base_url, href) parent = a.find_parent() text = "" if parent: content_div = parent.find("div", class_="tweet-content") or parent.find("div", class_="content") if content_div: text = content_div.get_text(" ", strip=True) results.append({"id": tweet_id, "user": user, "url": tweet_url, "text": text}) dedup = [] seen_ids = set() for r in results: if r["id"] not in seen_ids: dedup.append(r) seen_ids.add(r["id"]) return dedup

def scan_nitter_for_queries(queries: List[str], limit_per_query: int = 10): headers = {"User-Agent": "AirdropVision/Offchain (+https://github.com)"} for instance in NITTER_INSTANCES: try: logger.info("Trying Nitter instance: %s", instance) working = False total_found = 0 for q in queries: q_enc = urllib.parse.quote(q) url = f"{instance}/search?f=tweets&q={q_enc}" try: r = session.get(url, headers=headers, timeout=12) if r.status_code != 200: logger.debug("Nitter %s returned %s for query %s", instance, r.status_code, q) continue parsed = parse_nitter_search_html(r.text, instance) if not parsed: logger.debug("Nitter %s parse returned 0 results for %s", instance, q) continue working = True for tweet in parsed[:limit_per_query]: tweet_id = tweet["id"] seen_key = f"tweet:{tweet_id}" if db.seen_add(seen_key, kind="nitter", meta=tweet): txt = (tweet.get("text") or "").lower() tag = "FREE MINT" if "free mint" in txt or "free-mint" in txt or "free mint nft" in txt else ("AIRDROP" if "airdrop" in txt else "TWEET") msg = f"🐦 {tag}: {tweet.get('user') or 'user'}\n{tweet.get('text') or ''}\nLink: {tweet.get('url')}" send_telegram_sync(msg) total_found += 1 time.sleep(0.25) time.sleep(1.0) except Exception as e: logger.debug("Nitter fetch error for %s: %s", url, e) continue if working: logger.info("Nitter instance %s worked, found %s new items", instance, total_found) return except Exception as e: logger.debug("Nitter instance %s failed: %s", instance, e) continue

----------------- SCHEDULER -----------------

def scheduler_loop(): logger.info("Scheduler thread started, interval=%s minute(s)", POLL_INTERVAL_MINUTES) while True: try: scan_nftcalendar(limit=MAX_RESULTS) scan_nitter_for_queries(NITTER_SEARCH_QUERIES, limit_per_query=min(10, MAX_RESULTS)) logger.info("Scheduler cycle finished. Seen=%s", db.seen_count()) except Exception as e: logger.exception("Scheduler error: %s", e) time.sleep(POLL_INTERVAL_MINUTES * 60)

----------------- TELEGRAM COMMANDS -----------------

def start_cmd(update: Update, context: CallbackContext): keyboard = [[InlineKeyboardButton("📊 Stats", callback_data="stats"), InlineKeyboardButton("🚀 Force Scan", callback_data="scan_now")]] text = f"🤖 {BOT_NAME} v{VERSION}\nPolling every {POLL_INTERVAL_MINUTES}m\nSeen: {db.seen_count()}" update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

def callback_handler(update: Update, context: CallbackContext): query = update.callback_query if not query: return query.answer() if query.data == "stats": query.edit_message_text(f"📊 {BOT_NAME} v{VERSION}\nTracked: {db.seen_count()} items.") elif query.data == "scan_now": query.edit_message_text("⏳ Manual scan started...") threading.Thread(target=scan_nftcalendar, kwargs={"limit": MAX_RESULTS}, daemon=True).start() threading.Thread(target=scan_nitter_for_queries, args=(NITTER_SEARCH_QUERIES, min(10, MAX_RESULTS)), daemon=True).start() query.edit_message_text("✅ Manual scan started.")

----------------- FLASK health app -----------------

flask_app = Flask(name)

@flask_app.route("/health") def health(): return "OK", 200

def run_flask(): port = int(os.environ.get("PORT", 8080)) flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

----------------- MAIN -----------------

def main(): if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: logger.error("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set") return

flask_thread = threading.Thread(target=run_flask, daemon=True)
flask_thread.start()
logger.info("Flask health thread started")

sched_thread = threading.Thread(target=scheduler_loop, daemon=True)
sched_thread.start()
logger.info("Scheduler thread started")

updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
dp = updater.dispatcher
dp.add_handler(CommandHandler("start", start_cmd))
dp.add_handler(CallbackQueryHandler(callback_handler))

logger.info("Starting Telegram polling (main thread).")
try:
    updater.start_polling()
    updater.idle()
except Exception as e:
    logger.exception("Updater error: %s", e)
finally:
    logger.info("Shutting down.")

if name == "main": main()
