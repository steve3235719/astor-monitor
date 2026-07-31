#!/usr/bin/env python3
"""
VCSO Active Calls Monitor
=========================
Watches https://www.vcso.us/ActiveCalls/ and alerts you when a NEW call
appears for one of your watched locations (default: Astor, Osteen).

It remembers which call numbers it has already seen (in seen_calls.json),
so you only get alerted once per call and it survives restarts.

Alert channels (any combination):
  - Console/log  (always on)
  - Email        (works while away; use a carrier email->SMS gateway to text your phone)
  - Desktop notification (macOS/Linux/Windows, best-effort)

Quick start:
  pip install requests beautifulsoup4
  python vcso_monitor.py --test          # send a test alert and exit
  python vcso_monitor.py                  # run forever, poll every 60s
  python vcso_monitor.py --once           # check once and exit (good for cron)

Configure alerts by editing the CONFIG block below, or via a config.json
file next to this script (config.json values override the defaults here).
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import subprocess
import sys
import time
import platform
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage

import requests
from bs4 import BeautifulSoup

# On Windows, Python often can't complete a site's certificate chain
# ("unable to get local issuer certificate"). Routing verification through the
# operating system's own trust store fixes that cleanly, when available.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

# ---------------------------------------------------------------------------
# CONFIG  (edit here, or create a config.json next to this file to override)
# ---------------------------------------------------------------------------
CONFIG = {
    # Locations to watch. Case-insensitive substring match against the
    # "Location" column. "astor" also catches "Astor Park".
    "watch_locations": ["Astor", "Osteen"],

    # How often to poll, in seconds. The page itself refreshes every 60s.
    "poll_seconds": 60,

    # On the very first run, seed the list of currently-active calls WITHOUT
    # alerting, so you aren't flooded by everything already on the board.
    # Set to False if you want to be alerted about currently-active matches too.
    "seed_silently_on_first_run": True,

    # --- Email alerts (recommended; reaches your phone) ---
    "email_enabled": False,
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 465,                    # 465 = SSL
    "smtp_user": "youraddress@gmail.com",
    "smtp_password": "your-app-password",  # Gmail: create an App Password
    "email_from": "youraddress@gmail.com",
    # Send to a normal email, and/or a carrier email-to-SMS gateway to text you:
    #   Verizon:  5551234567@vtext.com
    #   AT&T:     5551234567@txt.att.net
    #   T-Mobile: 5551234567@tmomail.net
    #   Sprint:   5551234567@messaging.sprintpcs.com
    "email_to": ["youraddress@gmail.com"],

    # --- Push to YOUR "Astor / Osteen Alerts" iPhone app (via Expo) ---
    # These tokens come from the app's "Push alerts" box. Add more phones by
    # listing more tokens. This is what makes your own app buzz.
    "expo_enabled": True,
    "expo_tokens": ["ExponentPushToken[fhYwRcIB_TiKFVc3ftuECc]"],

    # --- iPhone/Android push via ntfy (the earlier method; still works) ---
    # Download the free "ntfy" app from the App Store, then subscribe to the
    # exact topic name below. Anyone who knows the topic can see the pushes,
    # so this is a long random name -- keep it private. Change it if you like,
    # then subscribe to the new name in the app.
    "ntfy_enabled": False,
    "ntfy_topic": "astor-osteen-7f75b85b",
    "ntfy_server": "https://ntfy.sh",

    # --- Desktop notification (best-effort, only when at the machine) ---
    "desktop_notification": True,

    # File used to remember which calls we've already alerted on.
    "state_file": "seen_calls.json",

    # Drop remembered calls older than this many days (keeps the file small).
    "forget_after_days": 7,

    # Be a polite scraper.
    "user_agent": "Mozilla/5.0 (compatible; AstorCallMonitor/1.0; personal use)",
    "request_timeout": 20,
}

URL = "https://www.vcso.us/ActiveCalls/"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
def load_config() -> dict:
    cfg = dict(CONFIG)
    path = os.path.join(SCRIPT_DIR, "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
            log(f"Loaded overrides from {path}")
        except Exception as e:
            log(f"WARNING: could not read config.json ({e}); using built-in CONFIG")
    return cfg


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Fetch + parse
# ---------------------------------------------------------------------------
def fetch_calls(cfg: dict) -> list[dict]:
    """Return a list of call dicts scraped from the active-calls table."""
    headers = {"User-Agent": cfg["user_agent"]}
    try:
        resp = requests.get(URL, headers=headers, timeout=cfg["request_timeout"])
        resp.raise_for_status()
        return parse_calls(resp.text)
    except requests.exceptions.SSLError:
        # Some Windows/network setups can't verify the site's certificate chain.
        # This is a read-only, public page (no login, nothing sensitive), so we
        # fall back to an unverified connection rather than failing outright.
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
        if not getattr(fetch_calls, "_warned_ssl", False):
            log("Note: could not verify the site's security certificate; using an "
                "unverified connection (fine for this public page).")
            fetch_calls._warned_ssl = True
        resp = requests.get(URL, headers=headers,
                            timeout=cfg["request_timeout"], verify=False)
        resp.raise_for_status()
        return parse_calls(resp.text)


def parse_calls(html: str) -> list[dict]:
    """
    Parse the VCSO active-calls table. We find the table whose header row
    contains 'Call Number' and 'Location', then read rows positionally:
        Call Number | Description | Priority | Location | Entry Time | Zone
    This is deliberately tolerant of markup changes (no reliance on ids/classes).
    """
    soup = BeautifulSoup(html, "html.parser")
    fields = ["call_number", "description", "priority", "location", "entry_time", "zone"]

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        header_idx = None
        for i, tr in enumerate(rows):
            cells = [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
            joined = " ".join(cells).lower()
            if "call number" in joined and "location" in joined:
                header_idx = i
                break
        if header_idx is None:
            continue

        calls = []
        for tr in rows[header_idx + 1:]:
            cells = [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
            if len(cells) < 4:            # skip spacer/empty rows
                continue
            cells = (cells + [""] * len(fields))[: len(fields)]
            row = dict(zip(fields, cells))
            if row["call_number"]:
                calls.append(row)
        return calls

    return []   # table not found (page layout may have changed)


def matches_watch(call: dict, watch_locations: list[str]) -> bool:
    loc = call.get("location", "").lower()
    return any(w.lower().strip() in loc for w in watch_locations)


# ---------------------------------------------------------------------------
# State (which calls we've already alerted on)
# ---------------------------------------------------------------------------
def load_state(cfg: dict) -> dict:
    path = os.path.join(SCRIPT_DIR, cfg["state_file"])
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"seen": {}}   # call_number -> ISO timestamp first seen


def save_state(cfg: dict, state: dict) -> None:
    # prune old entries
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg["forget_after_days"])
    state["seen"] = {
        k: v for k, v in state["seen"].items()
        if _parse_iso(v) is None or _parse_iso(v) >= cutoff
    }
    path = os.path.join(SCRIPT_DIR, cfg["state_file"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def _parse_iso(s: str):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
def format_alert(new_calls: list[dict]) -> tuple[str, str]:
    n = len(new_calls)
    subject = f"VCSO alert: {n} new call{'s' if n != 1 else ''} " \
              f"({', '.join(sorted({c['location'] for c in new_calls}))})"
    lines = []
    for c in new_calls:
        lines.append(
            f"{c['location']} — {c['description']} (P{c['priority']})  "
            f"[{c['entry_time']}, zone {c['zone']}, {c['call_number']}]"
        )
    body = "New Volusia SO active call(s):\n\n" + "\n".join(lines) + f"\n\n{URL}"
    return subject, body


def send_email(cfg: dict, subject: str, body: str) -> None:
    if not cfg.get("email_enabled"):
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["email_from"]
    msg["To"] = ", ".join(cfg["email_to"])
    msg.set_content(body)
    ctx = ssl.create_default_context()
    port = int(cfg["smtp_port"])
    try:
        if port == 465:
            with smtplib.SMTP_SSL(cfg["smtp_host"], port, context=ctx, timeout=30) as s:
                s.login(cfg["smtp_user"], cfg["smtp_password"])
                s.send_message(msg)
        else:  # e.g. 587 STARTTLS
            with smtplib.SMTP(cfg["smtp_host"], port, timeout=30) as s:
                s.starttls(context=ctx)
                s.login(cfg["smtp_user"], cfg["smtp_password"])
                s.send_message(msg)
        log(f"Email sent to {', '.join(cfg['email_to'])}")
    except Exception as e:
        log(f"ERROR sending email: {e}")


def send_ntfy(cfg: dict, subject: str, body: str) -> None:
    """Push an alert to the ntfy app on your phone."""
    if not cfg.get("ntfy_enabled"):
        return
    topic = str(cfg.get("ntfy_topic", "")).strip()
    if not topic:
        log("ntfy enabled but ntfy_topic is empty; skipping push")
        return
    server = str(cfg.get("ntfy_server", "https://ntfy.sh")).rstrip("/")
    try:
        r = requests.post(
            f"{server}/{topic}",
            data=body.encode("utf-8"),
            headers={
                "Title": subject.encode("ascii", "replace").decode("ascii"),
                "Priority": "high",
                "Tags": "rotating_light",
                "Click": URL,
            },
            timeout=cfg.get("request_timeout", 20),
        )
        r.raise_for_status()
        log(f"Push sent via ntfy to topic '{topic}'")
    except Exception as e:
        log(f"ERROR sending ntfy push: {e}")


def send_expo_push(cfg: dict, subject: str, body: str) -> None:
    """Push an alert to your own 'Astor / Osteen Alerts' iPhone app via Expo."""
    if not cfg.get("expo_enabled"):
        return
    tokens = cfg.get("expo_tokens") or []
    if isinstance(tokens, str):
        tokens = [tokens]
    tokens = [t for t in tokens if t and "ExponentPushToken" in t]
    if not tokens:
        log("expo enabled but no valid expo_tokens; skipping app push")
        return
    messages = [{
        "to": t,
        "title": subject,
        "body": body,
        "sound": "default",
        "priority": "high",
    } for t in tokens]
    try:
        r = requests.post(
            "https://exp.host/--/api/v2/push/send",
            json=messages,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=cfg.get("request_timeout", 20),
        )
        r.raise_for_status()
        # Expo returns a per-message status; log any errors it reports.
        data = r.json().get("data", [])
        errors = [d for d in data if isinstance(d, dict) and d.get("status") == "error"]
        if errors:
            log(f"Expo push accepted but reported {len(errors)} error(s): "
                f"{errors[0].get('message', '')}")
        else:
            log(f"Push sent to your app ({len(tokens)} device(s))")
    except Exception as e:
        log(f"ERROR sending app push: {e}")


def send_desktop(cfg: dict, subject: str, body: str) -> None:
    if not cfg.get("desktop_notification"):
        return
    try:
        system = platform.system()
        first_line = body.split("\n\n")[1] if "\n\n" in body else body
        if system == "Darwin":
            script = f'display notification {json.dumps(first_line)} with title {json.dumps(subject)} sound name "Ping"'
            subprocess.run(["osascript", "-e", script], check=False)
        elif system == "Linux":
            subprocess.run(["notify-send", subject, first_line], check=False)
        elif system == "Windows":
            _windows_toast(subject, first_line)
    except Exception as e:
        log(f"(desktop notification unavailable: {e})")


def _windows_toast(title: str, text: str) -> None:
    """Show a non-blocking notification balloon in the Windows tray/corner.

    Runs PowerShell from a temp UTF-8 script file so special characters (like
    the em dash) can't break command-line quoting, and launches it detached so
    the monitor loop keeps running.
    """
    import tempfile

    def esc(s: str) -> str:            # PowerShell single-quoted string escaping
        return str(s).replace("'", "''")

    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
        "$n.Visible = $true;"
        f"$n.BalloonTipTitle = '{esc(title)}';"
        f"$n.BalloonTipText = '{esc(text)}';"
        "$n.ShowBalloonTip(15000);"
        "Start-Sleep -Seconds 10;"
        "$n.Dispose();"
    )
    tmp = os.path.join(tempfile.gettempdir(), "vcso_toast.ps1")
    with open(tmp, "w", encoding="utf-8-sig") as f:   # BOM so PowerShell reads unicode
        f.write(ps)
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-WindowStyle", "Hidden", "-File", tmp],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def alert(cfg: dict, new_calls: list[dict]) -> None:
    subject, body = format_alert(new_calls)
    log("ALERT: " + subject)
    for c in new_calls:
        log("   " + f"{c['location']} — {c['description']} (P{c['priority']}) "
                    f"{c['entry_time']} zone {c['zone']} {c['call_number']}")
    send_expo_push(cfg, subject, body)
    send_ntfy(cfg, subject, body)
    send_email(cfg, subject, body)
    send_desktop(cfg, subject, body)


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------
def check_once(cfg: dict, state: dict) -> None:
    try:
        calls = fetch_calls(cfg)
    except Exception as e:
        log(f"WARNING: fetch failed ({e}); will retry next cycle")
        return

    matched = [c for c in calls if matches_watch(c, cfg["watch_locations"])]
    seen = state["seen"]
    first_run = state.get("_initialized") is not True

    new_calls = [c for c in matched if c["call_number"] not in seen]

    now_iso = datetime.now(timezone.utc).isoformat()

    if first_run and cfg["seed_silently_on_first_run"]:
        for c in matched:
            seen[c["call_number"]] = now_iso
        state["_initialized"] = True
        watch = ", ".join(cfg["watch_locations"])
        log(f"First run: watching [{watch}]. Seeded {len(matched)} current match(es) "
            f"without alerting. {len(calls)} total active calls on the board.")
        save_state(cfg, state)
        return

    state["_initialized"] = True

    if new_calls:
        for c in new_calls:
            seen[c["call_number"]] = now_iso
        alert(cfg, new_calls)
        save_state(cfg, state)
    else:
        log(f"No new watched calls. ({len(matched)} watched active / "
            f"{len(calls)} total on board)")


def run_forever(cfg: dict) -> None:
    state = load_state(cfg)
    watch = ", ".join(cfg["watch_locations"])
    log(f"Starting VCSO monitor. Watching: {watch}. Polling every {cfg['poll_seconds']}s.")
    while True:
        check_once(cfg, state)
        time.sleep(cfg["poll_seconds"])


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Monitor VCSO active calls for watched locations.")
    ap.add_argument("--once", action="store_true", help="Check once and exit (for cron).")
    ap.add_argument("--test", action="store_true", help="Send a test alert and exit.")
    ap.add_argument("--show", action="store_true", help="Print current watched calls and exit.")
    args = ap.parse_args()

    cfg = load_config()

    if args.test:
        sample = [{"call_number": "TEST0001", "description": "Test Alert", "priority": "3",
                   "location": cfg["watch_locations"][0], "entry_time": "12:00 PM", "zone": "00"}]
        alert(cfg, sample)
        return

    if args.show:
        calls = fetch_calls(cfg)
        matched = [c for c in calls if matches_watch(c, cfg["watch_locations"])]
        log(f"{len(calls)} total active calls; {len(matched)} match {cfg['watch_locations']}:")
        for c in matched:
            print("   ", c)
        return

    if args.once:
        state = load_state(cfg)
        check_once(cfg, state)
        return

    try:
        run_forever(cfg)
    except KeyboardInterrupt:
        log("Stopped.")


if __name__ == "__main__":
    main()
