import asyncio
import json
import logging
import ssl
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx
from sqlmodel import Session, delete, select

from . import dnscheck, notifier, pbs, problems, proxmox, topology
from .config import settings
from .database import engine
from .models import AlertEvent, ContainerHistory, Device, HourlyHistory, ReportHistory, ServiceCheck

# checks that report a list of findings, each its own problem (pushed by problems.sync)
ISSUE_KINDS = ("pbs", "proxmox")

logger = logging.getLogger("devicemanager.monitor")

# (state key, ReportHistory column, human label) for the sustained-threshold check.
_METRICS = [("disk", "disk_percent", "Disk"), ("mem", "mem_percent", "Memory"), ("cpu", "cpu_percent", "CPU")]


def _notify(device: Device, message: str, key: str | None = None, **kw) -> None:
    """ntfy push unless the device is in a mute/maintenance window. key: the
    problem it's about, for the notification's Acknowledge button."""
    if device.alerts_muted():
        logger.info("alert suppressed (muted): %s", message)
        return
    notifier.send(message, device_id=device.id, key=key, **kw)


def _storm_path():
    return settings.data_dir() / "storm.json"


def check_offline_devices(session: Session, now: datetime | None = None) -> list[str]:
    """Mark devices that stopped reporting offline, and push about it, folded:
    - when most of the fleet (and at least STORM_MIN_DEVICES) goes quiet in one
      sweep, it's Pulse's own network: one push, not one per device;
    - a guest whose host is offline too is named in the host's push, not its own.
    Devices whose push was held back recover quietly (offline_quiet).
    Returns the messages pushed (for tests)."""
    now = now or datetime.utcnow()
    devices = session.exec(select(Device)).all()
    online_before = [d for d in devices if d.is_online and d.last_seen_at is not None]
    went = [d for d in online_before if now > d.last_seen_at + timedelta(seconds=d.offline_after_seconds)]
    if not went:
        return []
    parent_of = topology.parents(devices)
    by_id = {d.id: d for d in devices}
    for d in went:
        d.is_online = False
        last_seen = d.last_seen_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        msg = f"{d.name} stopped responding (last seen {last_seen})"
        session.add(AlertEvent(device_id=d.id, device_name=d.name, kind="offline", message=msg))
        logger.warning(msg)
    sent = []
    loud = [d for d in went if not d.alerts_muted(now)]
    # count machines, not guests: a host going down with its LXCs is one outage
    went_ids = {d.id for d in went}
    roots_went = [d for d in went if parent_of.get(d.id) not in went_ids]
    roots_before = [d for d in online_before if parent_of.get(d.id) not in {o.id for o in online_before}]
    storm = len(roots_went) >= max(2, settings.storm_min_devices) and len(roots_went) >= 0.6 * len(roots_before)
    if storm:
        for d in went:
            d.offline_quiet = True
        names = ", ".join(sorted(d.name for d in went))
        msg = (f"Pulse lost contact with {len(went)} of {len(online_before)} devices at once: {names}. "
               f"That's most likely Pulse's own network (mac-deb's link, a switch, DNS), not {len(went)} separate outages.")
        _storm_path().write_text(json.dumps({"ids": [d.id for d in went], "at": now.isoformat()}), encoding="utf-8")
        if loud:
            notifier.send(msg, title="Lost contact", priority="high", tags="warning,satellite", click="/")
            sent.append(msg)
    else:
        down = {d.id for d in devices if not d.is_online}
        for d in went:
            # host offline too (now or earlier): this one's news goes with the host's
            if parent_of.get(d.id) in down:
                d.offline_quiet = True
        for d in went:
            if d.offline_quiet:
                continue
            guests = sorted(g.name for g in went if parent_of.get(g.id) == d.id)
            msg = f"{d.name} stopped responding (last seen {d.last_seen_at:%Y-%m-%d %H:%M:%S} UTC)"
            if guests:
                msg += f". Its guests are unreachable too: {', '.join(guests)}"
            if not d.alerts_muted(now):
                _notify(d, msg, key="offline", title="Device offline", priority="high", tags="warning,skull")
                sent.append(msg)
        for d in went:
            if d.offline_quiet and parent_of.get(d.id) not in went_ids:
                logger.info("%s offline while its host %s is offline: no push", d.name, by_id[parent_of[d.id]].name)
    for d in went:
        session.add(d)
    session.commit()
    return sent


