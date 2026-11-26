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
    ENVIZOM_EMAIL       if set (with ENVIZOM_PASSWORD), the monitor also logs in and counts
    ENVIZOM_PASSWORD    the site as DOWN if login does not succeed. Use a dedicated test account.
    LOGIN_TIMEOUT       seconds to wait for login to complete, default 30
    LOGIN_USER_SELECTOR / LOGIN_PASS_SELECTOR / LOGIN_SUBMIT_SELECTOR / LOGIN_SUCCESS_SELECTOR /
    LOGIN_CHECKBOX_SELECTOR (the login ticks the "I accept the Terms and Conditions" box automatically)
                        optional CSS selectors if auto-detection of the login form fails
    REMIND_EVERY        while still down, re-alert every N seconds, default 1800 (0 = never)
    GCHAT_STATUS_WEBHOOK_URL  optional second Google Chat webhook that gets a daily
                        "Still running fine" message
    HEARTBEAT_TIME      local time (HH:MM, 24h) to send the daily message, default 09:00
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
        stage = {"now": "loading the page"}
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

            # ---- login check (only if credentials are configured)
            if os.environ.get("ENVIZOM_EMAIL") and os.environ.get("ENVIZOM_PASSWORD"):
                stage["now"] = "logging in"
                ok, reason = do_login(page, details)
                if not ok:
                    return _fail(page, reason, details)

            details["title"] = page.title()
            return True, "OK", details, None

        except PWTimeout as e:
            if stage["now"] == "loading the page":
                return _fail(page, f"Page did not load within {timeout_s}s (timeout)", details)
            details["login_debug"] = describe_form(page)
            return _fail(page, f"Timed out while {stage['now']}: {str(e).splitlines()[0][:150]}", details)
        except Exception as e:  # DNS failure, connection refused, SSL error, ...
            msg = str(e).split("\n")[0]
            return _fail(page, f"Could not open site: {msg[:200]}", details)
        finally:
            browser.close()


# Fallback selectors, tried in order. Override with LOGIN_*_SELECTOR env vars if needed.
USER_SELECTORS = [
    "input[type=email]", "input[formcontrolname*=email i]", "input[name*=email i]",
    "input[id*=email i]", "input[placeholder*=email i]", "input[formcontrolname*=user i]",
    "input[name*=user i]", "input[id*=user i]", "input[placeholder*=user i]",
    "input[type=text]",
]
SUBMIT_SELECTORS = [
    "button[type=submit]", "input[type=submit]",
    "button:has-text('Log in')", "button:has-text('Login')", "button:has-text('Sign in')",
    "button:has-text('Sign In')", "button:has-text('Continue')",
]
LOGIN_ERROR_WORDS = [
    "invalid", "incorrect", "wrong password", "not found", "failed", "unauthorized",
    "try again", "locked", "doesn't exist", "does not exist",
]


def _type_into(el, value):
    """Type a value into a field, trying progressively stronger methods. Returns error text or None."""
    last = None
    for attempt in ("fill", "force", "js"):
        try:
            if attempt == "fill":
                el.fill(value, timeout=8000)
            elif attempt == "force":
                el.fill(value, timeout=8000, force=True)
            else:
                el.evaluate("""(e, v) => { e.focus(); e.value = v;
                    e.dispatchEvent(new Event('input', {bubbles: true}));
                    e.dispatchEvent(new Event('change', {bubbles: true}));
                    e.dispatchEvent(new Event('blur', {bubbles: true})); }""", value)
            return None
        except Exception as e:
            last = str(e).splitlines()[0][:100]
    return last


def _tick_checkboxes(page):
    """Tick every unticked checkbox on the login form (e.g. Terms and Conditions).
    Handles Angular Material checkboxes whose real <input> is visually hidden. Returns error or None."""
    sel = os.environ.get("LOGIN_CHECKBOX_SELECTOR") or "input[type=checkbox]"
    boxes = page.locator(sel)
    for i in range(min(boxes.count(), 5)):
        box = boxes.nth(i)
        try:
            if box.is_checked():
                continue
        except Exception:
            continue
        done = False
        for how in ("check", "force", "label", "js"):
            try:
                if how == "check":
                    box.check(timeout=5000)
                elif how == "force":
                    box.check(timeout=5000, force=True)
                elif how == "label":
                    page.get_by_text("I accept", exact=False).first.click(timeout=5000)
                else:
                    box.evaluate("e => e.click()")
                page.wait_for_timeout(300)
                if box.is_checked():
                    done = True
                    break
            except Exception:
                pass
        if not done:
            return f"checkbox #{i + 1} would not tick"
    return None


