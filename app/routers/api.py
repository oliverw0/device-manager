from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from sqlmodel import Session

from .. import notifier, ssh_keys
from ..database import get_session
from ..models import AlertEvent, ContainerHistory, Device, ReportHistory
from ..schemas import ClientReport
from ..security import get_device_from_api_key

router = APIRouter(prefix="/api/v1", tags=["client"])


@router.get("/healthz")
def healthz():
    # Unauthenticated liveness/connectivity check. The /api/v1 prefix is exempt
    # from the admin login middleware, so clients can probe reachability without
    # a valid API key.
    return {"status": "ok"}


@router.get("/ssh-pubkey", response_class=PlainTextResponse)
def ssh_pubkey():
    # Public keys are not secret; the client install script fetches this to add
    # the host's key to a chosen user's authorized_keys on the target machine.
    return ssh_keys.public_key_text()


@router.post("/report")
def submit_report(
    report: ClientReport,
    request: Request,
    device: Device = Depends(get_device_from_api_key),
    session: Session = Depends(get_session),
):
    now = datetime.utcnow()
    was_online = device.is_online
    had_reported_before = device.last_seen_at is not None

    device.last_seen_at = now
    device.is_online = True
    device.last_report_json = report.model_dump_json()

    # Remember where this report came from — used as a fallback SSH address when
    # the device reports no Tailscale IP. X-Forwarded-For (first hop) wins if a
    # proxy set it, otherwise the direct peer address.
    forwarded = request.headers.get("x-forwarded-for")
    client_ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else None)
    if client_ip:
        device.last_report_ip = client_ip

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
        # "connected" is False both when Tailscale is genuinely down AND when the
        # client couldn't read it (e.g. a momentary socket/CLI hiccup right after
        # the client relaunches). Only act on a DEFINITE reading — otherwise a
        # failed read would masquerade as a disconnect and fire a false alert.
        state = report.tailscale.backend_state
        determined = state not in (None, "not_installed", "timeout", "error", "unparseable", "unknown")
        if determined:
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
        # else: undetermined reading — leave the last known state untouched.

    session.add(device)
    session.add(
        ReportHistory(
            device_id=device.id,
            cpu_percent=report.system.cpu_percent,
            mem_percent=report.system.mem_percent,
            disk_percent=report.system.disk_percent,
        )
    )

    for container in report.docker_containers:
        if container.cpu_percent is None and container.mem_percent is None:
            continue
        session.add(
            ContainerHistory(
                device_id=device.id,
                container_name=container.name,
                cpu_percent=container.cpu_percent or 0.0,
                mem_percent=container.mem_percent or 0.0,
            )
        )

    session.commit()

    return {"status": "ok"}
