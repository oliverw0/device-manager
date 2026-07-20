import asyncio
import logging
from datetime import datetime, timedelta

from sqlmodel import Session, delete, select

from . import notifier
from .config import settings
from .database import engine
from .models import AlertEvent, Device, ReportHistory

logger = logging.getLogger("devicemanager.monitor")


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
            notifier.send(event.message, title="Device offline", priority="high", tags="warning,skull")
            logger.warning(event.message)


def prune_history(session: Session) -> None:
    cutoff = datetime.utcnow() - timedelta(days=settings.history_retention_days)
    session.exec(delete(ReportHistory).where(ReportHistory.timestamp < cutoff))
    session.commit()


async def run_forever() -> None:
    while True:
        try:
            with Session(engine) as session:
                check_offline_devices(session)
                prune_history(session)
        except Exception:
            logger.exception("monitor loop iteration failed")
        await asyncio.sleep(settings.check_interval_seconds)