def check_storm_over(session: Session) -> str | None:
    """After a "lost contact" push: one push when every device in it reports again."""
    path = _storm_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    devices = [session.get(Device, i) for i in data.get("ids") or []]
    if any(d is not None and not d.is_online for d in devices):
        return None
    path.unlink(missing_ok=True)
    n = sum(1 for d in devices if d is not None)
    msg = f"Contact restored: all {n} devices are reporting again."
    notifier.send(msg, title="Back in contact", priority="default", tags="white_check_mark", click="/")
    return msg


def _sustained_over(values: list[float], threshold: float) -> bool:
    """True only if we have a couple of readings and EVERY one is at/above the
    threshold — a single spike within the window won't trip it."""
    return len(values) >= 2 and min(values) >= threshold


def check_thresholds(session: Session) -> None:
    now = datetime.utcnow()
    window_start = now - timedelta(minutes=settings.alert_sustained_minutes)
    for device in session.exec(select(Device)).all():
        if not device.is_online:
            continue
        state = json.loads(device.alert_state) if device.alert_state else {}
        rows = session.exec(
            select(ReportHistory)
            .where(ReportHistory.device_id == device.id, ReportHistory.timestamp >= window_start)
            .order_by(ReportHistory.timestamp)
        ).all()
        changed = False
        for key, col, label in _METRICS:
            threshold = device.threshold(key)
            values = [getattr(r, col) for r in rows]
            over = threshold > 0 and _sustained_over(values, threshold)
            if over and key not in state:
                state[key] = True
                changed = True
                msg = f"{device.name}: {label} at {values[-1]:.0f}% (over {threshold}% for {settings.alert_sustained_minutes}m)"
                session.add(AlertEvent(device_id=device.id, device_name=device.name, kind=f"{key}_high", message=msg))
                _notify(device, msg, key=key if key != "disk" else None, title=f"{label} high", priority="high", tags="chart_with_upwards_trend,warning")
            elif not over and key in state:
                del state[key]  # recovered — clear silently, like apt
                changed = True
        if changed:
            device.alert_state = json.dumps(state) if state else None
            session.add(device)
    session.commit()


async def _run_check(kind: str, target: str, secret: str = "", expect: str = ""):
    """Returns (status, latency_ms, error, summary, issues). status is 'up' or
    'down'; summary and issues are only for pbs/proxmox checks."""
    loop = asyncio.get_event_loop()
    start = loop.time()
    ms = lambda: round((loop.time() - start) * 1000, 1)  # noqa: E731
    if kind == "pbs":
        try:
            ok, summary, error, issues = await pbs.check(target, secret, max_age_hours=settings.pbs_backup_max_age_hours,
                                                         retired_days=settings.pbs_retired_days, full_percent=settings.alert_disk_percent)
            return ("up" if ok else "down"), ms(), error, summary, issues
        except pbs.PbsError as exc:
            return "down", None, str(exc), "", []
    if kind == "proxmox":
        try:
            ok, summary, error, issues = await proxmox.check(target, secret, full_percent=settings.alert_disk_percent)
            return ("up" if ok else "down"), ms(), error, summary, issues
        except proxmox.PveError as exc:
            return "down", None, str(exc), "", []
    if kind == "dns":
        up, error = await dnscheck.check(target, expect)
        return ("up" if up else "down"), (ms() if up else None), error, None, None
    try:
        if kind == "tcp":
            host, _, port = target.rpartition(":")
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout=8)
            writer.close()
            return "up", ms(), None, None, None
        url = target if "://" in target else "http://" + target
        # verify=False: homelab services often use self-signed certs; we're
        # checking reachability, not trust. ponytail comment: tighten if needed.
        async with httpx.AsyncClient(timeout=8, follow_redirects=True, verify=False) as client:
            r = await client.get(url)
        up, error = http_verdict(r.status_code, r.text, expect)
        return ("up" if up else "down"), ms(), error, None, None
    except Exception as exc:  # noqa: BLE001 — any failure means "down", record why
        return "down", None, str(exc)[:200], None, None


