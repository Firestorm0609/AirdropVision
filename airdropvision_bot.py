#!/usr/bin/env python3
"""
AirdropVision - PTB v13 compatible version (synchronous Updater)
Drop this file into the repository root as `app.py`.
"""

import os
import json
import time
import sqlite3
import logging
import requests
import asyncio
import threading
from datetime import datetime
from typing import Optional
from web3 import Web3
from solana.rpc.async_api import AsyncClient as SolanaClient
from flask import Flask

# ----------------- CONFIG -----------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL", "1"))  # minimum: 1 minute by default
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "25"))
BOT_NAME = os.environ.get("BOT_NAME", "AirdropVision")
VERSION = os.environ.get("VERSION", "2.0.0")
DB_PATH = os.environ.get("DB_PATH", "airdropvision.db")

# Free RPC endpoints (no paid API required)
ETH_RPC = os.environ.get("ETH_RPC", "https://cloudflare-eth.com")
POLY_RPC = os.environ.get("POLY_RPC", "https://polygon-rpc.com")
SOLANA_RPC = os.environ.get("SOLANA_RPC", "https://api.mainnet-beta.solana.com")

# Operational limits
MAX_BLOCKS_PER_CYCLE = int(os.environ.get("MAX_BLOCKS_PER_CYCLE", "4"))
TELEGRAM_SEND_DELAY_SEC = float(os.environ.get("TELEGRAM_SEND_DELAY_SEC", "1.0"))

# ----------------- LOGGING -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(BOT_NAME)

# ----------------- WEB3 CLIENTS -----------------
w3_eth = Web3(Web3.HTTPProvider(ETH_RPC))
w3_poly = Web3(Web3.HTTPProvider(POLY_RPC))

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
        self.path = path
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute(CREATE_SEEN_SQL)
        self.conn.execute(CREATE_META_SQL)
        self.conn.commit()

    def seen_add(self, id: str, kind: str = "generic", meta: Optional[dict] = None) -> bool:
        try:
            self.conn.execute("INSERT INTO seen(id, kind, meta) VALUES (?, ?, ?)", (id, kind, json.dumps(meta or {})))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def seen_count(self) -> int:
        cur = self.conn.execute("SELECT COUNT(1) FROM seen")
        return cur.fetchone()[0]

    def meta_get(self, k: str, default: Optional[str] = None) -> Optional[str]:
        cur = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,))
        row = cur.fetchone()
        return row[0] if row else default

    def meta_set(self, k: str, v: str):
        self.conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES (?,?)", (k, v))
        self.conn.commit()

db = DB()

# ----------------- TELEGRAM SENDER (sync, rate-limited + backoff) -----------------
session = requests.Session()
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

def send_telegram(text: str, parse_mode: str = "Markdown") -> bool:
    """
    Synchronous Telegram sender with simple exponential backoff and 429 handling.
    Safe to call from threads.
    """
    url = f"{TELEGRAM_API_BASE}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": parse_mode}

    backoff = 1
    while True:
        try:
            r = session.post(url, json=payload, timeout=10)
        except requests.RequestException as e:
            logger.warning("Telegram send exception: %s", e)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        if r.status_code == 200:
            time.sleep(TELEGRAM_SEND_DELAY_SEC)
            return True
        elif r.status_code == 429:
            try:
                body = r.json()
                retry_after = int(body.get("parameters", {}).get("retry_after", 5))
            except Exception:
                retry_after = backoff
            logger.warning("Telegram rate limited, retrying after %s seconds", retry_after)
            time.sleep(retry_after)
            backoff = min(backoff * 2, 60)
            continue
        else:
            logger.error("Telegram send failed %s -> %s", r.status_code, r.text)
            return False

