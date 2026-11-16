"""Amul stock alert: watches Amul product pages and sends an ntfy push
notification the moment a product comes back in stock.

Usage:
  python amul_stock_alert.py               # run forever (checks every N seconds)
  python amul_stock_alert.py --once        # check once, print status, exit
  python amul_stock_alert.py --minutes 5   # keep checking for 5 minutes, then exit (GitHub Actions)
  python amul_stock_alert.py --test-notify # send a test notification and exit

Settings live in config.json next to this file.
"""
import json
import os
import random
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"
STATE_FILE = HERE / "state.json"
LOG_FILE = HERE / "amul_stock_alert.log"
INDIA = timezone(timedelta(hours=5, minutes=30))  # "once a day" means an India calendar day
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass  # no console when run hidden
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    # keep the log small
    try:
        if LOG_FILE.stat().st_size > 2_000_000:
            lines = LOG_FILE.read_text(encoding="utf-8").splitlines()[-5000:]
            LOG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def load_config():
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    # On GitHub Actions the private values come from repository secrets, not the file.
    if os.environ.get("NTFY_TOPIC"):
        cfg["ntfy_topic"] = os.environ["NTFY_TOPIC"]
    if os.environ.get("PINCODE"):
        cfg["pincode"] = os.environ["PINCODE"]
    return cfg


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def notify(cfg, title, message, url=None, priority="urgent", tags="tada"):
    topic = cfg["ntfy_topic"]
    if not topic or topic.startswith("CHANGE-ME"):
        log("No ntfy topic set (NTFY_TOPIC secret or config.json); skipping notification")
        return
    req = urllib.request.Request(
        f"{cfg.get('ntfy_server', 'https://ntfy.sh').rstrip('/')}/{topic}",
        data=message.encode("utf-8"),
        method="POST",
    )
    # ntfy header values must be ASCII-safe
    req.add_header("Title", title.encode("ascii", "ignore").decode())
    req.add_header("Priority", priority)
    req.add_header("Tags", tags)
    if url:
        req.add_header("Click", url)
        req.add_header("Actions", f"view, Open product, {url}")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                log(f"ntfy sent ({r.status}): {title}")
                return
        except Exception as e:
            log(f"ntfy send failed (try {attempt + 1}): {e}")
            time.sleep(3)


def alias_of(url):
    return url.rstrip("/").split("/product/")[-1]


class AmulChecker:
    def __init__(self, pw, pincode):
        self.pincode = str(pincode)
        self.browser = pw.chromium.launch()
        self.ctx = self.browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
        self.page = self.ctx.new_page()
        self.pincode_set = False

    def close(self):
        try:
            self.browser.close()
        except Exception:
            pass

    def _set_pincode(self):
        box = self.page.get_by_placeholder("Enter Your Pincode")
        box.first.wait_for(state="visible", timeout=15000)
        box.first.click()
        box.first.fill("")
        box.first.press_sequentially(self.pincode, delay=120)
        item = self.page.locator(
            f".searchitem-name, li:has-text('{self.pincode}'), a:has-text('{self.pincode}')"
        ).first
        item.wait_for(state="visible", timeout=15000)
        with self.page.expect_response(lambda r: "setPreferences" in r.url, timeout=20000):
            item.click()
        self.page.wait_for_timeout(2000)
        self.pincode_set = True
        log(f"Delivery pincode set to {self.pincode}")

    def _load_product(self, url):
        """Load the product page and return the product record from the site's own API."""
        alias = alias_of(url)
        found = {}

        def on_response(r):
            if "ms.products" in r.url and "alias" in r.url and alias in r.url:
                try:
                    data = r.json().get("data") or []
                    if data:
                        found["product"] = data[0]
                    else:
                        found.setdefault("empty", True)
                except Exception:
                    pass

        self.page.on("response", on_response)
        try:
            self.page.goto(url, wait_until="networkidle", timeout=60000)
            self.page.wait_for_timeout(1500)
        finally:
            self.page.remove_listener("response", on_response)
        return found.get("product")

    def check(self, url):
        product = self._load_product(url)
        if product is None:
            # No data means the site has no delivery pincode for this session yet.
            self._set_pincode()
            product = self._load_product(url)
        if product is None:
            raise RuntimeError("could not read product data (site layout may have changed)")
        qty = product.get("inventory_quantity") or 0
        available = product.get("available") == 1
        return {
            "name": product.get("name") or alias_of(url),
            "in_stock": bool(available and qty > 0),
            "quantity": qty,
            "price": product.get("price"),
        }


def run(once=False, minutes=None):
    deadline = time.time() + minutes * 60 if minutes else None
    cfg = load_config()
    state = load_state()
    with sync_playwright() as pw:
        checker = None
        failures = 0
        successes = 0
        while True:
            cfg = load_config()  # pick up edits without restarting
            try:
                if checker is None:
                    checker = AmulChecker(pw, cfg["pincode"])
                for url in cfg["products"]:
                    s = checker.check(url)
                    prev = state.get(url, {}).get("in_stock")
                    last_alert = state.get(url, {}).get("last_alert_date")
                    today = datetime.now(INDIA).date().isoformat()
                    log(f"{'IN STOCK ' if s['in_stock'] else 'SOLD OUT '} qty={s['quantity']:<4} {s['name']}")
                    if s["in_stock"] and prev is False:
                        if last_alert == today:
                            log("Back in stock, but already alerted today for this item; staying quiet")
                        else:
                            notify(cfg, "Back in stock: " + s["name"],
                                   f"{s['name']} is back in stock on Amul "
                                   f"(qty {s['quantity']}, Rs {s['price']}). Order now!",
                                   url=url, priority="urgent", tags="rotating_light,shopping_cart")
                            last_alert = today
                    elif not s["in_stock"] and prev is True and cfg.get("notify_when_sold_out", True):
                        notify(cfg, "Sold out: " + s["name"],
                               f"{s['name']} just went out of stock. I'll alert you when it's back.",
                               url=url, priority="low", tags="hourglass")
                    state[url] = {"in_stock": s["in_stock"], "quantity": s["quantity"],
                                  "checked": datetime.now(INDIA).isoformat(timespec="seconds"),
                                  "last_alert_date": last_alert}
                    save_state(state)
                failures = 0
                successes += 1
            except Exception as e:
                failures += 1
                log(f"Check failed ({failures} in a row): {e}")
                if failures == 1:
                    log(traceback.format_exc().strip())
                if checker:
                    checker.close()
                checker = None
                if failures == 10:
                    notify(cfg, "Amul stock alert has a problem",
                           f"The last 10 checks failed. Latest error: {e}",
                           priority="default", tags="warning")
            if once:
                break
            wait = int(cfg.get("check_every_seconds", 60))
            if failures:
                wait = min(wait * (2 ** min(failures, 4)), 900)
            wait += random.uniform(0, 10)
            if deadline and time.time() + wait > deadline:
                break
            time.sleep(wait)
        if checker:
            checker.close()
    if successes == 0:
        sys.exit("No check succeeded in this run (site blocked or changed).")


if __name__ == "__main__":
    if "--test-notify" in sys.argv:
        notify(load_config(), "Amul stock alert is working",
               "Test notification: you'll get an alert like this when the whey protein is back in stock.",
               url=load_config()["products"][0], priority="high", tags="white_check_mark")
    else:
        mins = None
        if "--minutes" in sys.argv:
            mins = float(sys.argv[sys.argv.index("--minutes") + 1])
        run(once="--once" in sys.argv, minutes=mins)
