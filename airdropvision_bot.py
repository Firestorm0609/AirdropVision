#!/usr/bin/env python3
"""
AirdropVision - improved & patched
"""

import os
import json
import time
import sqlite3
import logging
import requests
import asyncio
from datetime import datetime
from typing import Optional
from web3 import Web3
from web3.middleware import geth_poa_middleware
from solana.rpc.async_api import AsyncClient as SolanaClient

# ----------------- CONFIG -----------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL", "1"))
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "25"))
BOT_NAME = os.environ.get("BOT_NAME", "AirdropVision")
VERSION = os.environ.get("VERSION", "2.0.0")
DB_PATH = os.environ.get("DB_PATH", "airdropvision.db")

ETH_RPC = os.environ.get("ETH_RPC", "https://cloudflare-eth.com")
POLY_RPC = os.environ.get("POLY_RPC", "https://polygon-rpc.com")
SOLANA_RPC = os.environ.get("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
SOLANA_RPC_FALLBACK = "https://api.mainnet-beta.solana.com"

MAX_BLOCKS_PER_CYCLE = int(os.environ.get("MAX_BLOCKS_PER_CYCLE", "4"))
TELEGRAM_SEND_DELAY_SEC = float(os.environ.get("TELEGRAM_SEND_DELAY_SEC", "1.0"))

# ----------------- LOGGING -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(BOT_NAME)

# ----------------- WEB3 CLIENTS -----------------
w3_eth = Web3(Web3.HTTPProvider(ETH_RPC))

w3_poly = Web3(Web3.HTTPProvider(POLY_RPC))
# Polygon POA fix
w3_poly.middleware_onion.inject(geth_poa_middleware, layer=0)

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
            self.conn.execute("INSERT INTO seen(id, kind, meta) VALUES (?, ?, ?)",
                              (id, kind, json.dumps(meta or {})))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def seen_count(self) -> int:
        cur = self.conn.execute("SELECT COUNT(1) FROM seen")
        return cur.fetchone()[0]

    def meta_get(self, k: str, default=None):
        cur = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,))
        row = cur.fetchone()
        return row[0] if row else default

    def meta_set(self, k: str, v: str):
        self.conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES (?,?)", (k, v))
        self.conn.commit()


db = DB()

# ----------------- TELEGRAM SENDER -----------------
session = requests.Session()
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

