from datetime import datetime

from fastapi import APIRouter, Depends
from sqlmodel import Session

from .. import notifier
from ..database import get_session
from ..models import AlertEvent, Device, ReportHistory
from ..schemas import ClientReport
from ..security import get_device_from_api_key

router = APIRouter(prefix="/api/v1", tags=["client"])


@router.post("/report")
def submit_report(
    report: ClientReport,
    device: Device = Depends(get_device_from_api_key),
    session: Session = Depends(get_session),
):
    now = datetime.utcnow()
    was_online = device.is_online
    had_reported_before = device.last_seen_at is not None

    device.last_seen_at = now
    device.is_online = True
    device.last_report_json = report.model_dump_json()

    if had_reported_before and not was_online:
        event = AlertEvent(
            device_id=device.id,
            device_name=device.name,
            kind="online",
            message=f"{device.name} is responding again",
        )
        session.add(event)
        notifier.send(event.message, title="Device back online", priority="default", tags="white_check_mark")

    if report.tailscale is not None:
        new_state = report.tailscale.connected
        if device.tailscale_connected is not None and device.tailscale_connected != new_state:
            kind = "tailscale_up" if new_state else "tailscale_down"
            message = f"{device.name}: Tailscale {'connected' if new_state else 'disconnected'}"
            session.add(AlertEvent(device_id=device.id, device_name=device.name, kind=kind, message=message))
            notifier.send(
                message,
                title="Tailscale status change",
                priority="high" if not new_state else "default",
                tags="satellite",
            )
        device.tailscale_connected = new_state

    session.add(device)
    session.add(
        ReportHistory(
            device_id=device.id,
            cpu_percent=report.system.cpu_percent,
            mem_percent=report.system.mem_percent,
            disk_percent=report.system.disk_percent,
        )
    )
    session.commit()

    return {"status": "ok"}
