#!/usr/bin/env python3
"""
AirdropVision - PTB13-compatible single-file bot

- Telegram bot runs in main thread (PTB 13 Updater).
- Scheduler (blockchain scanners) runs in a background thread.
- Flask health endpoint runs in a background thread (for Railway / Render health checks).
- SQLite persistence for seen items and last-processed blocks.
- Uses public RPC endpoints (no paid APIs).
"""

import os
import json
import sqlite3
import time
import logging
import threading
import requests
import asyncio
from typing import Optional
from web3 import Web3
from web3.middleware import geth_poa_middleware
from solana.rpc.async_api import AsyncClient as SolanaClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, CallbackContext
from flask import Flask

# ----------------- CONFIG -----------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL", "1"))  # minutes
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "25"))
BOT_NAME = os.environ.get("BOT_NAME", "AirdropVision")
VERSION = os.environ.get("VERSION", "2.0.0")
DB_PATH = os.environ.get("DB_PATH", "airdropvision.db")

ETH_RPC = os.environ.get("ETH_RPC", "https://cloudflare-eth.com")
POLY_RPC = os.environ.get("POLY_RPC", "https://polygon-rpc.com")
SOLANA_RPC = os.environ.get("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
SOLANA_RPC_FALLBACK = os.environ.get("SOLANA_RPC_FALLBACK", SOLANA_RPC)

MAX_BLOCKS_PER_CYCLE = int(os.environ.get("MAX_BLOCKS_PER_CYCLE", "3"))
TELEGRAM_SEND_DELAY_SEC = float(os.environ.get("TELEGRAM_SEND_DELAY_SEC", "1.0"))
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ----------------- LOGGING -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(BOT_NAME)

# ----------------- WEB3 clients -----------------
w3_eth = Web3(Web3.HTTPProvider(ETH_RPC))
w3_poly = Web3(Web3.HTTPProvider(POLY_RPC))
# fix POA extraData issue for Polygon/geth-style chains
try:
    w3_poly.middleware_onion.inject(geth_poa_middleware, layer=0)
except Exception:
    pass

# ----------------- DB -----------------
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
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute(CREATE_SEEN_SQL)
        self.conn.execute(CREATE_META_SQL)
        self.conn.commit()
        self._lock = threading.Lock()

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

# ----------------- TELEGRAM SENDER (sync, rate-limited, backoff) -----------------
session = requests.Session()

def send_telegram_sync(text: str, parse_mode: str = "Markdown") -> bool:
    """
    Synchronous Telegram sender using HTTP API with exponential backoff.
    Using direct requests so any thread can call this safely.
    """
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

# ----------------- ERC20 PROBE -----------------
def probe_erc20_metadata(contract_address: str, w3: Web3):
    if not contract_address:
        return {}
    try:
        abi = [
            {"constant": True, "inputs": [], "name": "name", "outputs": [{"name": "", "type": "string"}], "type": "function"},
            {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
            {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"}
        ]
        c = w3.eth.contract(address=w3.to_checksum_address(contract_address), abi=abi)
        name = None
        symbol = None
        decimals = None
        try: name = c.functions.name().call()
        except: pass
        try: symbol = c.functions.symbol().call()
        except: pass
        try: decimals = c.functions.decimals().call()
        except: pass
        return {"name": name, "symbol": symbol, "decimals": decimals}
    except Exception as e:
        logger.debug("ERC20 probe failed: %s", e)
        return {}

# ----------------- CHAIN SCANNERS -----------------
def scan_chain_for_contract_creations(w3: Web3, chain_name: str, meta_key: str):
    """
    Synchronous scan of a small range of blocks for contract creation txs.
    Records tx hash in DB 'seen' table.
    """
    try:
        latest = w3.eth.block_number
    except Exception as e:
        logger.error("Failed to get latest block for %s: %s", chain_name, e)
        return

    last = db.meta_get(meta_key)
    try:
        last = int(last) if last is not None else latest - 1
    except Exception:
        last = latest - 1

    start = last + 1
    end = min(latest, start + MAX_BLOCKS_PER_CYCLE - 1)
    if start > end:
        db.meta_set(meta_key, str(latest))
        return

    logger.info("Scanning %s blocks %s..%s", chain_name, start, end)

    for bn in range(start, end + 1):
        try:
            block = w3.eth.get_block(bn, full_transactions=True)
        except Exception as e:
            logger.warning("%s block %s failed: %s", chain_name, bn, e)
            continue

        for tx in block.transactions:
            if tx.to is None:  # contract creation
                tx_hash = tx.hash.hex()
                if db.seen_add(tx_hash, kind=f"{chain_name}_contract_tx"):
                    # attempt to get contract address from receipt
                    try:
                        receipt = w3.eth.get_transaction_receipt(tx_hash)
                        contract_addr = receipt.contractAddress
                    except Exception:
                        contract_addr = None

                    metadata = probe_erc20_metadata(contract_addr, w3) if contract_addr else {}
                    msg = (
                        f"🟢 New {chain_name} contract\n"
                        f"Tx: `{tx_hash}`\n"
                        f"Contract: `{contract_addr}`\n"
                        f"Name: {metadata.get('name')}\n"
                        f"Symbol: {metadata.get('symbol')}"
                    )
                    send_telegram_sync(msg)

    db.meta_set(meta_key, str(end))

async def solana_rpc_get(client: SolanaClient, limit: int):
    resp = await client.get_signatures_for_address("So11111111111111111111111111111111111111112", limit=limit)
    if not isinstance(resp, dict):
        return None
    return resp.get("result")

def scan_solana_sync(limit: int = MAX_RESULTS):
    """
    Wrap async Solana scanning in sync call for the scheduler thread.
    """
    try:
        # prefer primary, fall back to fallback
        results = asyncio.run(_scan_solana_async(SOLANA_RPC, limit))
        if not results and SOLANA_RPC_FALLBACK and SOLANA_RPC_FALLBACK != SOLANA_RPC:
            results = asyncio.run(_scan_solana_async(SOLANA_RPC_FALLBACK, limit))
        if not results:
            logger.warning("Solana RPC returned no results")
            return
        for tx in results:
            sig = tx.get("signature")
            if sig and db.seen_add(sig, "solana_sig"):
                send_telegram_sync(f"🌞 New Solana signature: `{sig}`")
    except Exception as e:
        logger.error("Solana scan sync failed: %s", e)

async def _scan_solana_async(rpc_url: str, limit: int):
    try:
        async with SolanaClient(rpc_url) as client:
            return await solana_rpc_get(client, limit)
    except Exception as e:
        logger.debug("Solana async client error (%s): %s", rpc_url, e)
        return None

# ----------------- SCHEDULER (background thread) -----------------
def scheduler_loop():
    logger.info("Scheduler thread started, interval=%s minute(s)", POLL_INTERVAL_MINUTES)
    while True:
        try:
            scan_chain_for_contract_creations(w3_eth, "Ethereum", "last_eth_block")
            scan_chain_for_contract_creations(w3_poly, "Polygon", "last_poly_block")
            scan_solana_sync(MAX_RESULTS)
            logger.info("Scheduler cycle finished. Seen=%s", db.seen_count())
        except Exception as e:
            logger.exception("Scheduler error: %s", e)
        # sleep
        time.sleep(POLL_INTERVAL_MINUTES * 60)

# ----------------- TELEGRAM COMMANDS -----------------
def start_cmd(update: Update, context: CallbackContext):
    keyboard = [[InlineKeyboardButton("📊 Stats", callback_data="stats"),
                 InlineKeyboardButton("🚀 Force Scan", callback_data="scan_now")]]
    text = f"🤖 {BOT_NAME} v{VERSION}\nPolling every {POLL_INTERVAL_MINUTES}m\nSeen: {db.seen_count()}"
    update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

def callback_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    if not query:
        return
    query.answer()
    if query.data == "stats":
        query.edit_message_text(f"📊 {BOT_NAME} v{VERSION}\nTracked: {db.seen_count()} items.")
    elif query.data == "scan_now":
        query.edit_message_text("⏳ Running manual scan...")
        # do quick scans in background so callback returns quickly
        threading.Thread(target=scan_chain_for_contract_creations, args=(w3_eth, "Ethereum", "last_eth_block"), daemon=True).start()
        threading.Thread(target=scan_chain_for_contract_creations, args=(w3_poly, "Polygon", "last_poly_block"), daemon=True).start()
        threading.Thread(target=scan_solana_sync, daemon=True).start()
        query.edit_message_text("✅ Manual scan started.")

# ----------------- FLASK health app (background thread) -----------------
flask_app = Flask(__name__)

@flask_app.route("/health")
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    # Flask dev server is okay for Railway health endpoints (not public heavy use)
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ----------------- MAIN -----------------
def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set")
        return

    # Start Flask thread (so Railway/Render can healthcheck)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask health thread started")

    # Start scheduler thread
    sched_thread = threading.Thread(target=scheduler_loop, daemon=True)
    sched_thread.start()
    logger.info("Scheduler thread started")

    # Start Telegram Updater in main thread (PTB 13)
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

if __name__ == "__main__":
    main()
