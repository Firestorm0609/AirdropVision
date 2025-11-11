#!/usr/bin/env python3
"""
AirdropVision — Off-chain bot (NFTCalendar + Nitter) [Async Version]

Scans nftcalendar.io upcoming API for free-mints and upcoming drops
Scrapes public Nitter instances for tweets matching free-mint / airdrop queries (no X API)
SQLite persistence (seen items + meta)
Flask health endpoint (for hosting healthchecks)
Telegram alerts (python-telegram-bot PTB v20+)
Async scheduler (asyncio)
"""

import os
import json
import sqlite3
import time
import logging
import threading
import httpx  # Async HTTP client
import asyncio
import urllib.parse
from typing import Optional, List
from datetime import datetime
from bs4 import BeautifulSoup
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL", "1"))
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "25"))
BOT_NAME = os.environ.get("BOT_NAME", "AirdropVision")
VERSION = os.environ.get("VERSION", "2.0.0-async")  # Updated version
DB_PATH = os.environ.get("DB_PATH", "airdropvision_offchain.db")

NFTCALENDAR_API = os.environ.get("NFTCALENDAR_API", "https://api.nftcalendar.io/upcoming")

NITTER_INSTANCES = [
    os.environ.get("NITTER_PRIMARY", "https://nitter.net"),
    os.environ.get("NITTER_FALLBACK1", "https://nitter.snopyta.org"),
    os.environ.get("NITTER_FALLBACK2", "https://nitter.1d4.us"),
]

NITTER_SEARCH_QUERIES = [
    '("free mint" OR "free-mint" OR "free mint nft") lang:en',
    '("airdrop" OR "air drop") lang:en',
    '("solana free mint" OR "sol mint") lang:en',
    '("eth free mint") lang:en',
]

TELEGRAM_SEND_DELAY_SEC = float(os.environ.get("TELEGRAM_SEND_DELAY_SEC", "0.8"))
TELEGRAM_RETRY_MAX = 5  # Max retries for failed sends
HTTP_TIMEOUT = 12  # Timeout for all HTTP requests

# ----------------- LOGGING -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(BOT_NAME)

# ----------------- DB -----------------
# NOTE: This DB class remains synchronous and thread-safe.
# This is acceptable because SQLite I/O is very fast and won't
# significantly block the async event loop.
# Using 'check_same_thread=False' is necessary to allow access
# from the main asyncio thread and the Flask thread.
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
        base_dir = os.path.dirname(os.path.abspath(path))
        if base_dir and base_dir != "/" and not os.path.exists(base_dir):
            try:
                os.makedirs(base_dir, exist_ok=True)
            except Exception:
                pass
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute(CREATE_SEEN_SQL)
        self.conn.execute(CREATE_META_SQL)
        self.conn.commit()
        self._lock = threading.Lock()  # Use threading.Lock for cross-thread safety

    def seen_add(self, id: str, kind: str = "generic", meta: Optional[dict] = None) -> bool:
        with self._lock:
            try:
                self.conn.execute(
                    "INSERT INTO seen(id, kind, meta) VALUES (?, ?, ?)",
                    (id, kind, json.dumps(meta or {})),
                )
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

# ----------------- HTTPX SESSION -----------------
# Global async client with headers
http_client = httpx.AsyncClient(
    headers={"User-Agent": "AirdropVision/Offchain (+https://github.com)"},
    timeout=HTTP_TIMEOUT,
    follow_redirects=True,
)

