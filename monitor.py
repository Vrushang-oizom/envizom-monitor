#!/usr/bin/env python3
"""
Envizom uptime monitor - Stage 1
--------------------------------
Opens envizom.oizom.com in a real headless browser (like reloading it in Chrome),
checks the page actually loads and renders, and posts an alert to Google Chat
the moment it is not working. Sends a "recovered" message when it comes back.

Run modes:
    python monitor.py            # loop forever, check every CHECK_INTERVAL seconds
    python monitor.py --once     # single check (for cron / GitHub Actions / Task Scheduler)
    python monitor.py --test-alert   # just send a test message to Google Chat

Configuration (environment variables or a .env file next to this script):
    GCHAT_WEBHOOK_URL   (required) Google Chat incoming-webhook URL
    ENVIZOM_URL         default https://envizom.oizom.com/
    CHECK_INTERVAL      seconds between checks in loop mode, default 60
    PAGE_TIMEOUT        seconds to wait for the page to load, default 45
    RETRY_DELAY         seconds to wait before re-checking a failure, default 15
                        (one quick re-check avoids false alarms from a network blip;
                        set 0 to alert on the very first failure)
    EXPECT_SELECTOR     optional CSS selector that must be visible, e.g. "input[type=password]"
    REMIND_EVERY        while still down, re-alert every N seconds, default 1800 (0 = never)
"""

import argparse
import json
import os
import socket
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / ".monitor_state.json"
SHOTS_DIR = HERE / "screenshots"

# Text that means the server/gateway is broken even if it returned a page
ERROR_MARKERS = [
    "502 bad gateway", "503 service", "504 gateway", "service unavailable",
    "internal server error", "bad gateway", "this site can’t be reached",
    "nginx error", "application error", "cannot get /",
]


# ---------------------------------------------------------------- config
def load_dotenv():
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def cfg(name, default=None, cast=str):
    val = os.environ.get(name, default)
    return cast(val) if val is not None else None


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


# ---------------------------------------------------------------- the check
def check_site(url, timeout_s, expect_selector=None):
    """Load the site in headless Chromium. Returns (ok, reason, details, screenshot_path)."""
    details = {}
    shot = None
    with sync_playwright() as p:
        channel = os.environ.get("BROWSER_CHANNEL") or None  # e.g. "chrome" = use installed Google Chrome
        browser = p.chromium.launch(headless=True, channel=channel)
        page = browser.new_page(ignore_https_errors=False)
        js_errors = []
        page.on("pageerror", lambda e: js_errors.append(str(e)))
        try:
            t0 = time.time()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            # give the single-page app time to render
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_s * 1000)
            except PWTimeout:
                pass  # some apps keep sockets open; not fatal on its own
            details["load_time_s"] = round(time.time() - t0, 1)

            if resp is None:
                return _fail(page, "No response received from server", details)
            details["http_status"] = resp.status
            if resp.status >= 400:
                return _fail(page, f"Server returned HTTP {resp.status}", details)

            if expect_selector:
                try:
                    page.wait_for_selector(expect_selector, state="visible", timeout=15000)
                except PWTimeout:
                    return _fail(page, f"Expected element not found: {expect_selector}", details)

            body_text = (page.inner_text("body") or "").strip()
            lower = body_text.lower()
            for marker in ERROR_MARKERS:
                if marker in lower:
                    return _fail(page, f'Error page shown: "{marker}"', details)

            # blank-page check: no text and no form fields / images = app didn't render
            interactive = page.locator("input, button, img, canvas, svg").count()
            details["visible_text_chars"] = len(body_text)
            if len(body_text) < 5 and interactive == 0:
                if js_errors:
                    details["js_error"] = js_errors[0][:200]
                return _fail(page, "Page loaded blank (app did not render)", details)

            details["title"] = page.title()
            return True, "OK", details, None

        except PWTimeout:
            return _fail(page, f"Page did not load within {timeout_s}s (timeout)", details)
        except Exception as e:  # DNS failure, connection refused, SSL error, ...
            msg = str(e).split("\n")[0]
            return _fail(page, f"Could not open site: {msg[:200]}", details)
        finally:
            browser.close()


def _fail(page, reason, details):
    shot = None
    try:
        SHOTS_DIR.mkdir(exist_ok=True)
        shot = SHOTS_DIR / f"down_{datetime.now():%Y%m%d_%H%M%S}.png"
        page.screenshot(path=str(shot), full_page=False, timeout=10000)
    except Exception:
        shot = None
    return False, reason, details, shot


