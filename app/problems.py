"""Problems: what's wrong right now, kept open until it clears.

detect() turns a device's latest report, its checks and alert state into a list
of problems; sync() runs every monitor sweep, opening new ones and resolving
ones that have cleared. Offline, thresholds, containers, updates and service
checks already push to ntfy where they're detected (api.py, monitor.py), so
sync() only pushes for what's new here: missed heartbeats and certificates.
"""
import json
import logging
from datetime import date, datetime, timedelta
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


def _p(key, severity, source, title, detail=""):
    return {"key": key, "severity": severity, "source": source, "title": title, "detail": detail}


def check_problems(c: ServiceCheck, now: datetime) -> list[dict]:
    if not c.enabled:
        return []
    out = []
    if c.kind == "heartbeat":
        if c.last_status == "down":
            last = f"Last ping {ago((now - c.last_checked_at).total_seconds())} ago." if c.last_checked_at else "No ping yet."
            out.append(_p(f"check:{c.id}", "warning", "Heartbeat", f"{c.name} missed",
                          f"Expected every {ago(c.interval_seconds)} (+{ago(c.grace_seconds)} grace). {last}"))
        return out
    if c.last_status == "down":
        out.append(_p(f"check:{c.id}", "high", "Check", f"{c.name} is down", c.last_error or c.target))
    if c.cert_expires_at:
        days = (c.cert_expires_at - now).total_seconds() / 86400
        if days < settings.tls_warn_days:
            when = "has expired" if days < 0 else f"expires in {int(days)} day{'s' if int(days) != 1 else ''}"
            out.append(_p(f"cert:{c.id}", "high" if days < 3 else "warning", "Certificate",
                          f"{c.name} certificate {when}", f"{c.target} · {c.cert_expires_at:%Y-%m-%d}"))
    return out


def detect(d: Device, checks: list[ServiceCheck], now: datetime) -> list[dict]:
    if not d.last_seen_at:
        return []  # never reported: a device being set up, not a problem yet
    if not d.is_online:
        return [_p("offline", "high", "Device", "Offline",
                   f"No report for {ago((now - d.last_seen_at).total_seconds())} (last seen {d.last_seen_at:%Y-%m-%d %H:%M} UTC).")]
    rep = json.loads(d.last_report_json) if d.last_report_json else {}
    sys = rep.get("system") or {}
    out = []
    disks = sys.get("disks") or [{"mount": "Disk", "percent": sys.get("disk_percent") or 0,
                                  "used_bytes": sys.get("disk_used_bytes"), "total_bytes": sys.get("disk_total_bytes")}]
    for dk in disks:
        if (dk.get("percent") or 0) >= settings.alert_disk_percent > 0:
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
    for c in rep.get("docker_containers") or []:
        if c.get("status") != "running":
            out.append(_p(f"container:{c['name']}", "warning", "Container", f"{c['name']} {c.get('status') or 'stopped'}", c.get("image") or ""))
    for c in checks:
        out += check_problems(c, now)
    return out


def sync(session: Session, now: datetime | None = None) -> list[Problem]:
    """Open/refresh/resolve problems. Returns the newly opened ones."""
    now = now or datetime.utcnow()
    devices = {d.id: d for d in session.exec(select(Device)).all()}
    checks = session.exec(select(ServiceCheck)).all()
    open_ = {(p.device_id, p.key): p for p in session.exec(select(Problem).where(Problem.resolved_at == None)).all()}  # noqa: E711
    seen, new = set(), []
    for d in devices.values():
        for f in detect(d, [c for c in checks if c.device_id == d.id], now):
            k = (d.id, f["key"])
            seen.add(k)
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
        if p.source in ("Heartbeat", "Certificate") and not d.alerts_muted(now):
            notifier.send(f"{d.name}: {p.title}. {p.detail}", title=p.source,
                          priority="high" if p.severity == "high" else "default", tags="warning")
    return new


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


def digest(session: Session, now: datetime) -> str:
    devices = {d.id: d.name for d in session.exec(select(Device)).all()}
    open_ = session.exec(select(Problem).where(Problem.resolved_at == None)).all()  # noqa: E711
    fixed = session.exec(select(Problem).where(Problem.resolved_at >= now - timedelta(hours=24))).all()
    lines = [f"{len(open_)} open · {len(fixed)} fixed in the last 24h"]
    for sev, label in SEVERITIES:
        items = [f"{devices.get(p.device_id, '?')} {p.title}" for p in open_ if p.severity == sev]
        if items:
            lines.append(f"{label}: " + "; ".join(items[:6]) + (f" (+{len(items) - 6})" if len(items) > 6 else ""))
    if fixed:
        lines.append("Fixed: " + "; ".join(f"{devices.get(p.device_id, '?')} {p.title}" for p in fixed[:6]))
    return "\n".join(lines)


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
        local = datetime.now()
        stamp = settings.data_dir() / "last_digest.txt"
        if settings.digest_hour == local.hour and _read(stamp) != local.date().isoformat():
            stamp.write_text(local.date().isoformat(), encoding="utf-8")
            notifier.send(digest(session, now), title="Daily summary", tags="clipboard")


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
    d.is_online = False
    assert [p["key"] for p in detect(d, [hb], now)] == ["offline"]
    assert detect(Device(id=2, name="new"), [], now) == []
    ratios, overall = uptime([CheckDay(check_id=1, day="2026-11-03", ok=9, total=10)], date(2026, 11, 3), 3)
    assert ratios == [None, None, 0.9] and overall == 0.9
    print("ok")
