# AirdropVision Bot

A Python-based Telegram bot that scans Ethereum, Polygon, and Solana for new token deployments and signatures.

## Features

* Scans Ethereum and Polygon using public RPC endpoints.
* Solana signature scanning via public RPC.
* Telegram bot alerts.
* SQLite database persistence.
* Rate limiting to avoid Telegram 429 errors.
* Flask health endpoint for Railway health checks.

## Setup

1. Clone this repo.
2. Install dependencies:

```
pip install -r requirements.txt
```

3. Create `.env` file with:

```
TELEGRAM_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
POLL_INTERVAL=1
MAX_RESULTS=25
```

4. Run locally:

```
python app.py
```

## Deployment on Railway

1. Connect GitHub repo to Railway.
2. Set environment variables in project settings.
3. Deploy using included `Procfile`.

## GitHub Actions for Termux

Termux-compatible GitHub Actions can be added for CI workflows.

