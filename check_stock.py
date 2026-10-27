#!/usr/bin/env python3
"""Check Amul product availability for one pincode and notify via Telegram."""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "stock_state.json"
SUBSCRIPTIONS_FILE = ROOT / "telegram_subscriptions.json"
HOME_URL = "https://shop.amul.com/en/"


def required(name: str, default: str = "") -> str:
    value = os.getenv(name, default).strip()
    if not value:
        raise RuntimeError(f"Missing {name}; set it in .env")
    return value


def telegram_chat_ids() -> list[str]:
    """Merge explicit recipients with people who opted in through /start."""
    raw = os.getenv("TELEGRAM_CHAT_IDS", "").strip()
    if raw:
        ids = [chat_id.strip() for chat_id in raw.split(",") if chat_id.strip()]
    else:
        legacy_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        ids = [legacy_id] if legacy_id else []
    try:
        subscriptions = json.loads(SUBSCRIPTIONS_FILE.read_text())
    except FileNotFoundError:
        subscriptions = {}
    dynamic_ids = subscriptions.get("subscribers", [])
    opted_out = set(subscriptions.get("unsubscribed_chat_ids", []))
    if isinstance(dynamic_ids, list):
        ids.extend(str(chat_id) for chat_id in dynamic_ids if str(chat_id) not in opted_out)
    # Keep order stable and avoid sending twice if an ID appears more than once.
    ids = [chat_id for chat_id in dict.fromkeys(ids) if chat_id not in opted_out]
    if not ids:
        raise RuntimeError("Set TELEGRAM_CHAT_IDS or TELEGRAM_CHAT_ID in .env")
    return ids


def send_telegram(token: str, chat_ids: list[str], text: str) -> list[str]:
    """Send to every recipient and return only chat IDs whose delivery failed."""
    failures: list[str] = []
    for index, chat_id in enumerate(chat_ids, start=1):
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
                timeout=20,
            )
            response.raise_for_status()
            body = response.json()
            if not body.get("ok"):
                raise RuntimeError(body.get("description", "Telegram API rejected the message"))
        except Exception as exc:
            # HTTP exceptions can include the bot token because it is part of the API URL.
            detail = str(exc).replace(token, "[redacted]")
            print(f"Telegram delivery failed for recipient #{index}: {detail}",
                  file=sys.stderr, flush=True)
            failures.append(chat_id)
    return failures


def load_previous_state(legacy_chat_id: str = "") -> dict[str, dict[str, object]]:
    try:
        data = json.loads(STATE_FILE.read_text())
        # Keep the old single-product state readable during the upgrade.
        if isinstance(data.get("in_stock"), bool):
            return {"60": {
                "in_stock": data["in_stock"],
                "notified_chat_ids": [legacy_chat_id] if legacy_chat_id else [],
                "pending_chat_ids": [],
            }}
        products = data.get("products", {})
        previous = {}
        for pack, entry in products.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("in_stock"), bool):
                continue
            notified = entry.get("notified_chat_ids")
            pending = entry.get("pending_chat_ids", [])
            previous[str(pack)] = {
                "in_stock": entry["in_stock"],
                "notified_chat_ids": notified if isinstance(notified, list)
                else ([legacy_chat_id] if legacy_chat_id else []),
                "pending_chat_ids": pending if isinstance(pending, list) else [],
            }
        return previous
    except FileNotFoundError:
        return {}


def save_state(products: dict[str, tuple[bool, str, list[str], list[str]]]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "products": {
            pack: {
                "in_stock": in_stock,
                "product": title,
                "notified_chat_ids": notified,
                "pending_chat_ids": pending,
            }
            for pack, (in_stock, title, notified, pending) in products.items()
        },
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
    }, indent=2))
    tmp.chmod(0o600)
    tmp.replace(STATE_FILE)


