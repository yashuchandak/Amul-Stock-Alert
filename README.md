# Amul stock alert (560103)

A headless Playwright checker for Amul Chocolate Whey Protein 34 g, packs of 60 and 30 sachets. It selects pincode 560103 in the Amul storefront, checks each product page's Sold Out / Add to Cart state, and sends a Telegram message when either pack's availability changes. It never places an order.

## Install on Ubuntu VPS

Copy this folder to the VPS, then run:

```bash
sudo apt update
sudo apt install -y python3 python3-venv
cd amul-stock-alert
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
cp .env.example .env
nano .env
chmod 600 .env
```

Set up Telegram alerts:

1. In Telegram, message `@BotFather`, send `/newbot`, and follow its steps. Copy the bot token into `TELEGRAM_BOT_TOKEN` in `.env`.
2. Open your new bot and send it `/start`.
3. On the VPS, run `./get_telegram_chat_id.sh`. Copy your private chat ID from its output into `TELEGRAM_CHAT_ID` in `.env`.

Keep the bot token private. The pincode and both product URLs are already filled in; edit them if needed. Existing `.env` files using `AMUL_PRODUCT_URL` continue to set the 60 sachet product. You can instead set `AMUL_PRODUCT_URL_60` and `AMUL_PRODUCT_URL_30` independently.

Run one manual check:

```bash
./run_check.sh
```

The first check creates `stock_state.json`. Each pack is tracked independently; Telegram alerts are sent when a pack is first checked or its status changes in either direction. Repeated checks with the same status do not send duplicate messages. The old single-pack state is migrated automatically as the 60 sachet status.

## Run every 30 minutes with cron

```bash
mkdir -p logs
crontab -e
```

Add this line, replacing `/path/to/amul-stock-alert` with the actual folder on the VPS:

```cron
*/30 * * * * flock -n /tmp/amul-stock-check.lock /path/to/amul-stock-alert/run_check.sh >> /path/to/amul-stock-alert/logs/stock.log 2>&1
```

The lock prevents overlapping runs. Review logs with:

```bash
tail -n 50 /path/to/amul-stock-alert/logs/stock.log
```

## Availability request note

Amul's storefront is built on StoreHippo and loads product data through its `ms.products` entity API. Delivery pincode is selected in the storefront session, so a bare product API URL can report generic or wrong-region availability. This checker uses the site's own pincode selection flow and reads the resulting location-specific product state. If Amul changes its page markup, the selectors in `check_stock.py` may need an update.
