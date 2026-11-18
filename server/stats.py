"""Store the activity reports machines send and summarize them for the Kindle."""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

WINDOW_DAYS = 7
HOST_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def save_report(state_dir: str, report: dict) -> str:
    """Validate a report and keep it as <state_dir>/<host>.json, replacing the host's last one."""
    host = report.get("host")
    if not isinstance(host, str) or not HOST_RE.match(host):
        raise ValueError("report needs a host made of letters, digits, - or _")
    for key in ("prompts", "tokens", "tools"):
        if not isinstance(report.get(key), list):
            raise ValueError(f"report needs a {key} list")
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, host + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(report, f)
    os.replace(tmp, path)
    return host


def load_reports(state_dir: str) -> list[dict]:
    if not os.path.isdir(state_dir):
        return []
    reports = []
    for name in sorted(os.listdir(state_dir)):
        if name.endswith(".json"):
            try:
                with open(os.path.join(state_dir, name)) as f:
                    reports.append(json.load(f))
            except (OSError, ValueError):
                continue
    return reports


def model_label(model: str) -> str:
    """claude-opus-5-5 -> Opus 5.5, claude-3-5-sonnet-20241022 -> Sonnet 3.5."""
    parts = [p for p in model.removeprefix("claude-").split("-") if not (p.isdigit() and len(p) == 8)]
    words = [p for p in parts if not p.isdigit()]
    nums = [p for p in parts if p.isdigit()]
    return " ".join([" ".join(w.title() for w in words), ".".join(nums)]).strip() or model


def summarize(reports: list[dict], now: datetime) -> dict | None:
    """Merge reports into totals for the last WINDOW_DAYS local days, today included.

    `now` must be timezone-aware; days and hours are bucketed in its timezone.
    Returns None when no report has anything in the window.
    """
    tz = now.tzinfo
    first_day = now.date() - timedelta(days=WINDOW_DAYS - 1)
    start = datetime.combine(first_day, datetime.min.time(), tz).timestamp()

    def local(ts: float) -> datetime:
        return datetime.fromtimestamp(ts, tz)

    days = [0] * WINDOW_DAYS
    heat: Counter = Counter()
    sessions = set()
    models: Counter = Counter()
    projects: Counter = Counter()
    tools: Counter = Counter()
    hosts = []
    for r in reports:
        host = r.get("host", "?")
        hosts.append(host)
        for ts, _project, session in r["prompts"]:
            if ts < start:
                continue
            t = local(ts)
            row = (t.date() - first_day).days
            if 0 <= row < WINDOW_DAYS:
                days[row] += 1
                heat[(row, t.hour)] += 1
                sessions.add((host, session))
        for hour, project, model, n in r["tokens"]:
            if hour >= start:
                models[model_label(model)] += n
                projects[project] += n
        for hour, name, n in r["tools"]:
            if hour >= start:
                tools[name.removeprefix("mcp__").split("__")[0] if name.startswith("mcp__") else name] += n

    if not sum(days) and not models:
        return None
    return {
        "days": [((first_day + timedelta(days=i)).strftime("%a")[:2], n) for i, n in enumerate(days)],
        "heat": dict(heat),
        "prompts": sum(days),
        "sessions": len(sessions),
        "tokens": sum(models.values()),
        "models": models.most_common(),
        "projects": projects.most_common(),
        "tools": tools.most_common(),
        "hosts": sorted(hosts),
    }
