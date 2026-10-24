#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
set -a
source "$SCRIPT_DIR/.env"
set +a
exec "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/check_stock.py"
