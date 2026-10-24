#!/usr/bin/env python3
"""Opt Telegram users in or out from /start and /stop before each stock check."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "telegram_subscriptions.json"


def load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return {"offset": 0, "subscribers": [], "unsubscribed_chat_ids": []}
    if not isinstance(state, dict):
        raise RuntimeError("Subscription state must be a JSON object")
    return state


def save_state(state: dict) -> None:
    temp_file = STATE_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(state, indent=2))
    temp_file.chmod(0o600)
    temp_file.replace(STATE_FILE)


def reply(token: str, chat_id: str, message: str) -> None:
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": message},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram API rejected the reply"))


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("Subscription sync skipped: TELEGRAM_BOT_TOKEN is not set.", file=sys.stderr)
        return 1

    try:
        state = load_state()
        offset = int(state.get("offset", 0))
        subscribers = set(str(value) for value in state.get("subscribers", []))
        opted_out = set(str(value) for value in state.get("unsubscribed_chat_ids", []))
        response = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": offset, "timeout": 0, "allowed_updates": '["message"]'},
            timeout=25,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "Telegram API rejected getUpdates"))
    except Exception as exc:
        print(str(exc).replace(token, "[redacted]"), file=sys.stderr, flush=True)
        return 1

    next_offset = offset
    for update in data.get("result", []):
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            next_offset = max(next_offset, update_id + 1)
        message = update.get("message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat", {})
        if not isinstance(chat, dict) or chat.get("type") != "private" or chat.get("id") is None:
            continue
        chat_id = str(chat["id"])
        text = message.get("text", "")
        if not isinstance(text, str) or not text.strip():
            continue
        command = re.split(r"\s+", text.strip(), maxsplit=1)[0].split("@", 1)[0].lower()
        if command == "/start":
            subscribers.add(chat_id)
            opted_out.discard(chat_id)
            try:
                reply(token, chat_id, "Subscribed to Amul stock change alerts. Send /stop to unsubscribe.")
            except Exception as exc:
                print(str(exc).replace(token, "[redacted]"), file=sys.stderr, flush=True)
        elif command == "/stop":
            subscribers.discard(chat_id)
            opted_out.add(chat_id)
            try:
                reply(token, chat_id, "Unsubscribed from Amul stock alerts.")
            except Exception as exc:
                print(str(exc).replace(token, "[redacted]"), file=sys.stderr, flush=True)

    state["offset"] = next_offset
    state["subscribers"] = sorted(subscribers)
    state["unsubscribed_chat_ids"] = sorted(opted_out)
    try:
        save_state(state)
    except Exception as exc:
        print(f"Could not save Telegram subscriptions: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