def check_site(pincode: str, product_url: str) -> tuple[bool, str, str]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45000)
            # A fresh Amul session opens the location modal automatically. Its
            # heading says "Select Delivery Pincode" and is not a trigger button;
            # clicking that heading is intercepted by the modal backdrop.
            # Wait for the modal's actual search field instead of clicking text.
            pin_field = page.locator('#locationWidgetModal input#search')
            try:
                pin_field.wait_for(state="visible", timeout=15000)
            except PlaywrightTimeoutError as exc:
                raise RuntimeError(
                    "Amul's pincode dialog did not open. "
                    f"Current page: {page.url!r}; title: {page.title()!r}."
                ) from exc
            pin_field.fill(pincode)
            page.get_by_role("button", name=pincode, exact=True).click(timeout=15000)
            location_modal = page.locator("#locationWidgetModal")
            try:
                location_modal.wait_for(state="hidden", timeout=15000)
            except PlaywrightTimeoutError as exc:
                try:
                    excerpt = " ".join(page.locator("body").inner_text(timeout=3000).split())[:800]
                except Exception:
                    excerpt = "<page body unavailable>"
                raise RuntimeError(
                    f"Amul did not close the pincode dialog after selecting {pincode}. "
                    f"Visible text: {excerpt!r}"
                ) from exc

            # The selected pincode is now in the Amul storefront session. Load the product
            # in that same browser context so the displayed availability is location-aware.
            page.goto(product_url, wait_until="domcontentloaded", timeout=45000)
            detail = page.locator(".product-detail")
            title_locator = detail.locator("h1")
            try:
                title_locator.wait_for(state="visible", timeout=30000)
            except PlaywrightTimeoutError as exc:
                try:
                    excerpt = page.locator("body").inner_text(timeout=3000)
                except Exception:
                    excerpt = "<page body unavailable>"
                excerpt = " ".join(excerpt.split())[:1000]
                debug_image = ROOT / "amul-debug.png"
                try:
                    page.screenshot(path=str(debug_image), full_page=True, timeout=10000)
                    image_note = f" Screenshot saved to {debug_image}."
                except Exception:
                    image_note = " Screenshot could not be saved."
                raise RuntimeError(
                    "Product details did not load after selecting the pincode. "
                    f"Current page: {page.url!r}; title: {page.title()!r}; "
                    f"visible text: {excerpt!r}.{image_note}"
                ) from exc
            # Amul's Add to Cart anchor is the stock signal. Its disabled
            # attribute is "0" when available and "true" when unavailable.
            add_to_cart = detail.locator(
                "a", has_text=re.compile(r"^\s*Add to Cart\s*$", re.I)
            )
            try:
                add_to_cart.wait_for(state="visible", timeout=10000)
            except PlaywrightTimeoutError as exc:
                raise RuntimeError(
                    "Product page loaded without a visible Add to Cart control."
                ) from exc
            disabled_attr = add_to_cart.get_attribute("disabled")
            disabled_value = (disabled_attr or "").strip().lower()
            aria_disabled = (add_to_cart.get_attribute("aria-disabled") or "").strip().lower()
            classes = add_to_cart.get_attribute("class") or ""
            is_disabled = (
                (disabled_attr is not None and disabled_value not in {"0", "false", "no"})
                or aria_disabled == "true"
                or "disabled" in classes.split()
            )
            return not is_disabled, title_locator.inner_text().strip(), page.url
        finally:
            browser.close()


def main() -> int:
    pincode = required("AMUL_PINCODE", "560103")
    product_urls = {
        "60": os.getenv("AMUL_PRODUCT_URL_60",
            "https://shop.amul.com/product/amul-chocolate-whey-protein-34-g-or-pack-of-60-sachets").strip(),
        "30": os.getenv("AMUL_PRODUCT_URL_30",
            "https://shop.amul.com/product/amul-chocolate-whey-protein-34-g-or-pack-of-30-sachets").strip(),
    }
    # Normalize localized links commonly used in existing VPS .env files.
    product_urls = {pack: re.sub(r"^https://shop\.amul\.com/en/product/",
                                 "https://shop.amul.com/product/", url)
                    for pack, url in product_urls.items()}
    token = required("TELEGRAM_BOT_TOKEN")
    chat_ids = telegram_chat_ids()

    legacy_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    previous = load_previous_state(legacy_chat_id)
    current: dict[str, tuple[bool, str, list[str], list[str]]] = {}
    delivery_failed = False
    for pack, url in product_urls.items():
        in_stock, title, final_url = check_site(pincode, url)
        print(f"{datetime.now().astimezone().isoformat()} pincode={pincode} pack={pack} in_stock={in_stock} product={title}", flush=True)
        old = previous.get(pack)
        changed = old is None or in_stock != old["in_stock"]
        notified = [] if changed else [chat_id for chat_id in old["notified_chat_ids"]
                                       if chat_id in chat_ids]
        pending = [] if changed else [chat_id for chat_id in old["pending_chat_ids"]
                                      if chat_id in chat_ids]
        # A newly added friend gets the current status on the next run; failed
        # recipients are retried alone so existing subscribers do not get repeats.
        recipients = chat_ids if changed else list(dict.fromkeys(
            pending + [chat_id for chat_id in chat_ids if chat_id not in notified]
        ))
        if recipients:
            status = "IN STOCK" if in_stock else "OUT OF STOCK"
            failed = send_telegram(
                token, recipients,
                f"{status} for {pincode} (pack of {pack}): {title}\n{final_url}",
            )
            notified.extend(chat_id for chat_id in recipients if chat_id not in failed)
            pending = failed
            delivery_failed = delivery_failed or bool(failed)
        current[pack] = (in_stock, title, list(dict.fromkeys(notified)), pending)
    save_state(current)
    return 1 if delivery_failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Stock check failed: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