# ----------------- TELEGRAM SENDER (Async) -----------------
# This standalone sender is used by the scheduler
async def send_telegram_async(text: str, parse_mode: str = "Markdown") -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured: missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID")
        return False

    TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    url = f"{TELEGRAM_API_BASE}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": parse_mode}

    backoff = 1
    retries = 0
    while retries < TELEGRAM_RETRY_MAX:
        try:
            r = await http_client.post(url, json=payload)
        except httpx.RequestError as e:
            logger.warning("Telegram request exception: %s", e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
            retries += 1
            continue

        if r.status_code == 200:
            await asyncio.sleep(TELEGRAM_SEND_DELAY_SEC)
            return True

        if r.status_code == 429:  # Rate limited
            try:
                body = r.json()
                retry_after = int(body.get("parameters", {}).get("retry_after", 5))
            except Exception:
                retry_after = backoff
            logger.warning("Telegram rate-limited. retry_after=%s", retry_after)
            await asyncio.sleep(retry_after)
            backoff = min(backoff * 2, 60)
            # Don't increment retries on a 429, as it's an explicit instruction
            continue

        # Other fatal errors (e.g., 400 Bad Request, 403 Forbidden)
        logger.error("Telegram send failed (fatal): %s %s", r.status_code, r.text)
        return False  # Give up on fatal errors

    logger.error("Telegram send failed after max retries.")
    return False


# ----------------- NFTCalendar scanner (Async) -----------------
async def scan_nftcalendar(limit: int = MAX_RESULTS):
    logger.info("Scanning NFTCalendar: %s (limit=%s)", NFTCALENDAR_API, limit)
    try:
        r = await http_client.get(NFTCALENDAR_API)
        if r.status_code != 200:
            logger.warning("nftcalendar API returned %s", r.status_code)
            return
        data = r.json()
        items = data.get("nfts") or data.get("data") or data
        if not isinstance(items, list):
            logger.debug("Unexpected nftcalendar response shape: %s", type(items))
            return
    except Exception as e:
        logger.warning("nftcalendar request failed: %s", e)
        return

    count = 0
    for nft in items:
        if count >= limit:
            break
        nft_id = None
        if isinstance(nft, dict):
            nft_id = str(
                nft.get("id")
                or nft.get("slug")
                or (nft.get("name") or "") + "|" + str(nft.get("launch_date") or "")
            )
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
        if mint_price is not None and str(mint_price).strip().lower() in (
            "0",
            "0.0",
            "0eth",
            "0.0eth",
            "0 wei",
            "0wei",
        ):
            likely_free = True

        seen_key = f"nftcal:{nft_id}"
        # DB call is blocking, but very fast.
        if db.seen_add(seen_key, kind="nftcalendar", meta=nft):
            url = (
                (nft.get("url") if isinstance(nft, dict) else None)
                or nft.get("link")
                if isinstance(nft, dict)
                else None
            )
            date = nft.get("launch_date") if isinstance(nft, dict) else None
            tag = "FREE MINT" if likely_free else "Upcoming"
            msg = f"🎨 {tag}: *{nft.get('name') or nft_id}*\n\nDate: {date or 'N/A'}\nLink: {url or 'N/A'}"
            await send_telegram_async(msg)
            count += 1


# ----------------- Nitter scraper (Async) -----------------
# This parser function itself is not async, as it's just CPU-bound string processing
def parse_nitter_search_html(html: str, base_url: str) -> List[dict]:
    soup = BeautifulSoup(html, "lxml")
    results = []
    # Prefer to iterate over tweets blocks if present
    tweet_blocks = soup.find_all("div", class_="timeline-item") or soup.find_all(
        "div", class_="tweet"
    )
    if not tweet_blocks:
        # fallback: scan anchors
        for a in soup.find_all("a", href=True):
            href = a["href"]
            parts = href.split("/")
            # nitter links look like /user/status/12345 or /user/statuses/12345 or /user/12345/status/...
            if len(parts) >= 4 and parts[2] in ("status", "statuses"):
                try:
                    tweet_id = parts[3]
                except Exception:
                    continue
                user = parts[1] if len(parts) > 1 else None
                tweet_url = urllib.parse.urljoin(base_url, href)
                parent = a.find_parent()
                text = ""
                if parent:
                    content_div = parent.find(
                        "div", class_="tweet-content"
                    ) or parent.find("div", class_="content")
                    if content_div:
                        text = content_div.get_text(" ", strip=True)
                results.append({"id": tweet_id, "user": user, "url": tweet_url, "text": text})
    else:
        for block in tweet_blocks:
            # find link inside block
            a = block.find("a", href=True)
            if not a:
                continue
            href = a["href"]
            parts = href.split("/")
            if len(parts) >= 4 and parts[2] in ("status", "statuses"):
                try:
                    tweet_id = parts[3]
                except Exception:
                    continue
                user = parts[1] if len(parts) > 1 else None
                tweet_url = urllib.parse.urljoin(base_url, href)
                text = block.get_text(" ", strip=True)
                results.append({"id": tweet_id, "user": user, "url": tweet_url, "text": text})

    # dedupe
    dedup = []
    seen_ids = set()
    for r in results:
        if r["id"] not in seen_ids:
            dedup.append(r)
            seen_ids.add(r["id"])
    return dedup


async def scan_nitter_for_queries(queries: List[str], limit_per_query: int = 10):
    # Note: Nitter scraping is inherently fragile and may break if HTML changes.
    for instance in NITTER_INSTANCES:
        try:
            logger.info("Trying Nitter instance: %s", instance)
            working = False
            total_found = 0
            for q in queries:
                q_enc = urllib.parse.quote(q)
                url = f"{instance}/search?f=tweets&q={q_enc}"
                try:
                    r = await http_client.get(url)
                    if r.status_code != 200:
                        logger.debug(
                            "Nitter %s returned %s for query %s",
                            instance,
                            r.status_code,
                            q,
                        )
                        continue
                    
                    # Run the CPU-bound parsing in a separate thread to not block the event loop
                    parsed = await asyncio.to_thread(
                        parse_nitter_search_html, r.text, instance
                    )
                    
                    if not parsed:
                        logger.debug(
                            "Nitter %s parse returned 0 results for %s", instance, q
                        )
                        continue
                    working = True
                    for tweet in parsed[:limit_per_query]:
                        tweet_id = tweet["id"]
                        seen_key = f"tweet:{tweet_id}"
                        if db.seen_add(seen_key, kind="nitter", meta=tweet):
                            txt = (tweet.get("text") or "").lower()
                            tag = (
                                "FREE MINT"
                                if (
                                    "free mint" in txt
                                    or "free-mint" in txt
                                    or "free mint nft" in txt
                                )
                                else ("AIRDROP" if "airdrop" in txt else "TWEET")
                            )
                            msg = f"🐦 {tag}: @{tweet.get('user') or 'user'} {tweet.get('text') or ''}\nLink: {tweet.get('url')}"
                            await send_telegram_async(msg)
                            total_found += 1
                            await asyncio.sleep(0.25)
                    # small pause between queries
                    await asyncio.sleep(1.0)
                except Exception as e:
                    logger.debug("Nitter fetch error for %s: %s", url, e)
                    continue
            if working:
                logger.info(
                    "Nitter instance %s worked, found %s new items",
                    instance,
                    total_found,
                )
                return  # Success, don't try other instances
        except Exception as e:
            logger.debug("Nitter instance %s failed: %s", instance, e)
            continue


# ----------------- SCHEDULER (Async) -----------------
async def scheduler_loop():
    logger.info("Scheduler task started, interval=%s minute(s)", POLL_INTERVAL_MINUTES)
    while True:
        try:
            await scan_nftcalendar(limit=MAX_RESULTS)
            await scan_nitter_for_queries(
                NITTER_SEARCH_QUERIES, limit_per_query=min(10, MAX_RESULTS)
            )
            logger.info("Scheduler cycle finished. Seen=%s", db.seen_count())
        except Exception as e:
            logger.exception("Scheduler error: %s", e)
        await asyncio.sleep(POLL_INTERVAL_MINUTES * 60)


# ----------------- TELEGRAM COMMANDS (PTB v20) -----------------
manual_scan_lock = asyncio.Lock()


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("📊 Stats", callback_data="stats"),
            InlineKeyboardButton("🚀 Force Scan", callback_data="scan_now"),
        ]
    ]
    text = f"🤖 {BOT_NAME} v{VERSION}\nPolling every {POLL_INTERVAL_MINUTES}m\nSeen: {db.seen_count()}"
    await update.message.reply_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    
    await query.answer()
    
    if query.data == "stats":
        await query.edit_message_text(
            f"📊 {BOT_NAME} v{VERSION}\nTracked: {db.seen_count()} items."
        )
    elif query.data == "scan_now":
        if manual_scan_lock.locked():
            await query.edit_message_text(
                "⏳ A manual scan is already in progress. Please wait."
            )
            return

        await query.edit_message_text("⏳ Manual scan starting...")

        # Run the scan in a new task to avoid blocking the handler
        async def do_scan():
            async with manual_scan_lock:
                logger.info("Starting manual scan...")
                await scan_nftcalendar(limit=MAX_RESULTS)
                await scan_nitter_for_queries(
                    NITTER_SEARCH_QUERIES, min(10, MAX_RESULTS)
                )
                logger.info("Manual scan finished.")
                # Send a *new* message on completion
                await context.bot.send_message(
                    chat_id=query.message.chat_id, text="✅ Manual scan finished."
                )

        asyncio.create_task(do_scan())


