# AirdropVision Bot

[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/release/python-3115/)

AirdropVision is an off-chain Python bot that monitors NFT and crypto sources for potential free mints and airdrops, sending real-time alerts to a Telegram channel.

It runs 24/7 on hosting platforms like Railway and provides a public health-check endpoint.

## 🚀 Features

* **NFTCalendar Scanner**: Monitors the `nftcalendar.io` API for upcoming drops and flags potential free mints.
* **Nitter Scraper**: Scrapes public Nitter (Twitter) instances for tweets matching "free mint" and "airdrop" keywords.
* **Telegram Alerts**: Sends formatted, instant alerts to a configured Telegram chat.
* **Persistent Database**: Uses `SQLite` to remember all items it has already seen, preventing duplicate notifications.
* **Health Check**: Runs a lightweight `Flask` web server on `/health` for easy monitoring by hosting platforms.
* **Async & Efficient**: Built on `asyncio` and `python-telegram-bot` v20+ for efficient, non-blocking I/O.
* **Telegram Commands**: Includes interactive `/start` command with "Stats" and "Force Scan" buttons.

## 📦 Deployment

This bot is designed to be deployed on a service like [Railway](https://railway.app/).

### 1. Fork to GitHub

Fork this repository to your own GitHub account.

### 2. Deploy on Railway

1.  Create a new project on Railway and link it to your GitHub repository.
2.  Railway will automatically detect the `Procfile` and `runtime.txt`.
3.  Go to the "Variables" tab in your Railway project.
4.  Add your secrets from the `.env.example` file. You **must** set:
    * `TELEGRAM_TOKEN`
    * `TELEGRAM_CHAT_ID`
5.  Railway will deploy the app. The "Deploy Logs" will show the bot starting up.

### 3. Database (Railway)

The bot uses `SQLite`, which writes to a file (`airdropvision_offchain.db`). This will not persist across deploys on Railway unless you attach a volume.

1.  In your Railway project, add a **Volume**.
2.  Set the **Mount Path** to `/app` (or just the directory where your `DB_PATH` is, e.g., `/data`).
3.  If you set the `DB_PATH` environment variable to `/data/airdropvision.db`, your database will persist.

## 🛠️ Local Development

1.  Clone the repository:
    ```bash
    git clone [https://github.com/YOUR_USERNAME/AirdropVision.git](https://github.com/YOUR_USERNAME/AirdropVision.git)
    cd AirdropVision
    ```
2.  Create a virtual environment:
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    ```
3.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```
4.  Create your `.env` file:
    ```bash
    cp .env.example .env
    ```
5.  Edit `.env` and add your `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`.
6.  Run the bot:
    ```bash
    python3 main.py
    ```

