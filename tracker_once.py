"""Runs ONE scan of the Apple Canada iPhone Duo stock, then exits.
Designed for GitHub Actions. The ntfy topic comes from the NTFY_TOPIC secret,
so it is never written in this (public) file."""

import json
import os
import random
import time
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
STATE_FILE = Path("state.json")

DELAY_BETWEEN_REGIONS = (3, 6)  # random pause (seconds) between postal codes
MAX_RETRIES = 1                 # retries per region before skipping it
RETRY_WAIT = 20                 # seconds to wait before a retry

SKUS = {
    "MK244VC/A": "256GB - Star White",
    "MK254VC/A": "256GB - Night Sky",
    "MK264VC/A": "512GB - Star White",
    "MK274VC/A": "512GB - Night Sky",
    "MK284VC/A": "1TB - Star White",
    "MK294VC/A": "1TB - Night Sky",
}

REGIONAL_POSTAL_CODES = [
    "V6B 2W9",  # Vancouver, BC
    "T2P 2Y5",  # Calgary, AB
    "T5T 4J2",  # Edmonton, AB
    "R3G 3J7",  # Winnipeg, MB
    "M5H 2N2",  # Toronto / GTA / Ottawa, ON
    "H3B 2Y5",  # Montreal / Quebec City, QC
    "B3J 2K9",  # Halifax, NS
]

PRODUCT_URL = "https://www.apple.com/ca/shop/buy-iphone/iphone-duo?product=MK284VC/A"


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def notify(title, message):
    """Send a push notification to your iPhone through ntfy.sh."""
    print(f"NOTIFY: {title} | {message}")
    if not NTFY_TOPIC:
        print("  (No NTFY_TOPIC set, so no notification was sent.)")
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "high", "Click": PRODUCT_URL},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"  (Could not send notification: {e})")


def fetch_region(page, postal_code):
    """One request that checks ALL models for one postal code."""
    params = "&".join(f"parts.{i}={urllib.parse.quote(sku)}" for i, sku in enumerate(SKUS))
    url = (
        f"/ca/shop/fulfillment-messages?fae=true&pl=true&mts.0=regular"
        f"&{params}&location={urllib.parse.quote(postal_code)}"
    )
    return page.evaluate(
        """async (url) => {
            try {
                const r = await fetch(url, {
                    credentials: 'include',
                    headers: { 'Accept': 'application/json, text/javascript, */*; q=0.01' }
                });
                return { status: r.status, body: await r.text() };
            } catch (e) {
                return { status: 0, body: String(e) };
            }
        }""",
        url,
    )


def main():
    state = load_state()
    was_blocked = state.get("_blocked", False)
    restocks = []
    seen_stores, seen_pairs = set(), set()
    in_stock = 0
    failed = 0
    ok_regions = 0
    blocked = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--disable-dev-shm-usage"])
        context = browser.new_context(locale="en-CA")
        page = context.new_page()

        try:
            page.goto(PRODUCT_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
        except Exception as e:
            print(f"Could not load Apple page: {e}")
            browser.close()
            save_state(state)
            return

        for postal_code in REGIONAL_POSTAL_CODES:
            result = {"status": 0}
            for attempt in range(MAX_RETRIES + 1):
                result = fetch_region(page, postal_code)
                if result.get("status") == 200:
                    break
                print(f"  {postal_code}: HTTP {result.get('status')} (attempt {attempt + 1})")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_WAIT)

            if result.get("status") != 200:
                failed += 1
                print(f"  Skipping {postal_code}. Response: {str(result.get('body'))[:120]}")
                if failed >= 3:
                    blocked = True
                    break
                continue

            try:
                data = json.loads(result.get("body", "{}"))
                stores = (
                    data.get("body", {}).get("content", {})
                    .get("pickupMessage", {}).get("stores", [])
                )
            except Exception as e:
                print(f"  Parse error for {postal_code}: {e}")
                continue

            ok_regions += 1
            for store in stores:
                store_num = store.get("storeNumber")
                store_name = store.get("storeName")
                province = store.get("state")
                seen_stores.add(store_num)

                for sku, model_name in SKUS.items():
                    if (store_num, sku) in seen_pairs:
                        continue
                    seen_pairs.add((store_num, sku))

                    details = (store.get("partsAvailability", {}) or {}).get(sku, {})
                    is_avail = 1 if details.get("pickupDisplay") == "available" else 0
                    key = f"{store_num}|{sku}"

                    if is_avail:
                        in_stock += 1
                        print(f"  [IN STOCK] {store_name} ({province}) -> {model_name}")
                        if not state.get(key, 0):
                            restocks.append(f"{store_name} ({province}): {model_name}")
                    state[key] = is_avail

            time.sleep(random.uniform(*DELAY_BETWEEN_REGIONS))

        browser.close()

    print(f"Scan done: {len(seen_stores)} stores checked, {in_stock} units available, {failed} regions failed.")

    if restocks:
        notify("iPhone Duo in stock!", "\n".join(restocks))

    # Only alert about blocking once, and again when it recovers (no spam).
    if blocked and not was_blocked:
        notify("Tracker blocked", "Apple is rejecting requests from GitHub. Will keep trying quietly.")
    elif not blocked and was_blocked and ok_regions > 0:
        notify("Tracker working again", "Apple is answering requests again.")
    state["_blocked"] = blocked

    save_state(state)


if __name__ == "__main__":
    try:
        main()
    finally:
        if not STATE_FILE.exists():
            save_state({})
