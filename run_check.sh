#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
set -a
source "$SCRIPT_DIR/.env"
set +a
PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
if ! "$PYTHON_BIN" "$SCRIPT_DIR/sync_telegram_subscriptions.py"; then
    echo "Warning: Telegram subscription sync failed; checking saved recipients." >&2
fi
exec "$PYTHON_BIN" "$SCRIPT_DIR/check_stock.py"
