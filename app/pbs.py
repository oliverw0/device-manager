"""Proxmox Backup Server: what its tasks (backups, verify, GC, prune, sync) did
in the last 24h, read from its API with a token that has the Audit role on /.

The 'pbs' check runs check() on its interval: down when a task failed or PBS
can't be reached (a problem on the Problems page), and its summary goes in the
daily summary.
"""
import time
from urllib.parse import urlsplit

import httpx

class PbsError(Exception):
    """PBS couldn't be read; the message says why."""


# worker_type -> (one, many)
KINDS = {
    "backup": ("backup", "backups"), "verify": ("verify", "verifies"), "verificationjob": ("verify", "verifies"),
    "garbage_collection": ("GC", "GC runs"), "prune": ("prune", "prunes"), "prunejob": ("prune", "prunes"),
    "syncjob": ("sync", "syncs"), "tape-backup": ("tape backup", "tape backups"),
}


def normalize_url(target: str) -> str:
    """'pbs.lan' or '10.0.0.5' -> https://host:8007 (PBS's port)."""
    url = target.strip().rstrip("/")
    url = url if "://" in url else "https://" + url
    return url if urlsplit(url).port else url + ":8007"


def summarize(tasks: list[dict]) -> tuple[bool, str, str | None]:
    """-> (all ok, summary, error). Running tasks (no endtime) are skipped."""
    counts, failed, warned = {}, [], 0
    for t in tasks:
        kind = KINDS.get(t.get("worker_type"))
        if not kind or not t.get("endtime"):
            continue
        counts[kind] = counts.get(kind, 0) + 1
        status = t.get("status") or "unknown"
        if status.startswith("WARNINGS"):
            warned += 1
        elif status != "OK":
            what = (t.get("worker_id") or "").split(":", 1)[-1]
            failed.append(f"{kind[0]} {what} failed: {status}".replace("  ", " "))
    if not counts:
        return True, "no backup tasks in the last 24h", None
    done = ", ".join(f"{n} {kind[0] if n == 1 else kind[1]}" for kind, n in counts.items())
    if failed:
        return False, f"{done}; {len(failed)} failed", "; ".join(failed[:3]) + (f" (+{len(failed) - 3} more)" if len(failed) > 3 else "")
    return True, f"{done}, all OK" + (f" ({warned} with warnings)" if warned else ""), None


async def check(url: str, token: str, now: float | None = None) -> tuple[bool, str, str | None]:
    """Same as summarize(), from the live API. Raises PbsError in plain words."""
    try:
        # PBS ships a self-signed certificate. ponytail: pin its fingerprint if the LAN isn't trusted
        async with httpx.AsyncClient(timeout=15, verify=False) as c:
            r = await c.get(f"{url}/api2/json/nodes/localhost/tasks",
                            params={"since": int((now or time.time()) - 86400), "limit": 1000},
                            headers={"Authorization": f"PBSAPIToken={token}"})
    except httpx.HTTPError as exc:
        raise PbsError(f"Couldn't reach PBS at {url}: {exc or type(exc).__name__}") from exc
    if r.status_code == 401:
        raise PbsError(f"PBS at {url} refused the API token (401). Check it's USER@REALM!NAME:SECRET and not expired.")
    if r.status_code >= 400:
        raise PbsError(f"PBS at {url} answered {r.status_code}: {r.text[:150]}")
    ok, summary, error = summarize(r.json().get("data") or [])
    if summary.startswith("no backup"):
        summary += " (or the token lacks the Audit role on /)"
    return ok, summary, error


if __name__ == "__main__":  # ponytail: self-check
    assert normalize_url("pbs.lan") == "https://pbs.lan:8007"
    assert normalize_url("http://10.0.0.5:8008/") == "http://10.0.0.5:8008"
    t = lambda kind, status, wid="store1:vm/101", end=1: {"worker_type": kind, "status": status, "worker_id": wid, "endtime": end}
    assert summarize([]) == (True, "no backup tasks in the last 24h", None)
    ok, s, err = summarize([t("backup", "OK"), t("backup", "OK"), t("garbage_collection", "OK", "store1"),
                            t("verificationjob", "WARNINGS: 1"), t("backup", "OK", end=None), t("logrotate", "OK")])
    assert ok and s == "2 backups, 1 GC, 1 verify, all OK (1 with warnings)" and err is None, s
    ok, s, err = summarize([t("backup", "OK"), t("backup", "connection error: broken pipe", "store1:ct/105")])
    assert not ok and s == "2 backups; 1 failed" and err == "backup ct/105 failed: connection error: broken pipe", (s, err)
    print("ok")