def describe_form(page):
    """Summary of inputs/buttons on the page (never their values) to help fix selectors."""
    try:
        return page.evaluate("""() => {
          const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
          const ins = [...document.querySelectorAll('input')].slice(0, 10).map(i =>
            `input type=${i.type} name=${i.name||'-'} id=${i.id||'-'} formcontrolname=${i.getAttribute('formcontrolname')||'-'} placeholder="${(i.placeholder||'').slice(0,30)}" checked=${i.checked} visible=${vis(i)}`);
          const bts = [...document.querySelectorAll('button, [type=submit], [role=button]')].slice(0, 10).map(b =>
            `button "${(b.innerText||b.value||'').trim().slice(0,30)}" type=${b.type||'-'} disabled=${!!b.disabled} visible=${vis(b)}`);
          return `URL: ${location.href}\\n` + ins.concat(bts).join('\\n');
        }""")
    except Exception as e:
        return f"(could not read form: {e})"


def _first_visible(page, selectors):
    for sel in selectors:
        loc = page.locator(sel)
        for i in range(min(loc.count(), 5)):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    return el
            except Exception:
                pass
    return None


def do_login(page, details):
    """Fill in the login form and confirm we get past it. Returns (ok, reason)."""
    email = os.environ["ENVIZOM_EMAIL"]
    password = os.environ["ENVIZOM_PASSWORD"]
    login_timeout = int(os.environ.get("LOGIN_TIMEOUT", "30"))
    t0 = time.time()

    pw_sel = os.environ.get("LOGIN_PASS_SELECTOR") or "input[type=password]"
    try:
        page.wait_for_selector(pw_sel, state="visible", timeout=20000)
    except PWTimeout:
        details["login_debug"] = describe_form(page)
        return False, "Login page did not show a password field"
    pw = page.locator(pw_sel).first

    user_sel = os.environ.get("LOGIN_USER_SELECTOR")
    user = page.locator(user_sel).first if user_sel else _first_visible(page, USER_SELECTORS)
    if user is None:
        details["login_debug"] = describe_form(page)
        return False, "Login page: email/username field not found"

    login_url = page.url
    err = _type_into(user, email)
    if err:
        details["login_debug"] = describe_form(page)
        return False, f"Login: could not type into the email field ({err})"
    err = _type_into(pw, password)
    if err:
        details["login_debug"] = describe_form(page)
        return False, f"Login: could not type into the password field ({err})"

    # tick "I accept the Terms and Conditions" (or any other required, unticked checkbox)
    err = _tick_checkboxes(page)
    if err:
        details["login_debug"] = describe_form(page)
        return False, f"Login: could not tick the Terms checkbox ({err})"

    submit_sel = os.environ.get("LOGIN_SUBMIT_SELECTOR")
    btn = page.locator(submit_sel).first if submit_sel else _first_visible(page, SUBMIT_SELECTORS)
    if btn is not None:
        # wait (up to 10 s) for the button to become enabled after the form is valid
        for _ in range(20):
            try:
                if btn.is_enabled():
                    break
            except Exception:
                break
            page.wait_for_timeout(500)
        else:
            details["login_debug"] = describe_form(page)
            return False, "Login: LOG IN button stayed disabled after filling the form"
    clicked = False
    if btn is not None:
        for kw in ({}, {"force": True}):
            try:
                btn.click(timeout=8000, **kw)
                clicked = True
                break
            except Exception:
                pass  # e.g. button covered/disabled -> try next, then Enter
    if not clicked:
        try:
            pw.press("Enter", timeout=5000)
        except Exception:
            pass

    success_sel = os.environ.get("LOGIN_SUCCESS_SELECTOR")
    deadline = time.time() + login_timeout
    while time.time() < deadline:
        page.wait_for_timeout(1000)
        try:
            if success_sel:
                if page.locator(success_sel).first.is_visible():
                    break
            else:
                pw_gone = page.locator(pw_sel).count() == 0 or not page.locator(pw_sel).first.is_visible()
                if pw_gone and page.url != login_url:
                    break
            # an error message appeared on the login page?
            if page.locator(pw_sel).count() and page.locator(pw_sel).first.is_visible():
                txt = page.inner_text("body").lower()
                hit = next((w for w in LOGIN_ERROR_WORDS if w in txt), None)
                if hit and time.time() - t0 > 2:
                    snippet = next((l.strip() for l in page.inner_text("body").splitlines()
                                    if hit in l.lower()), hit)
                    return False, f'Login failed - page says: "{snippet[:120]}"'
        except Exception:
            pass  # page navigating; keep waiting
    else:
        details["login_debug"] = describe_form(page)
        return False, f"Login did not complete within {login_timeout}s (still on login page)"

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass
    details["login_time_s"] = round(time.time() - t0, 1)
    details["after_login_url"] = page.url
    return True, "OK"


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
    return "\n".join([
        "*Envizom not Working*",
        f"*Problem:* {reason}",
        f"*Detected:* {now()}",
    ])