async def telegram_send(text: str, parse_mode: str = "Markdown"):
    url = f"{TELEGRAM_API_BASE}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": parse_mode}

    backoff = 1
    while True:
        try:
            r = session.post(url, json=payload, timeout=10)
        except Exception as e:
            logger.warning("Telegram error: %s", e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        if r.status_code == 200:
            await asyncio.sleep(TELEGRAM_SEND_DELAY_SEC)
            return True

        if r.status_code == 429:
            body = r.json()
            retry_after = int(body.get("parameters", {}).get("retry_after", 5))
            logger.warning("Rate-limited. retry_after=%s", retry_after)
            await asyncio.sleep(retry_after)
            continue

        logger.error("Telegram request failed: %s %s", r.status_code, r.text)
        return False


# ----------------- ERC20 PROBE -----------------
def probe_erc20_metadata(contract_address: str, w3: Web3):
    try:
        abi = [
            {"constant": True, "inputs": [], "name": "name",
             "outputs": [{"name": "", "type": "string"}], "type": "function"},
            {"constant": True, "inputs": [], "name": "symbol",
             "outputs": [{"name": "", "type": "string"}], "type": "function"},
            {"constant": True, "inputs": [], "name": "decimals",
             "outputs": [{"name": "", "type": "uint8"}], "type": "function"}
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
    except:
        return {}


# ----------------- ETH + POLYGON SCANNER -----------------
def scan_chain_for_contract_creations(w3: Web3, chain_name: str, meta_key: str):
    try:
        latest = w3.eth.block_number
    except Exception as e:
        logger.error("Failed to get latest block for %s: %s", chain_name, e)
        return

    last = db.meta_get(meta_key)
    last = int(last) if last else latest - 1

    start = last + 1
    end = min(latest, start + MAX_BLOCKS_PER_CYCLE - 1)

    if start > end:
        db.meta_set(meta_key, str(latest))
        return

    logger.info("Scanning %s blocks %s..%s", chain_name, start, end)

    loop = asyncio.get_event_loop()

    for bn in range(start, end + 1):
        try:
            block = w3.eth.get_block(bn, full_transactions=True)
        except Exception as e:
            logger.warning("%s block %s failed: %s", chain_name, bn, e)
            continue

        for tx in block.transactions:
            if tx.to is None:
                tx_hash = tx.hash.hex()

                if db.seen_add(tx_hash, kind=f"{chain_name}_contract_tx"):
                    try:
                        receipt = w3.eth.get_transaction_receipt(tx_hash)
                        contract_addr = receipt.contractAddress
                    except:
                        contract_addr = None

                    metadata = probe_erc20_metadata(contract_addr, w3) if contract_addr else {}

                    msg = (
                        f"🟢 New {chain_name} contract\n"
                        f"Tx: `{tx_hash}`\n"
                        f"Contract: `{contract_addr}`\n"
                        f"Name: {metadata.get('name')}\n"
                        f"Symbol: {metadata.get('symbol')}"
                    )

                    asyncio.run_coroutine_threadsafe(telegram_send(msg), loop)

    db.meta_set(meta_key, str(end))


# ----------------- SOLANA SCANNER -----------------
async def solana_rpc_get(client, limit):
    resp = await client.get_signatures_for_address(
        "So11111111111111111111111111111111111111112", limit=limit
    )
    if not isinstance(resp, dict):
        return None
    if "result" not in resp or not isinstance(resp["result"], list):
        return None
    return resp["result"]


async def scan_solana(latest_limit: int = MAX_RESULTS):
    try:
        async with SolanaClient(SOLANA_RPC) as client:
            results = await solana_rpc_get(client, latest_limit)

            if not results:
                logger.warning("Bad Solana RPC response, retrying fallback")
                async with SolanaClient(SOLANA_RPC_FALLBACK) as fallback:
                    results = await solana_rpc_get(fallback, latest_limit)

            if not results:
                logger.warning("Solana RPC failed entirely")
                return

            for tx in results:
                sig = tx.get("signature")
                if sig and db.seen_add(sig, "solana_sig"):
                    asyncio.ensure_future(telegram_send(f"🌞 New Solana signature: `{sig}`"))
    except Exception as e:
        logger.error("Solana scan failed: %s", e)


# ----------------- SCHEDULER -----------------
async def scheduler_loop():
    logger.info("Scheduler running every %s minute(s)", POLL_INTERVAL_MINUTES)
    while True:
        try:
            scan_chain_for_contract_creations(w3_eth, "Ethereum", "last_eth_block")
            scan_chain_for_contract_creations(w3_poly, "Polygon", "last_poly_block")
            await scan_solana()
            logger.info("Cycle complete. Seen=%s", db.seen_count())
        except Exception as e:
            logger.exception("Scheduler error: %s", e)

        await asyncio.sleep(POLL_INTERVAL_MINUTES * 60)


# ----------------- TELEGRAM BOT (PTB 13) -----------------
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, CallbackContext

def start_cmd(update: Update, context: CallbackContext):
    keyboard = [[
        InlineKeyboardButton("📊 Stats", callback_data="stats"),
        InlineKeyboardButton("🚀 Force Scan", callback_data="scan_now")
    ]]
    text = f"🤖 {BOT_NAME} v{VERSION}\nPolling every {POLL_INTERVAL_MINUTES}m\nSeen: {db.seen_count()}"
    update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

def button_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    if query.data == "stats":
        query.edit_message_text(f"📊 Stats\nTracked: {db.seen_count()} items.")
    elif query.data == "scan_now":
        query.edit_message_text("⏳ Manual scan running...")
        scan_chain_for_contract_creations(w3_eth, "Ethereum", "last_eth_block")
        scan_chain_for_contract_creations(w3_poly, "Polygon", "last_poly_block")
        asyncio.get_event_loop().create_task(scan_solana())
        query.edit_message_text("✅ Manual scan complete!")

async def launch_scheduler():
    await scheduler_loop()


# ----------------- FLASK HEALTH ENDPOINT -----------------
from flask import Flask
health_app = Flask(__name__)

@health_app.route("/")
def health():
    return "OK", 200


# ----------------- MAIN -----------------
def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID")
        return

    loop = asyncio.get_event_loop()
    loop.create_task(scheduler_loop())

    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start_cmd))
    dp.add_handler(CallbackQueryHandler(button_cb))

    updater.start_polling()
    logger.info("Bot running...")
    updater.idle()


# -------------------------
# Production Entry (Railway)
# -------------------------
import threading
from flask import Flask

app = Flask(__name__)

@app.route("/health")
def health():
    return "OK", 200


def run_bot():
    """Runs the Telegram bot (PTB 13) inside a thread."""
    try:
        from telegram.ext import Updater
        updater = Updater(TELEGRAM_TOKEN, use_context=True)

        dispatcher = updater.dispatcher

        # attach your handlers again if needed
        # dispatcher.add_handler(CommandHandler("start", start))
        # dispatcher.add_handler(CallbackQueryHandler(button_handler))

        updater.start_polling()
        updater.idle()
    except Exception as e:
        print("BOT THREAD CRASHED:", e)


def run_scheduler():
    """Runs your scheduled loops, blockchain scanners, etc."""
    try:
        while True:
            run_polygon_scan()      # your own function
            run_solana_scan()       # your own function
            time.sleep(SCAN_INTERVAL)
    except Exception as e:
        print("SCHEDULER CRASHED:", e)


if __name__ == "__main__":
    print("Starting AirdropVision in multi-thread mode...")

    # thread: telegram bot
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    # thread: main blockchain scanner / scheduler
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    # Railway only pings Flask, so Flask is the main process
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        debug=False,
        use_reloader=False
    )
