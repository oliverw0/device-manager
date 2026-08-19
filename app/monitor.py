import asyncio
import json
import logging
from datetime import datetime, timedelta

import httpx
from sqlmodel import Session, delete, select

from . import notifier
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


def _project_disk_full_days(points: list[tuple[float, float]]):
    """Least-squares fit of disk% over time -> days until 100%. points are
    (epoch_seconds, disk_percent) in any order. Returns None when there isn't
    enough data, the trend is flat/falling, or full is >180 days out (not worth
    showing). ponytail: linear fit — fine for a slow-filling disk, not bursty I/O."""
    n = len(points)
    if n < 5:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    slope_per_sec = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / denom
    per_day = slope_per_sec * 86400
    if per_day < 0.05:  # essentially flat; ignore measurement noise
        return None
    last = max(points, key=lambda p: p[0])[1]
    if last >= 100:
        return 0.0
    days = (100 - last) / per_day
    return days if days <= 180 else None


async def _run_check(kind: str, target: str):
    """Returns (status, latency_ms, error). status is 'up' or 'down'."""
    loop = asyncio.get_event_loop()
    start = loop.time()
    try:
        if kind == "tcp":
            host, _, port = target.rpartition(":")
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout=8)
            writer.close()
            return "up", round((loop.time() - start) * 1000, 1), None
        url = target if "://" in target else "http://" + target
        # verify=False: homelab services often use self-signed certs; we're
        # checking reachability, not trust. ponytail comment: tighten if needed.
        async with httpx.AsyncClient(timeout=8, follow_redirects=True, verify=False) as client:
            r = await client.get(url)
        up = r.status_code < 400
        return ("up" if up else "down"), round((loop.time() - start) * 1000, 1), (None if up else f"HTTP {r.status_code}")
    except Exception as exc:  # noqa: BLE001 — any failure means "down", record why
        return "down", None, str(exc)[:200]


async def check_services() -> None:
    now = datetime.utcnow()
    with Session(engine) as session:
        checks = session.exec(select(ServiceCheck).where(ServiceCheck.enabled == True)).all()  # noqa: E712
        due = [
            (c.id, c.kind, c.target)
            for c in checks
            if c.last_checked_at is None or (now - c.last_checked_at).total_seconds() >= c.interval_seconds
        ]
    if not due:
        return
    results = await asyncio.gather(*[_run_check(kind, target) for (_, kind, target) in due])
    with Session(engine) as session:
        for (cid, _, _), (status, latency, error) in zip(due, results):
            check = session.get(ServiceCheck, cid)
            if check is None:
                continue
            prev = check.last_status
            check.last_status = status
            check.last_checked_at = datetime.utcnow()
            check.last_latency_ms = latency
            check.last_error = error
            session.add(check)
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
                prune_history(session)
            await check_services()
        except Exception:
            logger.exception("monitor loop iteration failed")
        await asyncio.sleep(settings.check_interval_seconds)
