#!/usr/bin/env python3
"""Summarize this machine's Claude Code activity and send it to the claudindle server.

Usage:
  report.py [--server http://192.168.1.104:8080] [--days 8] [--dry-run]

Reads the transcripts in ~/.claude/projects (CLAUDE_CONFIG_DIR is honored) and
POSTs counts only, never prompt text, to <server>/report. Standard library only,
so it runs on any machine with Python 3.9+.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

HOUR = 3600


def projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return Path(base) / "projects"


def project_name(cwd: str | None, fallback: str) -> str:
    """Name a project after its checkout, folding worktrees into their parent repo."""
    if not cwd:
        return fallback
    cwd = cwd.split("/.claude/worktrees/")[0].rstrip("/")
    return os.path.basename(cwd) or fallback


def is_prompt(entry: dict) -> bool:
    """True for a message the person typed, not tool results, commands or subagent turns."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip()) and not content.lstrip().startswith("<")
    if isinstance(content, list):
        kinds = {c.get("type") for c in content if isinstance(c, dict)}
        return "text" in kinds and "tool_result" not in kinds
    return False


def summarize(lines, since: float) -> dict:
    """Aggregate transcript lines (JSON strings) newer than `since` (epoch seconds)."""
    prompts = []
    seen_entries, seen_msgs = set(), set()
    tokens: Counter = Counter()
    tools: Counter = Counter()
    for line in lines:
        try:
            e = json.loads(line)
            ts = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).timestamp()
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if ts < since or e.get("isSidechain"):
            continue
        # Resumed sessions copy earlier entries into the new file; count each once.
        uid = e.get("uuid")
        if uid:
            if uid in seen_entries:
                continue
            seen_entries.add(uid)
        project = project_name(e.get("cwd"), "unknown")
        hour = int(ts // HOUR * HOUR)
        if is_prompt(e):
            prompts.append([int(ts), project, e.get("sessionId") or ""])
            continue
        msg = e.get("message") or {}
        model = msg.get("model")
        if e.get("type") != "assistant" or not model or model.startswith("<"):
            continue
        tools.update((hour, c["name"]) for c in msg.get("content") or []
                     if isinstance(c, dict) and c.get("type") == "tool_use")
        # One API response is split over several entries that repeat its usage.
        mid = msg.get("id") or uid
        if mid in seen_msgs:
            continue
        seen_msgs.add(mid)
        u = msg.get("usage") or {}
        tokens[(hour, project, model)] += (u.get("input_tokens", 0) + u.get("output_tokens", 0)
                                           + u.get("cache_creation_input_tokens", 0))
    return {
        "prompts": sorted(prompts),
        "tokens": [[h, p, m, n] for (h, p, m), n in sorted(tokens.items())],
        "tools": [[h, name, n] for (h, name), n in sorted(tools.items())],
    }


def transcript_lines(since: float):
    for path in projects_dir().glob("*/*.jsonl"):
        if path.stat().st_mtime < since:
            continue
        with open(path, errors="replace") as f:
            yield from f


def collect(days: int) -> dict:
    since = time.time() - days * 86400
    return summarize(transcript_lines(since), since)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=os.environ.get("CLAUDINDLE_SERVER", "http://192.168.1.104:8080"))
    ap.add_argument("--days", type=int, default=8)
    ap.add_argument("--host", default=socket.gethostname().split(".")[0])
    ap.add_argument("--dry-run", action="store_true", help="print the payload instead of sending it")
    a = ap.parse_args()

    payload = {"host": a.host, "sent_at": int(time.time()), **collect(a.days)}
    body = json.dumps(payload).encode()
    if a.dry_run:
        print(json.dumps(payload, indent=1))
        return
    req = urllib.request.Request(a.server.rstrip("/") + "/report", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
    except OSError as e:
        sys.exit(f"report to {a.server} failed: {e}")
    print(f"sent {len(payload['prompts'])} prompts from {a.host}")


if __name__ == "__main__":
    main()