# ----------------- FLASK health app -----------------
# This remains unchanged, as Flask is a blocking WSGI app
# and is best run in its own thread.
flask_app = Flask(__name__)


@flask_app.route("/health")
def health():
    return "OK", 200


def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# ----------------- MAIN (Async) -----------------
async def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set")
        return

    # Start Flask in a separate thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask health thread started")

    # Set up the PTB Application
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CallbackQueryHandler(callback_handler))

    # Start the scheduler as a background task
    # We store the task object so we can cancel it on shutdown
    scheduler_task = asyncio.create_task(scheduler_loop())
    logger.info("Scheduler task started")

    # --- REVISED BOT STARTUP ---
    # This pattern avoids the `run_polling` bug by manually
    # starting the components and not letting PTB manage the loop.
    try:
        logger.info("Initializing Telegram application...")
        await application.initialize()
        
        logger.info("Starting Telegram polling (background)...")
        await application.updater.start_polling()
        
        logger.info("Starting Telegram application handlers...")
        await application.start()

        logger.info("Bot is now running. Main coroutine will sleep indefinitely.")
        # Keep the main coroutine alive so asyncio.run() doesn't exit
        while True:
            await asyncio.sleep(3600)

    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown signal received.")
    except Exception as e:
        logger.exception("Unhandled error in main async task: %s", e)
    finally:
        logger.info("Shutting down...")
        
        # Stop PTB components
        if application.updater and application.updater.is_running:
            await application.updater.stop()
        if application.running:
            await application.stop()
        await application.shutdown()

        # Cancel our scheduler task
        logger.info("Cancelling scheduler task...")
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            logger.info("Scheduler task cancelled successfully.")

        # Close the global HTTP client
        logger.info("Shutting down http_client...")
        await http_client.aclose()
        logger.info("Async shutdown complete.")


if __name__ == "__main__":
    # asyncio.run() is the modern way. It handles loop creation
    # and shutdown, including signal handling (like KeyboardInterrupt).
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user (KeyboardInterrupt).")
    except Exception as e:
        logger.exception("Critical unhandled error in asyncio.run: %s", e)
    finally:
        logger.info("Process exiting.")