def http_verdict(status: int, body: str, expect: str = "") -> tuple[bool, str | None]:
    """expect: empty = any status under 400; a 3-digit number = that status;
    anything else = text the page must contain (an error page served with a 200
    otherwise passes)."""
    expect = (expect or "").strip()
    if expect.isdigit() and len(expect) == 3:
        return (True, None) if status == int(expect) else (False, f"HTTP {status}, expected {expect}")
    if status >= 400:
        return False, f"HTTP {status}"
    if expect and expect not in body:
        return False, f"HTTP {status}, but the page doesn't contain \"{expect[:60]}\""
    return True, None


async def _cert_expiry(url: str):
    """When an https URL's certificate expires (naive UTC), or None if unreadable.
    Read without verifying, like the check itself: self-signed still expires."""
    from cryptography import x509

    u = urlsplit(url)
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(u.hostname, u.port or 443, ssl=ctx, server_hostname=u.hostname), timeout=8)
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        writer.close()
        cert = x509.load_der_x509_certificate(der)
        end = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after.replace(tzinfo=timezone.utc)
        return end.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception as exc:  # noqa: BLE001 — no cert info is not a failure of the check
        logger.info("couldn't read the certificate of %s: %s", url, exc)
        return None


def check_heartbeats(session: Session) -> None:
    """Heartbeats are pushed (/ping/<token>); here they only go late. Every sweep
    also counts toward the day's uptime, so the bars show time on schedule."""
    now = datetime.utcnow()
    for c in session.exec(select(ServiceCheck).where(ServiceCheck.kind == "heartbeat", ServiceCheck.enabled == True)).all():  # noqa: E712
        if c.last_checked_at is None:
            continue  # waiting for its first ping
        late = now > c.last_checked_at + timedelta(seconds=c.interval_seconds + c.grace_seconds)
        c.last_status = "down" if late else "up"
        session.add(c)
        problems.record_check(session, c.id, not late, now)
    session.commit()


async def check_services() -> None:
    now = datetime.utcnow()
    with Session(engine) as session:
        checks = session.exec(select(ServiceCheck).where(ServiceCheck.enabled == True, ServiceCheck.kind != "heartbeat")).all()  # noqa: E712
        due = [
            (c.id, c.kind, c.target, c.secret, c.expect)
            for c in checks
            if c.last_checked_at is None or (now - c.last_checked_at).total_seconds() >= c.interval_seconds
        ]
        certs = [
            (c.id, c.target) for c in checks
            if c.kind == "http" and c.target.startswith("https://")
            and (c.cert_checked_at is None or (now - c.cert_checked_at).total_seconds() >= 6 * 3600)
        ]
    if certs:
        expiries = await asyncio.gather(*[_cert_expiry(t) for _, t in certs])
        with Session(engine) as session:
            for (cid, _), end in zip(certs, expiries):
                check = session.get(ServiceCheck, cid)
                if check:
                    check.cert_checked_at, check.cert_expires_at = now, end or check.cert_expires_at
                    session.add(check)
            session.commit()
    if not due:
        return
    results = await asyncio.gather(*[_run_check(*c[1:]) for c in due])
    with Session(engine) as session:
        for (cid, kind, *_), (status, latency, error, summary, issues) in zip(due, results):
            check = session.get(ServiceCheck, cid)
            if check is None:
                continue
            prev = check.last_status
            check.last_status = status
            check.last_checked_at = datetime.utcnow()
            check.last_latency_ms = latency
            check.last_error = error
            if summary is not None:
                check.summary = summary
            if kind in ISSUE_KINDS:
                check.issues = json.dumps(issues or [])
            session.add(check)
            problems.record_check(session, cid, status == "up", now)
            if kind in ISSUE_KINDS:
                continue  # each finding is pushed as its own problem by problems.sync
            if prev != status and status in ("up", "down"):
                device = session.get(Device, check.device_id)
                dname = device.name if device else "device"
                if status == "down":
                    msg = f"{dname}: {check.name} is DOWN ({error or check.target})"
                    session.add(AlertEvent(device_id=check.device_id, device_name=dname, kind="service_down", message=msg))
                    if device:
                        _notify(device, msg, key=f"check:{cid}", title="Service down", priority="high", tags="red_circle")
                elif prev == "down":  # up after being down = recovery
                    msg = f"{dname}: {check.name} recovered"
                    session.add(AlertEvent(device_id=check.device_id, device_name=dname, kind="service_up", message=msg))
                    if device:
                        _notify(device, msg, title="Service recovered", priority="default", tags="green_circle")
        session.commit()