def up_message(url, down_since, details):
    if down_since:
        mins = int((time.time() - down_since) // 60)
        after = f"After {mins} Minute{'s' if mins != 1 else ''}" if mins >= 1 else "After less than 1 Minute"
    else:
        after = ""
    title = f"*Envizom is Working {after}*" if after else "*Envizom is Working*"
    lines = [title, f"*Recovered:* {now()}"]
    if "load_time_s" in details:
        lines.append(f"*Load Time:* {details['load_time_s']}s")
    return "\n".join(lines)


# ---------------------------------------------------------------- state
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"status": "up", "down_since": None, "last_alert": 0}


def save_state(s):
    STATE_FILE.write_text(json.dumps(s))


# ---------------------------------------------------------------- main logic
def maybe_send_heartbeat(conf, state, ok, details):
    """Once a day (at/after HEARTBEAT_TIME) send a 'Still running fine' message to the status chat."""
    hook = conf.get("status_webhook")
    if not hook:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("last_heartbeat") == today:
        return
    if datetime.now().strftime("%H:%M") < conf["heartbeat_time"]:
        return
    if ok:
        login = " Login working." if "login_time_s" in details else ""
        msg = (f"*Still running fine* ✅\n"
               f"Envizom is working.{login}\n"
               f"*Checked:* {now()}")
    else:
        msg = (f"*Monitor running* - but Envizom is currently *not working* ⚠️\n"
               f"*Checked:* {now()}")
    if send_gchat(hook, msg):
        state["last_heartbeat"] = today


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
        login = f", login OK in {details['login_time_s']}s" if "login_time_s" in details else ""
        log(f"UP   - {url} - {details.get('load_time_s')}s, HTTP {details.get('http_status')}{login}")
        if state["status"] == "down":
            send_gchat(conf["webhook"], up_message(url, state.get("down_since"), details))
        state.update(status="up", down_since=None, last_alert=0)
    else:
        log(f"DOWN - {url} - {reason}" + (f" (screenshot: {shot})" if shot else ""))
        if details.get("login_debug"):
            log("Login page elements found:\n" + details["login_debug"])
        first_failure = state["status"] != "down"
        remind_due = conf["remind"] > 0 and time.time() - state.get("last_alert", 0) >= conf["remind"]
        if first_failure or remind_due:
            msg = down_message(url, reason, details, attempts)
            if not first_failure:
                msg = msg.replace("Envizom not Working", "Envizom still not Working")
            if send_gchat(conf["webhook"], msg):
                state["last_alert"] = time.time()
        if first_failure:
            state["down_since"] = time.time()
        state["status"] = "down"
    maybe_send_heartbeat(conf, state, ok, details)
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
        "status_webhook": cfg("GCHAT_STATUS_WEBHOOK_URL") or None,
        "heartbeat_time": cfg("HEARTBEAT_TIME", "10:30"),
    }
    if not conf["webhook"]:
        sys.exit("GCHAT_WEBHOOK_URL is not set. Put it in a .env file or environment variable.")

    if args.test_alert:
        ok = send_gchat(conf["webhook"], f"✅ Envizom monitor test message (alerts chat) at {now()}")
        if conf["status_webhook"]:
            ok = send_gchat(conf["status_webhook"], f"✅ Envizom monitor test message (status chat) at {now()}") and ok
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
