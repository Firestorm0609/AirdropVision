# AirdropVision Bot
A Python-based Telegram bot that scans Ethereum, Polygon, and Solana for new Airdrops and NFT launches. Built with Python and deployed on Railway.

---
## Features
- Telegram-only bot
- Tracks ETH and SOL wallets
- Uses free, non-paid public APIs
- Auto-restarter via Railway health checks
- SQLite persistent storage
- Automatic token metadata scraping
- Clean logging

---
## Requirements
- Python 3.11+
- `python-telegram-bot==13.15`
- Free API keys

---
## Environment Variables
Create a `.env` file or set these in Railway:
TELEGRAM_TOKEN=your_bot_token ETHERSCAN_API_KEY=your_etherscan_key SOLANA_RPC=https://api.mainnet-beta.solana.com PORT=8080

---
## Local Setup

git clone https://github.com/Firestorm0609/AirdropVision.git cd AirdropVision

python3 -m venv .venv source .venv/bin/activate

pip install -r requirements.txt python airdropvision_bot.py

---
## Railway Deployment
1. Login to https://railway.app
2. Create **New Project**
3. Choose **Deploy from GitHub**
4. Select repository: `Firestorm0609/AirdropVision`
5. Go to "Variables" and add:
   - `TELEGRAM_TOKEN`
   - `ETHERSCAN_API_KEY`
   - `SOLANA_RPC`
   - `PORT=8080`
6. Railway automatically runs `Procfile`
7. Visit `/health` endpoint to confirm service is running

---
## Procfile

web: python airdropvision_bot.py

---
## runtime.txt

python-3.11.9

---
## License
MIT