def prune_history(session: Session) -> None:
    cutoff = datetime.utcnow() - timedelta(days=settings.history_retention_days)
    session.exec(delete(ReportHistory).where(ReportHistory.timestamp < cutoff))
    session.exec(delete(ContainerHistory).where(ContainerHistory.timestamp < cutoff))
    session.exec(delete(HourlyHistory).where(HourlyHistory.hour < datetime.utcnow() - timedelta(days=settings.long_history_days)))
    session.commit()


def rollup_hourly(session: Session, now: datetime | None = None) -> int:
    """Average every finished hour of ReportHistory into HourlyHistory, once.
    Returns the rows added."""
    now = now or datetime.utcnow()
    this_hour = now.replace(minute=0, second=0, microsecond=0)
    added = 0
    for d in session.exec(select(Device)).all():
        last = session.exec(select(HourlyHistory.hour).where(HourlyHistory.device_id == d.id)
                            .order_by(HourlyHistory.hour.desc()).limit(1)).first()
        start = last + timedelta(hours=1) if last else this_hour - timedelta(days=settings.history_retention_days)
        rows = session.exec(select(ReportHistory).where(ReportHistory.device_id == d.id, ReportHistory.timestamp >= start,
                                                        ReportHistory.timestamp < this_hour)).all()
        buckets: dict[datetime, list] = {}
        for r in rows:
            buckets.setdefault(r.timestamp.replace(minute=0, second=0, microsecond=0), []).append(r)
        for hour, rs in sorted(buckets.items()):
            pw = [r.power_w for r in rs if r.power_w is not None]
            session.add(HourlyHistory(device_id=d.id, hour=hour, samples=len(rs),
                                      cpu_percent=round(sum(r.cpu_percent for r in rs) / len(rs), 2),
                                      mem_percent=round(sum(r.mem_percent for r in rs) / len(rs), 2),
                                      disk_percent=max(r.disk_percent for r in rs),
                                      power_w=round(sum(pw) / len(pw), 2) if pw else None))
            added += 1
    session.commit()
    return added


_watchdog = {"at": 0.0, "ok": None, "error": ""}


async def ping_watchdog() -> None:
    """Tell an outside monitor Pulse is alive: after a healthy sweep, at most
    every WATCHDOG_INTERVAL_SECONDS, and not while the last ntfy push failed."""
    if not settings.watchdog_url:
        return
    loop = asyncio.get_event_loop()
    if loop.time() - _watchdog["at"] < settings.watchdog_interval_seconds:
        return
    _watchdog["at"] = loop.time()
    if await asyncio.to_thread(notifier.last_failed):
        _watchdog.update(ok=False, error="held back: the last ntfy push failed")
        logger.warning("watchdog ping held back: the last ntfy push failed")
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(settings.watchdog_url)
        _watchdog.update(ok=r.status_code < 400, error="" if r.status_code < 400 else f"HTTP {r.status_code}")
    except httpx.HTTPError as exc:
        _watchdog.update(ok=False, error=str(exc) or type(exc).__name__)
    _watchdog["last"] = datetime.utcnow()


def watchdog_status() -> dict:
    return {"configured": bool(settings.watchdog_url), **{k: v for k, v in _watchdog.items() if k != "at"}}


async def run_forever() -> None:
    while True:
        healthy = False
        try:
            with Session(engine) as session:
                check_offline_devices(session)
                check_storm_over(session)
                check_thresholds(session)
                check_heartbeats(session)
                prune_history(session)
                rollup_hourly(session)
            await check_services()
            await asyncio.to_thread(problems.run)
            healthy = True
        except Exception:
            logger.exception("monitor loop iteration failed")
        if healthy:
            try:
                await ping_watchdog()
            except Exception:  # noqa: BLE001
                logger.exception("watchdog ping failed")
        await asyncio.sleep(settings.check_interval_seconds)