# ----------------- SCANNERS -----------------
def probe_erc20_metadata(contract_address: str, w3: Web3) -> dict:
    """Try to read name/symbol/decimals from a contract (best-effort)."""
    if not contract_address:
        return {}
    try:
        erc20_abi = [
            {"constant":True,"inputs":[],"name":"name","outputs":[{"name":"","type":"string"}],"type":"function"},
            {"constant":True,"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"type":"function"},
            {"constant":True,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}
        ]
        c = w3.eth.contract(address=w3.to_checksum_address(contract_address), abi=erc20_abi)
        name = None
        symbol = None
        decimals = None
        try:
            name = c.functions.name().call()
        except: pass
        try:
            symbol = c.functions.symbol().call()
        except: pass
        try:
            decimals = c.functions.decimals().call()
        except: pass
        return {"name": name, "symbol": symbol, "decimals": decimals}
    except Exception as e:
        logger.debug("ERC20 probe failed: %s", e)
        return {}

def scan_chain_for_contract_creations(w3: Web3, chain_name: str, meta_key_last_block: str):
    try:
        latest = w3.eth.block_number
    except Exception as e:
        logger.error("Failed to get latest block for %s: %s", chain_name, e)
        return

    last = db.meta_get(meta_key_last_block)
    try:
        last = int(last) if last is not None else latest - 1
    except:
        last = latest - 1

    start = last + 1
    end = min(latest, start + MAX_BLOCKS_PER_CYCLE - 1)
    if start > end:
        db.meta_set(meta_key_last_block, str(latest))
        return

    logger.info("Scanning %s blocks %s..%s", chain_name, start, end)
    for bn in range(start, end + 1):
        try:
            block = w3.eth.get_block(bn, full_transactions=True)
        except Exception as e:
            logger.warning("Failed to fetch block %s on %s: %s", bn, chain_name, e)
            continue
        for tx in block.transactions:
            try:
                if tx.to is None:
                    # contract creation
                    tx_hash = tx.hash.hex()
                    if db.seen_add(tx_hash, kind=f"{chain_name}_contract_tx"):
                        # probe receipt to get contract address
                        try:
                            receipt = w3.eth.get_transaction_receipt(tx_hash)
                            contract_addr = receipt.contractAddress
                        except Exception:
                            contract_addr = None

                        metadata = probe_erc20_metadata(contract_addr, w3) if contract_addr else {}
                        msg = (
                            f"🟢 New {chain_name} contract creation\n"
                            f"Tx: `{tx_hash}`\n"
                            f"Contract: `{contract_addr}`\n"
                            f"Name: {metadata.get('name')}\n"
                            f"Symbol: {metadata.get('symbol')}"
                        )
                        # synchronous send
                        send_telegram(msg)
            except Exception as e:
                logger.debug("Error processing tx in block %s: %s", bn, e)

    db.meta_set(meta_key_last_block, str(end))

async def scan_solana(latest_limit: int = MAX_RESULTS):
    try:
        async with SolanaClient(SOLANA_RPC) as client:
            resp = await client.get_signatures_for_address("So11111111111111111111111111111111111111112", limit=latest_limit)
            results = resp.get('result') if isinstance(resp, dict) else None
            if not results:
                logger.debug("No solana results or unexpected response: %s", resp)
                return
            for tx in results:
                sig = tx.get('signature')
                if sig and db.seen_add(sig, kind='solana_sig'):
                    # run blocking send_telegram in thread pool to avoid blocking asyncio loop
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, send_telegram, f"🌞 New Solana signature: `{sig}`")
    except Exception as e:
        logger.error("Solana scan failed: %s", e)

# ----------------- SCHEDULER -----------------
async def scheduler_loop():
    logger.info("Starting scheduler: poll every %s minute(s)", POLL_INTERVAL_MINUTES)
    while True:
        try:
            # Ethereum
            scan_chain_for_contract_creations(w3_eth, 'Ethereum', 'last_eth_block')
            # Polygon
            scan_chain_for_contract_creations(w3_poly, 'Polygon', 'last_poly_block')
            # Solana (async)
            await scan_solana()
            logger.info("Cycle finished. Seen count=%s", db.seen_count())
        except Exception as e:
            logger.exception("Scheduler loop error: %s", e)
        await asyncio.sleep(POLL_INTERVAL_MINUTES * 60)

def start_scheduler_in_thread():
    def runner():
        try:
            asyncio.run(scheduler_loop())
        except Exception as e:
            logger.exception("Scheduler thread crashed: %s", e)
    t = threading.Thread(target=runner, daemon=True)
    t.start()

# ----------------- TELEGRAM BOT HANDLERS (PTB v13) -----------------
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, CallbackContext

def start_cmd(update: Update, context: CallbackContext):
    keyboard = [
        [InlineKeyboardButton("📊 Stats", callback_data="stats"),
         InlineKeyboardButton("🚀 Force Scan", callback_data="scan_now")],
    ]
    text = f"🤖 *{BOT_NAME} v{VERSION}*\nPolling every {POLL_INTERVAL_MINUTES} minute(s)\nSeen: {db.seen_count()}"
    update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

def button_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    if query.data == 'stats':
        query.edit_message_text(f"📊 {BOT_NAME} v{VERSION}\nTracked: {db.seen_count()} items.")
    elif query.data == 'scan_now':
        query.edit_message_text("⏳ Running manual scan...")
        # Run quick scans
        scan_chain_for_contract_creations(w3_eth, 'Ethereum', 'last_eth_block')
        scan_chain_for_contract_creations(w3_poly, 'Polygon', 'last_poly_block')
        # run solana scan synchronously
        try:
            asyncio.run(scan_solana())
        except Exception as e:
            logger.exception("Manual solana scan failed: %s", e)
        query.edit_message_text("✅ Scan complete!")

def run_telegram_bot():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set in environment variables.")
        raise SystemExit(1)

    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start_cmd))
    dp.add_handler(CallbackQueryHandler(button_cb))

    # Start scheduler background thread
    start_scheduler_in_thread()

    # Start Flask health endpoint in a thread so Railway/Render can hit it
    def run_flask():
        health_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
    threading.Thread(target=run_flask, daemon=True).start()

    logger.info("Starting Telegram polling...")
    updater.start_polling()
    updater.idle()

# --- Flask health endpoint ---
health_app = Flask(__name__)
@health_app.route('/')
def health():
    return 'OK', 200

if __name__ == '__main__':
    try:
        run_telegram_bot()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