# ---------------------------------------------------------------- google chat
def send_gchat(webhook, text):
    data = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        webhook, data=data, headers={"Content-Type": "application/json; charset=UTF-8"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            log(f"Google Chat message sent (HTTP {r.status})")
            return True
    except Exception as e:
        log(f"!! Failed to send Google Chat message: {e}")
        return False


def down_message(url, reason, details, attempts):
    lines = [
        "🔴 *ENVIZOM IS DOWN*",
        f"*URL:* {url}",
        f"*Problem:* {reason}",
        f"*Detected:* {now()} (failed {attempts} check{'s' if attempts > 1 else ''} in a row)",
    ]
    if "http_status" in details:
        lines.append(f"*HTTP status:* {details['http_status']}")
    if "js_error" in details:
        lines.append(f"*JS error:* `{details['js_error']}`")
    lines.append(f"*Checked from:* {socket.gethostname()}")
    return "\n".join(lines)


def up_message(url, down_since, details):
    dur = ""
    if down_since:
        mins = int((time.time() - down_since) // 60)
        dur = f" after ~{mins} min" if mins else " (under a minute)"
    return (
        f"🟢 *ENVIZOM IS BACK UP*{dur}\n*URL:* {url}\n*Recovered:* {now()}"
        + (f"\n*Load time:* {details['load_time_s']}s" if "load_time_s" in details else "")
    )


# ---------------------------------------------------------------- state
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"status": "up", "down_since": None, "last_alert": 0}


def save_state(s):
    STATE_FILE.write_text(json.dumps(s))


# ---------------------------------------------------------------- main logic
def run_check(conf, state):
    url = conf["url"]
    ok, reason, details, shot = check_site(url, conf["timeout"], conf["selector"])
    attempts = 1
    if not ok and conf["retry_delay"] > 0:
        log(f"First check failed: {reason}. Re-checking in {conf['retry_delay']}s...")
        time.sleep(conf["retry_delay"])
        ok, reason, details, shot = check_site(url, conf["timeout"], conf["selector"])
        attempts = 2

    if ok:
        log(f"UP   - {url} - {details.get('load_time_s')}s, HTTP {details.get('http_status')}")
        if state["status"] == "down":
            send_gchat(conf["webhook"], up_message(url, state.get("down_since"), details))
        state.update(status="up", down_since=None, last_alert=0)
    else:
        log(f"DOWN - {url} - {reason}" + (f" (screenshot: {shot})" if shot else ""))
        first_failure = state["status"] != "down"
        remind_due = conf["remind"] > 0 and time.time() - state.get("last_alert", 0) >= conf["remind"]
        if first_failure or remind_due:
            msg = down_message(url, reason, details, attempts)
            if not first_failure:
                msg = msg.replace("ENVIZOM IS DOWN", "ENVIZOM STILL DOWN")
            if send_gchat(conf["webhook"], msg):
                state["last_alert"] = time.time()
        if first_failure:
            state["down_since"] = time.time()
        state["status"] = "down"
    save_state(state)
    return ok


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description="Envizom uptime monitor")
    ap.add_argument("--once", action="store_true", help="run a single check and exit")
    ap.add_argument("--test-alert", action="store_true", help="send a test message to Google Chat")
    args = ap.parse_args()

    conf = {
        "url": cfg("ENVIZOM_URL", "https://envizom.oizom.com/"),
        "webhook": cfg("GCHAT_WEBHOOK_URL"),
        "interval": cfg("CHECK_INTERVAL", "60", int),
        "timeout": cfg("PAGE_TIMEOUT", "45", int),
        "retry_delay": cfg("RETRY_DELAY", "15", int),
        "selector": cfg("EXPECT_SELECTOR") or None,
        "remind": cfg("REMIND_EVERY", "1800", int),
    }
    if not conf["webhook"]:
        sys.exit("GCHAT_WEBHOOK_URL is not set. Put it in a .env file or environment variable.")

    if args.test_alert:
        ok = send_gchat(conf["webhook"], f"✅ Envizom monitor test message from {socket.gethostname()} at {now()}")
        sys.exit(0 if ok else 1)

    state = load_state()
    if args.once:
        sys.exit(0 if run_check(conf, state) else 2)

    log(f"Monitoring {conf['url']} every {conf['interval']}s. Ctrl+C to stop.")
    while True:
        try:
            run_check(conf, state)
        except Exception as e:
            log(f"Monitor error (will keep running): {e}")
        time.sleep(conf["interval"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped.")
