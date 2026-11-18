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
import functools
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
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

import stats

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

FONT_DIRS = [
    "/usr/share/fonts/truetype/dejavu",  # Debian (fonts-dejavu-core)
    "/usr/share/fonts/TTF",  # Arch (ttf-dejavu)
    "/Library/Fonts",
]
STATE_DIR = os.path.expanduser(os.environ.get("CLAUDINDLE_STATE", "~/.local/state/claudindle/reports"))
MAX_REPORT_BYTES = 5 * 1024 * 1024


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


PENDING_PATH = CREDS_PATH + ".pending"


def login(code: str | None = None) -> None:
    """Two steps: `login` prints a URL and remembers the PKCE verifier;
    `login --code <code>` (pasted from the browser) finishes the exchange."""
    if code is None:
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(32)
        url = OAUTH_AUTHORIZE_URL + "?" + urllib.parse.urlencode({
            "code": "true", "client_id": OAUTH_CLIENT_ID, "response_type": "code",
            "redirect_uri": OAUTH_REDIRECT_URI, "scope": OAUTH_SCOPES,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        })
        os.makedirs(os.path.dirname(CREDS_PATH), exist_ok=True)
        fd = os.open(PENDING_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"verifier": verifier, "state": state}, f)
        print("1. Open this URL in a browser and sign in:\n\n" + url + "\n")
        print("2. Then run:  claudindle.py login --code <code shown in the browser>")
        return
    if not os.path.exists(PENDING_PATH):
        raise SystemExit("Run `login` without --code first.")
    with open(PENDING_PATH) as f:
        pending = json.load(f)
    code, _, got_state = code.strip().partition("#")
    tok = _post_json(OAUTH_TOKEN_URL, {
        "grant_type": "authorization_code", "code": code, "state": got_state or pending["state"],
        "client_id": OAUTH_CLIENT_ID, "redirect_uri": OAUTH_REDIRECT_URI,
        "code_verifier": pending["verifier"],
    })
    save_creds(tok)
    os.remove(PENDING_PATH)
    print("Saved credentials to", CREDS_PATH)


class LoginExpired(Exception):
    """The refresh token was rejected; only a new `login` can fix it."""

    def __str__(self) -> str:
        return "Login expired: run `claudindle.py login`"


def token_from_creds() -> str | None:
    if not os.path.exists(CREDS_PATH):
        return None
    with open(CREDS_PATH) as f:
        creds = json.load(f)
    if time.time() < creds.get("expires_at", 0) - 120:
        return creds["access_token"]
    if not creds.get("refresh_token"):
        return creds["access_token"]
    try:
        tok = _post_json(OAUTH_TOKEN_URL, {
            "grant_type": "refresh_token", "refresh_token": creds["refresh_token"],
            "client_id": OAUTH_CLIENT_ID,
        })
    except urllib.error.HTTPError as e:
        if e.code == 400 and b"invalid_grant" in e.read():
            raise LoginExpired() from e
        raise
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
MARGIN = 64
RIGHT = WIDTH - MARGIN
BLACK, DARK, MID, LIGHT, WHITE = 0, 70, 136, 204, 255


@functools.lru_cache(maxsize=None)
def font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans" + ("Mono" if mono else "") + ("-Bold" if bold else "") + ".ttf"
    for d in FONT_DIRS:
        path = os.path.join(d, name)
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    if os.path.exists("/System/Library/Fonts/Menlo.ttc"):  # dev convenience on a Mac
        return ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", size, index=1 if bold else 0)
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def fmt_tokens(n: float) -> str:
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}k"
    return str(int(n))


def text_right(d: ImageDraw.ImageDraw, x: int, y: int, text: str, f, fill=BLACK) -> None:
    d.text((x - d.textlength(text, font=f), y), text, font=f, fill=fill)


def fit(d: ImageDraw.ImageDraw, text: str, f, width: int) -> str:
    """Shorten text with an ellipsis until it fits in width pixels."""
    if d.textlength(text, font=f) <= width:
        return text
    while text and d.textlength(text + "…", font=f) > width:
        text = text[:-1]
    return text + "…"


def fill_pattern(img: Image.Image, box: tuple[int, int, int, int], style: int) -> None:
    """Fill a box with one of four e-ink friendly patterns: solid, hatched, gray, light."""
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return
    style %= 4
    if style == 1:
        w, h = x1 - x0, y1 - y0
        tile = Image.new("L", (w, h), WHITE)
        td = ImageDraw.Draw(tile)
        for x in range(-h, w, 16):
            td.line([(x, h), (x + h, 0)], fill=BLACK, width=6)
        img.paste(tile, (x0, y0))
    else:
        ImageDraw.Draw(img).rectangle(box, fill=(BLACK, None, MID, LIGHT)[style])


