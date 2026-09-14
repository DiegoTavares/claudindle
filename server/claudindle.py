#!/usr/bin/env python3
"""Render Claude subscription usage as a Kindle-sized PNG and serve it over HTTP.

Usage:
  claudindle.py render [--out usage.png]   # one-shot render
  claudindle.py serve  [--port 8080]       # re-render every REFRESH seconds and serve /usage.png

  claudindle.py login                      # one-time OAuth login (browser + paste code)

Token lookup order:
  1. CLAUDE_OAUTH_TOKEN env var
  2. credentials saved by `login` (CLAUDINDLE_CREDS, default ~/.config/claudindle/credentials.json),
     refreshed automatically when expired
  3. token file at CLAUDINDLE_TOKEN_FILE (default ~/.config/claudindle/token)
  4. macOS Keychain entry used by Claude Code (dev convenience on a Mac)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# Claude Code's public OAuth client; same flow as `claude login`.
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
OAUTH_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
OAUTH_SCOPES = "user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"
CREDS_PATH = os.path.expanduser(os.environ.get("CLAUDINDLE_CREDS", "~/.config/claudindle/credentials.json"))
TZ = ZoneInfo(os.environ.get("CLAUDINDLE_TZ", "America/Vancouver"))
REFRESH = int(os.environ.get("CLAUDINDLE_REFRESH", "180"))
WIDTH, HEIGHT = 1072, 1448  # Kindle Paperwhite 7th gen, portrait

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/Library/Fonts/DejaVuSansMono.ttf",
]


# --------------------------------------------------------------- oauth ----
def _post_json(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "claudindle/0.1"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def save_creds(tok: dict) -> None:
    os.makedirs(os.path.dirname(CREDS_PATH), exist_ok=True)
    creds = {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "expires_at": time.time() + int(tok.get("expires_in", 3600)),
        "scope": tok.get("scope"),
    }
    fd = os.open(CREDS_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(creds, f, indent=2)


def login() -> None:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    url = OAUTH_AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "code": "true", "client_id": OAUTH_CLIENT_ID, "response_type": "code",
        "redirect_uri": OAUTH_REDIRECT_URI, "scope": OAUTH_SCOPES,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
    })
    print("Open this URL in a browser, sign in, and paste the code shown:\n\n" + url + "\n")
    raw = input("Code: ").strip()
    code, _, got_state = raw.partition("#")
    tok = _post_json(OAUTH_TOKEN_URL, {
        "grant_type": "authorization_code", "code": code, "state": got_state or state,
        "client_id": OAUTH_CLIENT_ID, "redirect_uri": OAUTH_REDIRECT_URI, "code_verifier": verifier,
    })
    save_creds(tok)
    print("Saved credentials to", CREDS_PATH)


def token_from_creds() -> str | None:
    if not os.path.exists(CREDS_PATH):
        return None
    with open(CREDS_PATH) as f:
        creds = json.load(f)
    if time.time() < creds.get("expires_at", 0) - 120:
        return creds["access_token"]
    if not creds.get("refresh_token"):
        return creds["access_token"]
    tok = _post_json(OAUTH_TOKEN_URL, {
        "grant_type": "refresh_token", "refresh_token": creds["refresh_token"],
        "client_id": OAUTH_CLIENT_ID,
    })
    tok.setdefault("refresh_token", creds["refresh_token"])
    save_creds(tok)
    print(time.strftime("%H:%M:%S"), "refreshed access token", flush=True)
    return tok["access_token"]


# ---------------------------------------------------------------- data ----
def get_token() -> str:
    tok = os.environ.get("CLAUDE_OAUTH_TOKEN")
    if tok:
        return tok.strip()
    tok = token_from_creds()
    if tok:
        return tok
    path = os.path.expanduser(os.environ.get("CLAUDINDLE_TOKEN_FILE", "~/.config/claudindle/token"))
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    if sys.platform == "darwin":
        try:
            raw = subprocess.check_output(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                stderr=subprocess.DEVNULL,
            )
            return json.loads(raw)["claudeAiOauth"]["accessToken"]
        except Exception:
            pass
    raise RuntimeError("No token: set CLAUDE_OAUTH_TOKEN or write it to " + path)


def fetch_usage() -> dict:
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": "Bearer " + get_token(),
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claudindle/0.1",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def label_for(limit: dict) -> str:
    kind = limit.get("kind")
    if kind == "session":
        return "Current session"
    if kind == "weekly_all":
        return "Current week (all models)"
    if kind == "weekly_scoped":
        scope = limit.get("scope") or {}
        name = ((scope.get("model") or {}).get("display_name")
                or (scope.get("surface") or {}).get("display_name") or "scoped")
        return f"Current week ({name})"
    return kind or "Limit"


def fmt_reset(iso: str | None) -> str:
    if not iso:
        return ""
    dt = datetime.fromisoformat(iso).astimezone(TZ)
    dt = (dt + timedelta(seconds=30)).replace(second=0, microsecond=0)  # nearest minute
    now = datetime.now(TZ)
    hour = dt.strftime("%I").lstrip("0")
    ampm = dt.strftime("%p").lower()
    clock = f"{hour}{ampm}" if dt.minute == 0 else f"{hour}:{dt.minute:02d}{ampm}"
    if dt.date() == now.date():
        return f"Resets {clock}"
    return f"Resets {dt.strftime('%b')} {dt.day} at {clock}"


# ------------------------------------------------------------- rendering --
def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if not os.path.exists(path):
            continue
        if bold and "Bold" not in path and not path.endswith(".ttc"):
            continue
        try:
            # Menlo.ttc index 1 is Bold
            return ImageFont.truetype(path, size, index=1 if (bold and path.endswith(".ttc")) else 0)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def render(usage: dict | None, error: str | None = None) -> Image.Image:
    img = Image.new("L", (WIDTH, HEIGHT), 255)
    d = ImageDraw.Draw(img)
    f_title = load_font(58, bold=True)
    f_pct = load_font(50)
    f_sub = load_font(40)
    f_small = load_font(30)

    margin = 64
    y = 90
    d.text((margin, y), "Claude usage", font=load_font(72, bold=True), fill=0)
    y += 150

    limits = (usage or {}).get("limits") or []
    if error:
        d.text((margin, y), "Could not fetch usage:", font=f_title, fill=0)
        y += 80
        import textwrap
        for line in textwrap.wrap(error[:400], 48)[:8]:
            d.text((margin, y), line, font=f_small, fill=0)
            y += 40
    elif not limits:
        d.text((margin, y), "No limits reported", font=f_title, fill=0)

    bar_w = 700
    bar_h = 64
    for lim in limits:
        pct = max(0, min(100, int(round(lim.get("percent") or 0))))
        d.text((margin, y), label_for(lim), font=f_title, fill=0)
        y += 90
        # bar
        d.rectangle([margin, y, margin + bar_w, y + bar_h], outline=0, width=4)
        fill_w = int(bar_w * pct / 100)
        if fill_w:
            d.rectangle([margin, y, margin + fill_w, y + bar_h], fill=80)
        d.text((margin + bar_w + 30, y + 4), f"{pct}% used", font=f_pct, fill=0)
        y += bar_h + 24
        sub = fmt_reset(lim.get("resets_at"))
        if lim.get("severity") not in (None, "normal"):
            sub += f"   [{lim['severity']}]"
        d.text((margin, y), sub, font=f_sub, fill=110)
        y += 130

    stamp = datetime.now(TZ).strftime("Updated %b %-d %-I:%M%p").replace("AM", "am").replace("PM", "pm")
    d.text((margin, HEIGHT - 80), f"{stamp} ({TZ.key})", font=f_small, fill=110)
    return img


def render_png() -> bytes:
    try:
        img = render(fetch_usage())
    except Exception as e:  # keep the screen useful even when the API is down
        img = render(None, error=str(e))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- server --
class State:
    png: bytes = b""
    lock = threading.Lock()


def refresh_loop():
    while True:
        try:
            png = render_png()
            with State.lock:
                State.png = png
            print(time.strftime("%H:%M:%S"), "rendered", len(png), "bytes", flush=True)
        except Exception as e:
            print("render failed:", e, file=sys.stderr, flush=True)
        time.sleep(REFRESH)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] not in ("/", "/usage.png"):
            self.send_error(404)
            return
        with State.lock:
            png = State.png
        if not png:
            self.send_error(503, "not rendered yet")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(png)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(png)

    def log_message(self, fmt, *args):
        print(self.address_string(), fmt % args, flush=True)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("--out", default="usage.png")
    sub.add_parser("login")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--bind", default="0.0.0.0")
    a = ap.parse_args()

    if a.cmd == "login":
        login()
    elif a.cmd == "render":
        with open(a.out, "wb") as f:
            f.write(render_png())
        print("wrote", a.out)
    else:
        threading.Thread(target=refresh_loop, daemon=True).start()
        print(f"serving http://{a.bind}:{a.port}/usage.png every {REFRESH}s", flush=True)
        ThreadingHTTPServer((a.bind, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
