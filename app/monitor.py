import asyncio
import json
import logging
import ssl
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx
from sqlmodel import Session, delete, select

from . import notifier, pbs, problems
from .config import settings
from .database import engine
from .models import AlertEvent, ContainerHistory, Device, ReportHistory, ServiceCheck

logger = logging.getLogger("devicemanager.monitor")

# (state key, ReportHistory column, human label) for the sustained-threshold check.
_METRICS = [("disk", "disk_percent", "Disk"), ("mem", "mem_percent", "Memory"), ("cpu", "cpu_percent", "CPU")]


def _notify(device: Device, message: str, **kw) -> None:
    """ntfy push unless the device is in a mute/maintenance window."""
    if device.alerts_muted():
        logger.info("alert suppressed (muted): %s", message)
        return
    notifier.send(message, **kw)


def check_offline_devices(session: Session) -> None:
    now = datetime.utcnow()
    devices = session.exec(select(Device)).all()
    for device in devices:
        if device.last_seen_at is None or not device.is_online:
            continue
        deadline = device.last_seen_at + timedelta(seconds=device.offline_after_seconds)
        if now > deadline:
            device.is_online = False
            session.add(device)
            last_seen = device.last_seen_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            event = AlertEvent(
                device_id=device.id,
                device_name=device.name,
                kind="offline",
                message=f"{device.name} stopped responding (last seen {last_seen})",
            )
            session.add(event)
            session.commit()
            _notify(device, event.message, title="Device offline", priority="high", tags="warning,skull")
            logger.warning(event.message)


def _sustained_over(values: list[float], threshold: float) -> bool:
    """True only if we have a couple of readings and EVERY one is at/above the
    threshold — a single spike within the window won't trip it."""
    return len(values) >= 2 and min(values) >= threshold


def check_thresholds(session: Session) -> None:
    now = datetime.utcnow()
    window_start = now - timedelta(minutes=settings.alert_sustained_minutes)
    thresholds = {
        "cpu": settings.alert_cpu_percent,
        "mem": settings.alert_mem_percent,
        "disk": settings.alert_disk_percent,
    }
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
            threshold = thresholds[key]
            values = [getattr(r, col) for r in rows]
            over = threshold > 0 and _sustained_over(values, threshold)
            if over and key not in state:
                state[key] = True
                changed = True
                msg = f"{device.name}: {label} at {values[-1]:.0f}% (over {threshold}% for {settings.alert_sustained_minutes}m)"
                session.add(AlertEvent(device_id=device.id, device_name=device.name, kind=f"{key}_high", message=msg))
                _notify(device, msg, title=f"{label} high", priority="high", tags="chart_with_upwards_trend,warning")
            elif not over and key in state:
                del state[key]  # recovered — clear silently, like apt
                changed = True
        if changed:
            device.alert_state = json.dumps(state) if state else None
            session.add(device)
    session.commit()


async def _run_check(kind: str, target: str, secret: str = ""):
    """Returns (status, latency_ms, error, summary). status is 'up' or 'down';
    summary is only for pbs checks."""
    loop = asyncio.get_event_loop()
    start = loop.time()
    if kind == "pbs":
        try:
            ok, summary, error = await pbs.check(target, secret)
            return ("up" if ok else "down"), round((loop.time() - start) * 1000, 1), error, summary
        except pbs.PbsError as exc:
            return "down", None, str(exc), ""
    try:
        if kind == "tcp":
            host, _, port = target.rpartition(":")
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout=8)
            writer.close()
            return "up", round((loop.time() - start) * 1000, 1), None, None
        url = target if "://" in target else "http://" + target
        # verify=False: homelab services often use self-signed certs; we're
        # checking reachability, not trust. ponytail comment: tighten if needed.
        async with httpx.AsyncClient(timeout=8, follow_redirects=True, verify=False) as client:
            r = await client.get(url)
        up = r.status_code < 400
        return ("up" if up else "down"), round((loop.time() - start) * 1000, 1), (None if up else f"HTTP {r.status_code}"), None
    except Exception as exc:  # noqa: BLE001 — any failure means "down", record why
        return "down", None, str(exc)[:200], None


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
            (c.id, c.kind, c.target, c.secret)
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
        for (cid, *_), (status, latency, error, summary) in zip(due, results):
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
            session.add(check)
            problems.record_check(session, cid, status == "up", now)
            if prev != status and status in ("up", "down"):
                device = session.get(Device, check.device_id)
                dname = device.name if device else "device"
                if status == "down":
                    msg = f"{dname}: {check.name} is DOWN ({error or check.target})"
                    session.add(AlertEvent(device_id=check.device_id, device_name=dname, kind="service_down", message=msg))
                    if device:
                        _notify(device, msg, title="Service down", priority="high", tags="red_circle")
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
    session.commit()


async def run_forever() -> None:
    while True:
        try:
            with Session(engine) as session:
                check_offline_devices(session)
                check_thresholds(session)
                check_heartbeats(session)
                prune_history(session)
            await check_services()
            await asyncio.to_thread(problems.run)
        except Exception:
            logger.exception("monitor loop iteration failed")
        await asyncio.sleep(settings.check_interval_seconds)
