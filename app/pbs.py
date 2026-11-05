"""Proxmox Backup Server: what its tasks (backups, verify, GC, prune, sync) did
in the last 24h, read from its API with a token that has the Audit role on /.

The 'pbs' check runs check() on its interval. Each thing found is its own
problem: a failed task, no backups at all in 24h, a guest whose newest backup is
late, a datastore filling up. Its summary goes in the daily summary.
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


def _ago(seconds: float) -> str:
    h = int(seconds // 3600)
    return f"{h // 24}d {h % 24}h" if h >= 48 else f"{h}h"


def _issue(key, severity, title, detail=""):
    return {"key": key, "severity": severity, "title": title, "detail": detail}


def assess(tasks: list[dict], groups: dict[str, list[dict]], usage: list[dict], now: float,
           max_age_hours: int = 26, retired_days: int = 14, full_percent: int = 90) -> tuple[str, list[dict]]:
    """Everything worth a problem: failed tasks, no backups at all in 24h,
    guests whose newest backup is late, datastores filling up.
    groups: {store: [group from /admin/datastore/{store}/groups]}. A group
    silent for retired_days is taken as a removed guest and ignored.
    -> (summary, issues)."""
    ok, summary, _ = summarize(tasks)
    issues = []
    for t in tasks:
        kind = KINDS.get(t.get("worker_type"))
        status = t.get("status") or "unknown"
        if kind and t.get("endtime") and status != "OK" and not status.startswith("WARNINGS"):
            what = (t.get("worker_id") or "").split(":", 1)[-1]
            issues.append(_issue(f"task:{t.get('worker_type')}:{t.get('worker_id')}", "high",
                                 f"{kind[0]} {what} failed".replace("  ", " "), status))
    if not any(KINDS.get(t.get("worker_type")) == KINDS["backup"] and t.get("endtime") for t in tasks):
        issues.append(_issue("no-backups", "high", "No backups in the last 24h",
                             "No backup task finished in the last 24 hours. Is the backup job still scheduled and enabled?"))
    late = 0
    for store, gs in groups.items():
        for g in gs:
            last = g.get("last-backup")
            if not last:
                continue
            age = now - last
            if max_age_hours * 3600 < age < retired_days * 86400:
                late += 1
                label = g.get("comment") or f"{g.get('backup-type')}/{g.get('backup-id')}"
                ident = f"{g.get('backup-type')}/{g.get('backup-id')}"
                issues.append(_issue(f"late:{store}:{ident}", "high" if age > 72 * 3600 else "warning",
                                     f"{label}: last backup {_ago(age)} ago",
                                     f"{ident} on datastore {store}. Expected within {max_age_hours}h."))
    full = []
    for u in usage:
        total, used = u.get("total") or 0, u.get("used") or 0
        if total > 0:
            pct = used / total * 100
            if pct >= full_percent > 0:
                full.append(f"{u.get('store')} {pct:.0f}% full")
                issues.append(_issue(f"store:{u.get('store')}", "high" if pct >= 95 else "warning",
                                     f"Datastore {u.get('store')} is {pct:.0f}% full",
                                     f"About {(total - used) / 1024 ** 3:.0f} GiB left."))
    extra = ([f"{late} guest{'s' if late != 1 else ''} late"] if late else []) + full
    return summary + ("; " + "; ".join(extra) if extra else ""), issues


async def check(url: str, token: str, now: float | None = None, max_age_hours: int = 26,
                retired_days: int = 14, full_percent: int = 90) -> tuple[bool, str, str | None, list[dict]]:
    """Reads PBS's tasks, backup groups and datastore usage.
    -> (all ok, summary, error text, issues). Raises PbsError in plain words
    when PBS can't be read at all."""
    now = now or time.time()
    headers = {"Authorization": f"PBSAPIToken={token}"}
    try:
        # PBS ships a self-signed certificate. ponytail: pin its fingerprint if the LAN isn't trusted
        async with httpx.AsyncClient(timeout=15, verify=False, headers=headers) as c:
            r = await c.get(f"{url}/api2/json/nodes/localhost/tasks", params={"since": int(now - 86400), "limit": 1000})
            if r.status_code == 401:
                raise PbsError(f"PBS at {url} refused the API token (401). Check it's USER@REALM!NAME:SECRET and not expired.")
            if r.status_code >= 400:
                raise PbsError(f"PBS at {url} answered {r.status_code}: {r.text[:150]}")
            tasks = r.json().get("data") or []
            groups, usage = {}, []
            # the rest is best effort: a token without Datastore.Audit still gets task results
            try:
                stores = (await c.get(f"{url}/api2/json/admin/datastore")).json().get("data") or []
                for st in stores:
                    name = st.get("store")
                    g = await c.get(f"{url}/api2/json/admin/datastore/{name}/groups")
                    if g.status_code < 400:
                        groups[name] = g.json().get("data") or []
                u = await c.get(f"{url}/api2/json/status/datastore-usage")
                usage = u.json().get("data") or [] if u.status_code < 400 else []
            except (httpx.HTTPError, ValueError):
                pass
    except httpx.HTTPError as exc:
        raise PbsError(f"Couldn't reach PBS at {url}: {exc or type(exc).__name__}") from exc
    summary, issues = assess(tasks, groups, usage, now, max_age_hours, retired_days, full_percent)
    if not tasks:
        summary += " (or the token lacks the Audit role on /)"
    error = "; ".join(i["title"] for i in issues[:3]) + (f" (+{len(issues) - 3} more)" if len(issues) > 3 else "") if issues else None
    return not issues, summary, error, issues


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

    now = 1_800_000_000
    groups = {"nas": [
        {"backup-type": "ct", "backup-id": "101", "last-backup": now - 3600, "comment": "authentik"},
        {"backup-type": "ct", "backup-id": "103", "last-backup": now - 30 * 3600, "comment": "services"},
        {"backup-type": "vm", "backup-id": "102", "last-backup": now - 80 * 3600},
        {"backup-type": "ct", "backup-id": "100", "last-backup": now - 40 * 86400},  # removed guest
    ]}
    usage = [{"store": "nas", "total": 100 * 1024 ** 3, "used": 93 * 1024 ** 3}]
    summary, issues = assess([t("backup", "OK"), t("backup", "job error", "nas:ct/105")], groups, usage, now)
    keys = {i["key"]: i for i in issues}
    assert set(keys) == {"task:backup:nas:ct/105", "late:nas:ct/103", "late:nas:vm/102", "store:nas"}, keys
    assert keys["late:nas:ct/103"]["title"] == "services: last backup 30h ago" and keys["late:nas:ct/103"]["severity"] == "warning"
    assert keys["late:nas:vm/102"]["severity"] == "high" and "3d 8h" in keys["late:nas:vm/102"]["title"]
    assert keys["store:nas"]["title"] == "Datastore nas is 93% full"
    assert summary == "2 backups; 1 failed; 2 guests late; nas 93% full", summary
    _, issues = assess([], {}, [], now)
    assert [i["key"] for i in issues] == ["no-backups"]
    _, issues = assess([t("garbage_collection", "OK", "nas")], {}, [], now)  # GC alone isn't a backup
    assert [i["key"] for i in issues] == ["no-backups"]
    assert assess([t("backup", "OK")], {}, [], now)[1] == []
    print("ok")
