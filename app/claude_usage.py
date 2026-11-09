"""Claude's plan usage for the daily summary, read from the Claude runner on
Oliver's Mac (homelab repo, mac/claude-runner: GET /v1/usage). It's the whole
account's usage (the runner, Claude Code, claude.ai), as Claude reports it."""
import json
import logging
import urllib.request
from datetime import datetime

from .config import settings

logger = logging.getLogger(__name__)


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %-d %b, %H:%M")  # container local time (TZ)


def lines() -> list[str]:
    """The summary's Claude lines, e.g. "Weekly usage - 23%, resets Sat 14 Nov, 09:00".
    Empty when the runner isn't configured."""
    if not (settings.claude_runner_url and settings.claude_runner_token):
        return []
    req = urllib.request.Request(settings.claude_runner_url.rstrip("/") + "/v1/usage?max_age=3600",
                                 headers={"Authorization": f"Bearer {settings.claude_runner_token}"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            u = json.load(r)
    except Exception as e:  # noqa: BLE001 — the summary goes out regardless
        logger.warning("claude usage: %s", e)
        return ["Weekly usage - can't read it (is the Mac on?)"]
    out = []
    for key, label in (("seven_day", "Weekly usage"), ("five_hour", "5-hour window")):
        w = u.get(key)
        if not w or (key == "five_hour" and w.get("used_percent", 0) < 80):
            continue  # the 5-hour window only when it's close to the limit
        out.append(f"{label} - {w['used_percent']}%" + (f", resets {_when(w['resets_at'])}" if w.get("resets_at") else ""))
    return out