def draw_header(d: ImageDraw.ImageDraw) -> int:
    now = datetime.now(TZ)
    d.text((MARGIN, 52), "Claude", font=font(64, bold=True), fill=BLACK)
    text_right(d, RIGHT, 80, now.strftime("%a %b %-d"), font(30, mono=True), fill=DARK)
    d.rectangle([MARGIN, 150, RIGHT, 153], fill=BLACK)
    return 190


def draw_footer(d: ImageDraw.ImageDraw, right: str = "") -> None:
    stamp = datetime.now(TZ).strftime("Updated %-I:%M%p").replace("AM", "am").replace("PM", "pm")
    d.rectangle([MARGIN, HEIGHT - 96, RIGHT, HEIGHT - 95], fill=BLACK)
    d.text((MARGIN, HEIGHT - 80), stamp, font=font(26, mono=True), fill=DARK)
    if right:
        text_right(d, RIGHT, HEIGHT - 80, right, font(26, mono=True), fill=DARK)


def draw_label(d: ImageDraw.ImageDraw, y: int, text: str) -> int:
    d.text((MARGIN, y), text.upper(), font=font(26, mono=True), fill=DARK)
    return y + 46


def draw_limit(d: ImageDraw.ImageDraw, y: int, lim: dict) -> int:
    pct = max(0, min(100, int(round(lim.get("percent") or 0))))
    d.text((MARGIN, y + 14), label_for(lim), font=font(44, bold=True), fill=BLACK)
    text_right(d, RIGHT, y, f"{pct}%", font(64, bold=True, mono=True))
    y += 84
    d.rectangle([MARGIN, y, RIGHT, y + 64], outline=BLACK, width=4)
    if pct:
        d.rectangle([MARGIN, y, MARGIN + (RIGHT - MARGIN) * pct // 100, y + 64], fill=BLACK)
    y += 76
    sub = fmt_reset(lim.get("resets_at"))
    if lim.get("severity") not in (None, "normal"):
        sub += f"   [{lim['severity']}]"
    d.text((MARGIN, y), sub, font=font(28, mono=True), fill=DARK)
    return y + 76


def draw_models(img: Image.Image, d: ImageDraw.ImageDraw, y: int, models: list) -> int:
    models = models[:4]
    total = sum(n for _, n in models) or 1
    x = MARGIN
    for i, (_, n) in enumerate(models):
        w = round((RIGHT - MARGIN) * n / total) if i < len(models) - 1 else RIGHT - x
        fill_pattern(img, (x, y, x + w, y + 64), i)
        x += w
        if i < len(models) - 1:
            d.rectangle([x - 2, y, x + 1, y + 64], fill=WHITE)
    d.rectangle([MARGIN, y, RIGHT, y + 64], outline=BLACK, width=3)
    y += 88
    for i, (name, n) in enumerate(models):
        fill_pattern(img, (MARGIN, y, MARGIN + 34, y + 34), i)
        d.rectangle([MARGIN, y, MARGIN + 34, y + 34], outline=BLACK, width=3)
        d.text((MARGIN + 54, y - 4), name, font=font(34), fill=BLACK)
        text_right(d, RIGHT - 150, y, fmt_tokens(n), font(30, mono=True), fill=DARK)
        text_right(d, RIGHT, y, f"{round(100 * n / total)}%", font(30, bold=True, mono=True))
        y += 52
    return y


def draw_numbers(d: ImageDraw.ImageDraw, y: int, s: dict) -> int:
    cells = [(str(s["prompts"]), "prompts"), (str(s["sessions"]), "sessions"), (fmt_tokens(s["tokens"]), "tokens")]
    w = (RIGHT - MARGIN) // 3
    d.rectangle([MARGIN, y, RIGHT, y + 140], outline=BLACK, width=3)
    for i, (big, small) in enumerate(cells):
        x = MARGIN + i * w
        if i:
            d.rectangle([x - 1, y, x + 1, y + 140], fill=BLACK)
        d.text((x + 24, y + 12), big, font=font(68, bold=True), fill=BLACK)
        d.text((x + 24, y + 96), small, font=font(26, mono=True), fill=DARK)
    return y + 140


def heat_shade(n: int) -> int:
    return WHITE if n == 0 else LIGHT if n == 1 else MID if n <= 3 else BLACK


def draw_heat(d: ImageDraw.ImageDraw, y: int, s: dict) -> int:
    x0, gap, h = MARGIN + 90, 4, 40
    cw = (RIGHT - x0 - 23 * gap) / 24
    last = len(s["days"]) - 1
    for row, (day, _) in enumerate(s["days"]):
        d.text((MARGIN, y + 6), day, font=font(26, bold=row == last, mono=True), fill=BLACK if row == last else DARK)
        for hour in range(24):
            x = round(x0 + hour * (cw + gap))
            d.rectangle([x, y, round(x + cw), y + h], fill=heat_shade(s["heat"].get((row, hour), 0)), outline=LIGHT)
        y += h + gap
    for i, label in enumerate(["12am", "6am", "12pm", "6pm"]):
        d.text((round(x0 + i * 6 * (cw + gap)), y + 4), label, font=font(22, mono=True), fill=DARK)
    y += 44
    x = MARGIN
    d.text((x, y), "prompts/hour", font=font(24, mono=True), fill=DARK)
    x += 230
    for shade, label in [(WHITE, "0"), (LIGHT, "1"), (MID, "2-3"), (BLACK, "4+")]:
        d.rectangle([x, y, x + 28, y + 28], fill=shade, outline=MID)
        d.text((x + 38, y), label, font=font(24, mono=True), fill=DARK)
        x += 38 + d.textlength(label, font=font(24, mono=True)) + 30
    return y + 40


def draw_projects(d: ImageDraw.ImageDraw, y: int, projects: list, n: int = 4) -> int:
    top = projects[0][1] if projects else 1
    track0, track1 = MARGIN + 320, RIGHT - 170
    for i, (name, tok) in enumerate(projects[:n]):
        d.text((MARGIN, y - 2), fit(d, name, font(34), 300), font=font(34), fill=BLACK)
        w = max(3, round((track1 - track0) * tok / top))
        d.rectangle([track0, y + 4, track0 + w, y + 34], fill=BLACK if i == 0 else MID)
        text_right(d, RIGHT, y, fmt_tokens(tok), font(30, mono=True))
        y += 52
    return y


def draw_tools(d: ImageDraw.ImageDraw, y: int, tools: list) -> int:
    """Two columns of tool counts, as many rows as fit above the footer."""
    col_w = (RIGHT - MARGIN - 40) // 2
    f = font(30, mono=True)
    rows = max(0, (HEIGHT - 120 - y) // 54)
    tools = tools[:2 * rows]
    for i, (name, n) in enumerate(tools):
        x = MARGIN + (i % 2) * (col_w + 40)
        yy = y + (i // 2) * 54
        d.text((x, yy), fit(d, name, f, col_w - 120), font=f, fill=BLACK)
        text_right(d, x + col_w, yy, str(n), f)
        d.line([(x, yy + 44), (x + col_w, yy + 44)], fill=LIGHT, width=1)
    return y + 54 * ((len(tools) + 1) // 2)


def render_limits(usage: dict | None, activity: dict | None, error: str | None = None,
                  stale_since: datetime | None = None) -> Image.Image:
    """The limits screen: usage bars, and the 7-day model split when activity is known.

    With `usage`, `error` and `stale_since` the bars are the last good values and a note says
    when they are from and why they are not newer. With `error` alone, the error fills the screen.
    """
    img = Image.new("L", (WIDTH, HEIGHT), WHITE)
    d = ImageDraw.Draw(img)
    y = draw_header(d)
    limits = (usage or {}).get("limits") or []
    if usage and stale_since:
        when = stale_since.strftime("%-I:%M%p").replace("AM", "am").replace("PM", "pm")
        d.rectangle([MARGIN, y, RIGHT, y + 96], outline=BLACK, width=3)
        d.text((MARGIN + 20, y + 10), f"Showing limits from {when}", font=font(30, bold=True, mono=True), fill=BLACK)
        d.text((MARGIN + 20, y + 52), fit(d, error or "", font(26, mono=True), RIGHT - MARGIN - 40),
               font=font(26, mono=True), fill=DARK)
        y += 130
    elif error:
        d.text((MARGIN, y), "Could not fetch usage:", font=font(52, bold=True), fill=BLACK)
        y += 80
        import textwrap
        for line in textwrap.wrap(error[:400], 48)[:8]:
            d.text((MARGIN, y), line, font=font(30, mono=True), fill=BLACK)
            y += 40
        y += 40
    elif not limits:
        d.text((MARGIN, y), "No limits reported", font=font(52, bold=True), fill=BLACK)
        y += 120
    for lim in limits:
        y = draw_limit(d, y, lim)
    models_height = 56 + 88 + 52 * min(4, len((activity or {}).get("models") or []))
    if activity and activity["models"] and y + models_height <= HEIGHT - 110:
        y = draw_label(d, y + 10, "Models · last 7 days")
        draw_models(img, d, y, activity["models"])
    draw_footer(d, "1 of 2" if activity else "")
    return img


def render_activity(activity: dict) -> Image.Image:
    """The activity screen: totals, an hour-by-day grid, projects and tools for the last 7 days."""
    img = Image.new("L", (WIDTH, HEIGHT), WHITE)
    d = ImageDraw.Draw(img)
    y = draw_header(d)
    y = draw_label(d, y, "Last 7 days")
    y = draw_numbers(d, y, activity) + 36
    y = draw_label(d, y, "When you work")
    y = draw_heat(d, y, activity) + 30
    y = draw_label(d, y, "Projects · by tokens")
    y = draw_projects(d, y, activity["projects"]) + 30
    y = draw_label(d, y, "Tool calls")
    draw_tools(d, y, activity["tools"])
    draw_footer(d, ", ".join(activity["hosts"]) + " · 2 of 2")
    return img


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class UsageCache:
    """The last good usage response, and how long to wait after the API rate-limits us."""

    MAX_BACKOFF = 30 * 60

    def __init__(self):
        self.usage: dict | None = None
        self.fetched_at: datetime | None = None
        self.error: str | None = None
        self.strikes = 0
        self.retry_at = 0.0

    def get(self) -> tuple[dict | None, datetime | None, str | None]:
        """Returns (usage, stale_since, error). stale_since is None when usage is fresh."""
        now = time.time()
        if now >= self.retry_at:
            try:
                usage = fetch_usage()
            except Exception as e:
                print("fetch failed:", e, file=sys.stderr, flush=True)
                self.error = str(e)
                if isinstance(e, urllib.error.HTTPError) and e.code == 429:
                    self.strikes += 1
                    wait = min(self.MAX_BACKOFF, REFRESH * 2 ** self.strikes)
                    self.retry_at = now + wait
                    print(f"rate limited; next fetch in {wait // 60} min", file=sys.stderr, flush=True)
            else:
                self.usage, self.fetched_at, self.error = usage, datetime.now(TZ), None
                self.strikes, self.retry_at = 0, 0.0
                return usage, None, None
        return self.usage, self.fetched_at if self.usage else None, self.error


USAGE = UsageCache()


def render_pages() -> dict[str, bytes]:
    """Render every screen. The activity screen is left out when no machine has reported."""
    activity = stats.summarize(stats.load_reports(STATE_DIR), datetime.now(TZ))
    usage, stale_since, error = USAGE.get()
    if usage:
        limits = render_limits(usage, activity, error=error, stale_since=stale_since)
    else:  # keep the screen useful even when the API is down
        limits = render_limits(None, activity, error=error)
    pages = {"limits": png_bytes(limits)}
    if activity:
        pages["activity"] = png_bytes(render_activity(activity))
    return pages


# ---------------------------------------------------------------- server --
class State:
    pages: dict[str, bytes] = {}
    turn = 0
    lock = threading.Lock()


def refresh_loop():
    while True:
        try:
            pages = render_pages()
            with State.lock:
                State.pages = pages
            print(time.strftime("%H:%M:%S"), "rendered", ", ".join(pages), flush=True)
        except Exception as e:
            print("render failed:", e, file=sys.stderr, flush=True)
        time.sleep(REFRESH)


def next_page(query: str) -> bytes | None:
    """The page named by ?page=, or else the next screen in turn."""
    wanted = urllib.parse.parse_qs(query).get("page", [None])[0]
    with State.lock:
        if wanted:
            return State.pages.get(wanted)
        names = sorted(State.pages)
        if not names:
            return None
        name = names[State.turn % len(names)]
        State.turn += 1
        return State.pages[name]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path not in ("/", "/usage.png"):
            self.send_error(404)
            return
        png = next_page(query)
        if not png:
            self.send_error(503 if not State.pages else 404, "not rendered yet")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(png)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(png)

    def do_POST(self):
        if self.path != "/report":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_REPORT_BYTES:
            self.send_error(413 if length else 411)
            return
        try:
            host = stats.save_report(STATE_DIR, json.loads(self.rfile.read(length)))
        except ValueError as e:
            self.send_error(400, str(e))
            return
        print(time.strftime("%H:%M:%S"), "report from", host, flush=True)
        self.send_response(204)
        self.end_headers()

    def log_message(self, fmt, *args):
        print(self.address_string(), fmt % args, flush=True)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("--out", default="usage.png")
    r.add_argument("--page", choices=["limits", "activity"], default="limits")
    l = sub.add_parser("login")
    l.add_argument("--code", help="code shown in the browser after signing in")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--bind", default="0.0.0.0")
    a = ap.parse_args()

    if a.cmd == "login":
        login(a.code)
    elif a.cmd == "render":
        pages = render_pages()
        if a.page not in pages:
            raise SystemExit(f"no {a.page} page: no machine has reported activity to {STATE_DIR}")
        with open(a.out, "wb") as f:
            f.write(pages[a.page])
        print("wrote", a.out)
    else:
        threading.Thread(target=refresh_loop, daemon=True).start()
        print(f"serving http://{a.bind}:{a.port}/usage.png every {REFRESH}s", flush=True)
        ThreadingHTTPServer((a.bind, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
