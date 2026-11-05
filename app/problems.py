"""Problems: what's wrong right now, kept open until it clears.

detect() turns a device's latest report, its checks and alert state into a list
of problems; sync() runs every monitor sweep, opening new ones and resolving
ones that have cleared. Offline, thresholds, stopped containers, updates and
http/tcp/dns checks already push to ntfy where they're detected (api.py,
monitor.py), so sync() only pushes problems marked "push" here: missed
heartbeats, certificates, PBS and Proxmox findings, unhealthy or restart-looping
containers. High problems nobody acknowledges get a reminder every
REMINDER_HOURS.
"""
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import Session, delete, select

from . import notifier
from .config import settings
from .database import engine
from .models import CheckDay, Device, Problem, ServiceCheck

logger = logging.getLogger("devicemanager.problems")

SEVERITIES = [("high", "High"), ("warning", "Warning")]
GIB = 1024 ** 3


def ago(seconds: float) -> str:
    s = int(max(seconds, 0))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h" + (f" {s % 3600 // 60:02d}m" if s % 3600 // 60 else "")
    return f"{s // 86400}d" + (f" {s % 86400 // 3600}h" if s % 86400 // 3600 else "")


def _p(key, severity, source, title, detail="", push=False):
    return {"key": key, "severity": severity, "source": source, "title": title, "detail": detail, "push": push}


RESTART_WINDOW = 30 * 60
RESTART_LOOP = 3  # restarts within the window


def restart_loops(d: Device, now: float | None = None) -> dict[str, int]:
    """{container: restarts in the last 30 minutes} for those over RESTART_LOOP.
    From Device.container_restarts, which api.py keeps: [[epoch, count], ...]."""
    now = now or datetime.utcnow().replace(tzinfo=timezone.utc).timestamp()
    out = {}
    for name, pts in (json.loads(d.container_restarts) if d.container_restarts else {}).items():
        counts = [c for t, c in pts if now - t <= RESTART_WINDOW]
        if counts and max(counts) - min(counts) >= RESTART_LOOP:
            out[name] = max(counts) - min(counts)
    return out


def check_problems(c: ServiceCheck, now: datetime) -> list[dict]:
    if not c.enabled:
        return []
    out = []
    if c.kind == "heartbeat":
        if c.last_status == "down":
            last = f"Last ping {ago((now - c.last_checked_at).total_seconds())} ago." if c.last_checked_at else "No ping yet."
            out.append(_p(f"check:{c.id}", "warning", "Heartbeat", f"{c.name} missed",
                          f"Expected every {ago(c.interval_seconds)} (+{ago(c.grace_seconds)} grace). {last}", push=True))
        return out
    if c.kind in ("pbs", "proxmox"):
        source = "Backups" if c.kind == "pbs" else "Proxmox"
        issues = json.loads(c.issues) if c.issues else []
        for i in issues:
            out.append(_p(f"check:{c.id}:{i['key']}", i.get("severity") or "warning", source,
                          f"{c.name}: {i['title']}", i.get("detail") or "", push=True))
        if c.last_status == "down" and not issues:
            what = "PBS" if c.kind == "pbs" else "Proxmox"
            out.append(_p(f"check:{c.id}", "high", source, f"{c.name}: can't read {what}", c.last_error or "", push=True))
        return out
    if c.last_status == "down":
        out.append(_p(f"check:{c.id}", "high", "Check", f"{c.name} is down", c.last_error or c.target))
    if c.cert_expires_at:
        days = (c.cert_expires_at - now).total_seconds() / 86400
        if days < settings.tls_warn_days:
            when = "has expired" if days < 0 else f"expires in {int(days)} day{'s' if int(days) != 1 else ''}"
            out.append(_p(f"cert:{c.id}", "high" if days < 3 else "warning", "Certificate",
                          f"{c.name} certificate {when}", f"{c.target} · {c.cert_expires_at:%Y-%m-%d}", push=True))
    return out


def detect(d: Device, checks: list[ServiceCheck], now: datetime, host: Device | None = None) -> list[dict]:
    """host: the machine d runs on (topology.parents), to say so when both are down."""
    if not d.last_seen_at:
        return []  # never reported: a device being set up, not a problem yet
    if not d.is_online:
        detail = f"No report for {ago((now - d.last_seen_at).total_seconds())} (last seen {d.last_seen_at:%Y-%m-%d %H:%M} UTC)."
        if host is not None and not host.is_online:
            detail += f" Its host {host.name} is offline too."
        # checks still run against an offline device's services (a check can watch them from outside)
        out = [] if d.offline_ok else [_p("offline", "high", "Device", "Offline", detail)]
        for c in checks:
            out += check_problems(c, now)
        return out
    rep = json.loads(d.last_report_json) if d.last_report_json else {}
    sys = rep.get("system") or {}
    out = []
    disks = sys.get("disks") or [{"mount": "Disk", "percent": sys.get("disk_percent") or 0,
                                  "used_bytes": sys.get("disk_used_bytes"), "total_bytes": sys.get("disk_total_bytes")}]
    for dk in disks:
        if (dk.get("percent") or 0) >= d.threshold("disk") > 0:
            mount = (dk.get("mount") or "Disk").rstrip("\\") or "/"
            left = f"About {(dk['total_bytes'] - dk['used_bytes']) / GIB:.1f} GiB left." if dk.get("total_bytes") and dk.get("used_bytes") is not None else ""
            out.append(_p(f"disk:{dk.get('mount')}", "high", f"Disk {mount}", f"{mount} is {dk['percent']:.0f}% full", left))
    state = json.loads(d.alert_state) if d.alert_state else {}
    for key, label in (("cpu", "CPU"), ("mem", "Memory")):
        if state.get(key):
            out.append(_p(key, "warning", label, f"{label} high",
                          f"{sys.get(key + '_percent', 0):.0f}% now, over the limit for {settings.alert_sustained_minutes}m+."))
    if d.tailscale_connected is False:
        out.append(_p("tailscale", "warning", "Tailscale", "Tailscale disconnected", "SSH over Tailscale won't work until it reconnects."))
    apt = rep.get("apt") or {}
    if d.apt_needs_update:
        n = apt.get("upgradable")
        windows = apt.get("source") == "windows_update"
        title = f"{n} {'Windows ' if windows else ''}updates pending" if n else "Package list is stale" if n == 0 else "Updates pending"
        age = apt.get("last_update_age_seconds")
        out.append(_p("updates", "warning", "Updates", title, f"Package list refreshed {ago(age)} ago." if age and not windows else ""))
    loops = restart_loops(d, now.replace(tzinfo=timezone.utc).timestamp())
    for c in rep.get("docker_containers") or []:
        if c["name"] in loops:
            out.append(_p(f"restarts:{c['name']}", "high", "Container", f"{c['name']} keeps restarting",
                          f"{loops[c['name']]} restarts in the last 30 minutes. Check its logs.", push=True))
        if c.get("status") != "running":
            out.append(_p(f"container:{c['name']}", "warning", "Container", f"{c['name']} {c.get('status') or 'stopped'}", c.get("image") or ""))
        elif c.get("health") == "unhealthy":
            out.append(_p(f"health:{c['name']}", "warning", "Container", f"{c['name']} unhealthy",
                          "Running, but its Docker healthcheck is failing.", push=True))
    if state.get("ssh_fail"):
        auth = rep.get("ssh_auth") or {}
        out.append(_p("ssh_fail", "warning", "SSH", f"{auth.get('recent_failed_count') or 'Many'} failed SSH logins",
                      f"Latest from {auth.get('last_failed_ip') or '?'}. Someone may be guessing passwords."))
    for c in checks:
        out += check_problems(c, now)
    return out


def sync(session: Session, now: datetime | None = None) -> list[Problem]:
    """Open/refresh/resolve problems. Returns the newly opened ones."""
    now = now or datetime.utcnow()
    from .topology import parents
    devices = {d.id: d for d in session.exec(select(Device)).all()}
    parent_of = parents(list(devices.values()))
    checks = session.exec(select(ServiceCheck)).all()
    open_ = {(p.device_id, p.key): p for p in session.exec(select(Problem).where(Problem.resolved_at == None)).all()}  # noqa: E711
    seen, new, push = set(), [], set()
    for d in devices.values():
        for f in detect(d, [c for c in checks if c.device_id == d.id], now, devices.get(parent_of.get(d.id))):
            k = (d.id, f["key"])
            seen.add(k)
            if f.pop("push"):
                push.add(k)
            p = open_.get(k)
            if p is None:
                p = Problem(device_id=d.id, opened_at=now, **f)
                new.append(p)
            else:
                p.severity, p.source, p.title, p.detail = f["severity"], f["source"], f["title"], f["detail"]
            session.add(p)
    for k, p in open_.items():
        if k not in seen:
            p.resolved_at = now
            session.add(p)
    session.commit()
    for p in new:
        d = devices[p.device_id]
        if (p.device_id, p.key) in push and not d.alerts_muted(now):
            notifier.send(f"{d.name}: {p.title}. {p.detail}".strip(), title=p.source,
                          priority="high" if p.severity == "high" else "default", tags="warning",
                          device_id=d.id, key=p.key)
    remind(session, devices, now)
    return new


def remind(session: Session, devices: dict, now: datetime) -> list[str]:
    """One "still open" push per REMINDER_HOURS for each high problem nobody
    has acknowledged. Returns the messages."""
    hours = settings.reminder_hours
    if hours <= 0:
        return []
    due = now - timedelta(hours=hours)
    out = []
    for p in session.exec(select(Problem).where(Problem.resolved_at == None, Problem.acked_at == None,  # noqa: E711
                                                Problem.severity == "high")).all():
        d = devices.get(p.device_id)
        if d is None or (p.reminded_at or p.opened_at) > due or d.alerts_muted(now):
            continue
        msg = f"{d.name}: {p.title}, still open after {ago((now - p.opened_at).total_seconds())}. Acknowledge it if it can wait."
        notifier.send(msg, title="Still open", priority="default", tags="hourglass", device_id=d.id, key=p.key)
        p.reminded_at = now
        session.add(p)
        out.append(msg)
    session.commit()
    return out


def record_check(session: Session, check_id: int, ok: bool, now: datetime) -> None:
    day = now.strftime("%Y-%m-%d")
    row = session.exec(select(CheckDay).where(CheckDay.check_id == check_id, CheckDay.day == day)).first()
    row = row or CheckDay(check_id=check_id, day=day)
    row.ok += int(ok)
    row.total += 1
    session.add(row)


def uptime(rows: list[CheckDay], today: date, days: int = 30) -> tuple[list[float | None], float | None]:
    """Per-day pass ratio (None = no data), oldest first, and the overall ratio."""
    by_day = {r.day: r for r in rows}
    ratios = []
    for i in range(days - 1, -1, -1):
        r = by_day.get((today - timedelta(days=i)).isoformat())
        ratios.append(r.ok / r.total if r and r.total else None)
    ok, total = sum(r.ok for r in rows), sum(r.total for r in rows)
    return ratios, (ok / total if total else None)


def _hm(utc: datetime) -> str:
    return utc.replace(tzinfo=timezone.utc).astimezone().strftime("%H:%M")


def _size(b: float) -> str:
    return f"{b / GIB / 1024:.1f} TiB" if abs(b) >= 1024 * GIB else f"{b / GIB:.1f} GiB"


def _docker_time(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s[:19]) if s and not s.startswith("0001") else None
    except ValueError:
        return None


def digest(session: Session, now: datetime, disks_before: dict) -> tuple[str, dict]:
    """What happened in the last 24h: outages, restarts, errors, storage growth.
    Acknowledged problems and devices with alerts muted (a PC that's often
    off) are left out. disks_before is the snapshot from the last summary;
    returns the text and the new snapshot."""
    since = now - timedelta(hours=24)
    devices = {d.id: d for d in session.exec(select(Device)).all()}
    quiet = {i for i, d in devices.items() if d.alerts_muted(now)}
    switched_off = {i for i, d in devices.items() if d.offline_ok}
    name = lambda i: devices[i].name if i in devices else "?"
    probs = [p for p in session.exec(select(Problem).where(
        (Problem.opened_at >= since) | (Problem.resolved_at >= since) | (Problem.resolved_at == None))).all()  # noqa: E711
        if p.device_id not in quiet and not p.acked_at]
    sections, disks_now = {}, {}
    add = lambda k, line: sections.setdefault(k, []).append(line)

    for p in sorted(probs, key=lambda p: p.opened_at):
        if p.opened_at < since and p.resolved_at is None:
            continue  # older and still open: already in an earlier summary, on the Problems page
        if p.key == "offline":
            if p.device_id in switched_off:
                continue
            took = f"for {ago(((p.resolved_at or now) - p.opened_at).total_seconds())}" if p.resolved_at else "still"
            add("Went offline", f"{name(p.device_id)} at {_hm(p.opened_at)}, {took}" + ("" if p.resolved_at else " offline"))
        elif p.opened_at >= since and p.key != "updates":  # pending updates aren't news
            add("Errors", f"{name(p.device_id)}: {p.title}" + (f" (cleared {_hm(p.resolved_at)})" if p.resolved_at else ""))

    for d in devices.values():
        rep = json.loads(d.last_report_json) if d.last_report_json else {}
        sys = rep.get("system") or {}
        for dk in sys.get("disks") or []:
            if dk.get("used_bytes") is None or not dk.get("total_bytes"):
                continue
            k = f"{d.id}|{dk.get('mount')}"
            disks_now[k] = dk["used_bytes"]
            grew = dk["used_bytes"] - disks_before.get(k, dk["used_bytes"])
            if abs(grew) >= GIB:
                add("Storage", f"{d.name} {dk.get('mount')}: {'+' if grew > 0 else '-'}{_size(abs(grew))}, "
                               f"{_size(dk['total_bytes'] - dk['used_bytes'])} left ({dk.get('percent', 0):.0f}% used)")
        if d.id in quiet or not d.is_online:
            continue
        booted = now - timedelta(seconds=sys["uptime_seconds"]) if sys.get("uptime_seconds") else None
        if booted and booted >= since:
            add("Restarted", f"{d.name} rebooted at {_hm(booted)}")
        for c in rep.get("docker_containers") or []:
            started = _docker_time(c.get("started_at"))
            if c.get("status") == "running" and started and started >= since and not (booted and started - booted < timedelta(minutes=10)):
                add("Restarted", f"{d.name} {c['name']} at {_hm(started)}")

    checks = {c.id: c for c in session.exec(select(ServiceCheck)).all()}
    for c in checks.values():
        if c.kind in ("pbs", "proxmox") and c.enabled:
            add("Backups" if c.kind == "pbs" else "Proxmox", f"{c.name}: {c.summary or c.last_error or 'not read yet'}"
                + (f" ({c.last_error})" if c.summary and c.last_error else ""))
    days = {since.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")}
    for r in session.exec(select(CheckDay).where(CheckDay.day.in_(days))).all():
        c = checks.get(r.check_id)
        if c and c.kind not in ("pbs", "proxmox") and r.ok < r.total and c.device_id not in quiet:
            add("Errors", f"{c.name}: {r.total - r.ok} failed check{'s' if r.total - r.ok != 1 else ''} ({r.day[5:]})")

    order = ["Backups", "Proxmox", "Errors", "Went offline", "Restarted", "Storage"]
    text = "\n\n".join(f"{k}\n" + "\n".join(f"· {line}" for line in sections[k]) for k in order if k in sections)
    return text or "All quiet: no outages, restarts or errors, and storage is steady.", disks_now


def client_release(session: Session, stamp: Path) -> str | None:
    """Once per client release: the push saying which machines are behind it.
    Returns the message sent (None when nothing's due)."""
    from .routers.updates import _agent_version, is_outdated, latest_client_version  # here: the routers import this module
    versions = {d.name: _agent_version(d) for d in session.exec(select(Device)).all() if d.last_report_json}
    latest = latest_client_version(list(versions.values()))
    if not latest or _read(stamp) == latest:
        return None
    stamp.write_text(latest, encoding="utf-8")
    behind = sorted(n for n, v in versions.items() if is_outdated(v, latest))
    if not behind:
        return None
    msg = (f"Client {latest} is out. {len(behind)} machine{'s are' if len(behind) != 1 else ' is'} on an older release: "
           f"{', '.join(behind)}. Tap Update all on the Devices page.")
    notifier.send(msg, title="Client update", tags="arrow_up", click="/")
    return msg


def run() -> None:
    """One sweep: sync, prune, and the daily summary when it's due. Blocking
    (ntfy is plain HTTP): run it in a thread."""
    now = datetime.utcnow()
    with Session(engine) as session:
        sync(session, now)
        old = now - timedelta(days=31)
        session.exec(delete(Problem).where(Problem.resolved_at < old))
        session.exec(delete(CheckDay).where(CheckDay.day < old.strftime("%Y-%m-%d")))
        session.commit()
        client_release(session, settings.data_dir() / "client_release_notified.txt")
        local = datetime.now()
        stamp = settings.data_dir() / "last_digest.txt"
        if settings.digest_hour == local.hour and _read(stamp) != local.date().isoformat():
            stamp.write_text(local.date().isoformat(), encoding="utf-8")
            snap = settings.data_dir() / "digest_disks.json"
            text, disks = digest(session, now, json.loads(_read(snap) or "{}"))
            snap.write_text(json.dumps(disks), encoding="utf-8")
            notifier.send(text, title="Daily summary", tags="clipboard")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


if __name__ == "__main__":  # ponytail: self-check
    now = datetime(2026, 11, 3, 12, 0)
    d = Device(id=1, name="db", is_online=True, last_seen_at=now, apt_needs_update=True, alert_state='{"cpu": true}',
               last_report_json=json.dumps({"agent_version": "1.0.0", "system": {"cpu_percent": 97, "disks": [
                   {"mount": "/", "percent": 93, "used_bytes": 93 * GIB, "total_bytes": 100 * GIB},
                   {"mount": "/data", "percent": 40}]},
                   "apt": {"upgradable": 12, "reboot_required": True},
                   "docker_containers": [{"name": "web", "status": "running"}, {"name": "mqtt", "status": "exited"}]}))
    hb = ServiceCheck(id=7, device_id=1, name="backup", kind="heartbeat", interval_seconds=86400, grace_seconds=900,
                      last_status="down", last_checked_at=now - timedelta(hours=27))
    tls = ServiceCheck(id=8, device_id=1, name="site", kind="http", target="https://x", last_status="up",
                       cert_expires_at=now + timedelta(days=2))
    got = {p["key"]: p for p in detect(d, [hb, tls], now)}
    assert set(got) == {"disk:/", "cpu", "updates", "container:mqtt", "check:7", "cert:8"}, got
    assert got["disk:/"]["severity"] == "high" and "7.0 GiB left" in got["disk:/"]["detail"]
    assert got["cert:8"]["severity"] == "high" and "2 days" in got["cert:8"]["title"]
    assert "Last ping 1d 3h ago" in got["check:7"]["detail"]
    bad = ServiceCheck(id=9, device_id=1, name="pbs", kind="pbs", last_status="down", summary="3 backups; 1 failed",
                       issues=json.dumps([{"key": "task:backup:x", "severity": "high", "title": "backup vm/101 failed", "detail": "x"},
                                          {"key": "late:nas:ct/103", "severity": "warning", "title": "services: last backup 30h ago"}]))
    got_pbs = check_problems(bad, now)
    assert [g["key"] for g in got_pbs] == ["check:9:task:backup:x", "check:9:late:nas:ct/103"], got_pbs
    assert got_pbs[0]["title"] == "pbs: backup vm/101 failed" and got_pbs[0]["source"] == "Backups" and got_pbs[0]["push"]
    unread = ServiceCheck(id=10, device_id=1, name="pve", kind="proxmox", last_status="down", last_error="401")
    assert check_problems(unread, now)[0]["title"] == "pve: can't read Proxmox"
    d.is_online = False
    assert [p["key"] for p in detect(d, [hb], now)] == ["offline", "check:7"]  # checks still count while it's offline
    host = Device(id=9, name="pve", is_online=False)
    assert "Its host pve is offline too." in detect(d, [], now, host)[0]["detail"]
    d.is_online = True
    t0 = now.replace(tzinfo=timezone.utc).timestamp()
    d.container_restarts = json.dumps({"mqtt": [[t0 - 1500, 4], [t0 - 600, 6], [t0, 8]], "web": [[t0 - 3000, 1], [t0, 2]]})
    d.last_report_json = json.dumps({"system": {}, "docker_containers": [
        {"name": "mqtt", "status": "running"}, {"name": "web", "status": "running", "health": "unhealthy"}]})
    d.alert_state = json.dumps({"ssh_fail": True})
    got = {p["key"]: p for p in detect(d, [], now)}
    assert set(got) == {"restarts:mqtt", "health:web", "ssh_fail", "updates"}, got
    assert got["restarts:mqtt"]["detail"].startswith("4 restarts") and got["restarts:mqtt"]["push"]
    d.alert_disk_percent = 95  # per-device threshold
    d.last_report_json = json.dumps({"system": {"disks": [{"mount": "/", "percent": 93}]}})
    assert "disk:/" not in {p["key"] for p in detect(d, [], now)}
    d.is_online = False
    assert detect(Device(id=2, name="new"), [], now) == []
    ratios, overall = uptime([CheckDay(check_id=1, day="2026-11-03", ok=9, total=10)], date(2026, 11, 3), 3)
    assert ratios == [None, None, 0.9] and overall == 0.9

    from sqlmodel import SQLModel, create_engine
    mem = create_engine("sqlite://")
    SQLModel.metadata.create_all(mem)
    with Session(mem) as db:
        nas = Device(id=1, name="nas", is_online=True, last_report_json=json.dumps({"system": {
            "uptime_seconds": 3 * 3600, "disks": [{"mount": "/volume1", "used_bytes": 800 * GIB, "total_bytes": 1000 * GIB, "percent": 80}]},
            "docker_containers": [{"name": "mqtt", "status": "running", "started_at": "2026-11-03T11:00:00.1234Z"},
                                  {"name": "web", "status": "running", "started_at": "2026-11-03T09:02:00Z"}]}))
        pc = Device(id=2, name="oliver-pc", is_online=True, alerts_muted_until=datetime(9999, 1, 1),
                    last_report_json=json.dumps({"system": {"uptime_seconds": 600}}))
        db.add_all([nas, pc,
                    Problem(device_id=1, key="offline", severity="high", source="Device", title="Offline",
                            opened_at=now - timedelta(hours=5), resolved_at=now - timedelta(hours=5, minutes=-4)),
                    Problem(device_id=2, key="offline", severity="high", source="Device", title="Offline", opened_at=now - timedelta(hours=2)),
                    Problem(device_id=1, key="container:x", severity="warning", source="Container", title="x exited", opened_at=now - timedelta(hours=1)),
                    Problem(device_id=1, key="container:y", severity="warning", source="Container", title="y exited",
                            opened_at=now - timedelta(hours=1), acked_at=now),
                    Problem(device_id=1, key="updates", severity="warning", source="Updates", title="3 updates pending", opened_at=now),
                    ServiceCheck(id=7, device_id=1, name="backup", kind="heartbeat"),
                    ServiceCheck(id=9, device_id=1, name="pbs", kind="pbs", summary="12 backups, 1 GC, all OK"),
                    CheckDay(check_id=9, day="2026-11-03", ok=3, total=4),
                    CheckDay(check_id=7, day="2026-11-03", ok=10, total=12)])
        db.commit()
        text, snap = digest(db, now, {"1|/volume1": 758 * GIB})
    for want in ("Backups\n· pbs: 12 backups, 1 GC, all OK", "nas: x exited", "backup: 2 failed checks", "nas at", "for 4m", "nas rebooted at", "nas mqtt at",
                 "nas /volume1: +42.0 GiB, 200.0 GiB left (80% used)"):
        assert want in text, (want, text)
    for unwanted in ("y exited", "oliver-pc", "updates", "web", "pbs: 1 failed"):  # acked, muted device, not news, started at boot
        assert unwanted not in text, (unwanted, text)
    assert snap == {"1|/volume1": 800 * GIB}
    assert digest(Session(mem), now, snap)[0].count("Storage") == 0

    import tempfile
    import time
    from .routers import updates
    updates._latest_cache.update(at=time.time(), ttl=3600, version="1.2.0")
    stamp = Path(tempfile.mkdtemp()) / "notified.txt"
    with Session(mem) as db:
        db.add(Device(id=3, name="pbs", last_report_json=json.dumps({"agent_version": "1.1.0"})))
        db.add(Device(id=4, name="web", last_report_json=json.dumps({"agent_version": "1.2.0"})))
        db.commit()
        msg = client_release(db, stamp)  # nas and oliver-pc report no version: treated as old
        assert msg and "Client 1.2.0 is out. 3 machines" in msg and "pbs" in msg and "web" not in msg, msg
        assert client_release(db, stamp) is None  # once per release
    print(text)
    print("ok")
