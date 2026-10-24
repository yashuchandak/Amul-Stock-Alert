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
HOME_URL = "https://shop.amul.com/en/"


def required(name: str, default: str = "") -> str:
    value = os.getenv(name, default).strip()
    if not value:
        raise RuntimeError(f"Missing {name}; set it in .env")
    return value


def send_telegram(token: str, chat_id: str, text: str) -> None:
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
        timeout=20,
    )
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram send failed: {body}")


def load_previous_state() -> dict[str, bool]:
    try:
        data = json.loads(STATE_FILE.read_text())
        # Keep the old single-product state readable during the upgrade.
        if isinstance(data.get("in_stock"), bool):
            return {"60": data["in_stock"]}
        products = data.get("products", {})
        return {str(pack): entry["in_stock"] for pack, entry in products.items()
                if isinstance(entry, dict) and isinstance(entry.get("in_stock"), bool)}
    except FileNotFoundError:
        return {}


def save_state(products: dict[str, tuple[bool, str]]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "products": {
            pack: {"in_stock": in_stock, "product": title}
            for pack, (in_stock, title) in products.items()
        },
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
    }, indent=2))
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
            try:
                sold_out = detail.locator(".alert.alert-danger", has_text=re.compile("sold out|out of stock", re.I))
                sold_out.wait_for(state="visible", timeout=4000)
                return False, title_locator.inner_text().strip(), page.url
            except PlaywrightTimeoutError:
                pass

            # On the live product page, Amul renders Add to Cart as an anchor
            # with disabled="true" and a `disabled` class when unavailable.
            add_to_cart = detail.locator("a.add-to-cart")
            add_to_cart.wait_for(state="attached", timeout=10000)
            disabled_attr = add_to_cart.get_attribute("disabled")
            classes = add_to_cart.get_attribute("class") or ""
            button_text = add_to_cart.inner_text().strip().lower()
            if disabled_attr is None and "disabled" not in classes.split() and "add to cart" in button_text:
                return True, title_locator.inner_text().strip(), page.url
            raise RuntimeError("Could not determine stock state from the product page")
        finally:
            browser.close()


def main() -> int:
    pincode = required("AMUL_PINCODE", "560103")
    product_urls = {
        "60": os.getenv("AMUL_PRODUCT_URL_60", os.getenv(
            "AMUL_PRODUCT_URL",
            "https://shop.amul.com/product/amul-chocolate-whey-protein-34-g-or-pack-of-60-sachets",
        )).strip(),
        "30": os.getenv("AMUL_PRODUCT_URL_30",
            "https://shop.amul.com/product/amul-chocolate-whey-protein-34-g-or-pack-of-30-sachets").strip(),
    }
    # Normalize localized links commonly used in existing VPS .env files.
    product_urls = {pack: re.sub(r"^https://shop\.amul\.com/en/product/",
                                 "https://shop.amul.com/product/", url)
                    for pack, url in product_urls.items()}
    token = required("TELEGRAM_BOT_TOKEN")
    chat_id = required("TELEGRAM_CHAT_ID")

    previous = load_previous_state()
    current: dict[str, tuple[bool, str]] = {}
    for pack, url in product_urls.items():
        in_stock, title, final_url = check_site(pincode, url)
        current[pack] = (in_stock, title)
        print(f"{datetime.now().astimezone().isoformat()} pincode={pincode} pack={pack} in_stock={in_stock} product={title}", flush=True)
        if pack not in previous or in_stock != previous[pack]:
            status = "IN STOCK" if in_stock else "OUT OF STOCK"
            send_telegram(token, chat_id, f"{status} for {pincode} (pack of {pack}): {title}\n{final_url}")
    save_state(current)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Stock check failed: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
