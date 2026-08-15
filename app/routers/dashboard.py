import json
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

AEST = ZoneInfo("Australia/Sydney")  # handles AEST/AEDT automatically

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, delete, select

from ..database import get_session
from ..models import AlertEvent, ContainerHistory, Device, ReportHistory, generate_api_key

from ..assets import STATIC_VERSION

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["static_version"] = STATIC_VERSION


def _fmt_uptime(seconds) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "—"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _to_datetime(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _fmt_dt(value) -> str:
    dt = _to_datetime(value)
    if dt is None:
        return "never" if value in (None, "") else str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # stored timestamps are UTC
    return dt.astimezone(AEST).strftime("%Y-%m-%d %H:%M %Z")


def _fmt_reltime(value) -> str:
    """Accepts either an ISO/datetime timestamp or a raw seconds-ago float."""
    if isinstance(value, (int, float)):
        seconds = value
    else:
        dt = _to_datetime(value)
        if dt is None:
            return "never"
        seconds = (datetime.utcnow() - dt).total_seconds()
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m ago"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h ago"


templates.env.filters["uptime"] = _fmt_uptime
templates.env.filters["dt"] = _fmt_dt
templates.env.filters["reltime"] = _fmt_reltime


def serialize_device(device: Device) -> dict:
    report = json.loads(device.last_report_json) if device.last_report_json else None
    seconds_since_seen: Optional[float] = None
    if device.last_seen_at:
        seconds_since_seen = (datetime.utcnow() - device.last_seen_at).total_seconds()

    return {
        "id": device.id,
        "name": device.name,
        "notes": device.notes,
        "is_online": device.is_online,
        "last_seen_at": device.last_seen_at.isoformat() if device.last_seen_at else None,
        "seconds_since_seen": seconds_since_seen,
        "report_interval_seconds": device.report_interval_seconds,
        "offline_after_seconds": device.offline_after_seconds,
        "ssh_enabled": device.ssh_enabled,
        "apt_needs_update": bool(device.apt_needs_update),
        "report": report,
    }


@router.get("/", response_class=HTMLResponse)
def index(request: Request, session: Session = Depends(get_session)):
    devices = session.exec(select(Device).order_by(Device.name)).all()
    serialized = [serialize_device(d) for d in devices]
    apt_alert_count = sum(1 for d in serialized if d["apt_needs_update"])
    return templates.TemplateResponse(
        "index.html", {"request": request, "devices": serialized, "apt_alert_count": apt_alert_count}
    )


@router.get("/devices.json")
def devices_json(session: Session = Depends(get_session)):
    devices = session.exec(select(Device).order_by(Device.name)).all()
    return [serialize_device(d) for d in devices]


@router.post("/devices")
def create_device(
    name: str = Form(...),
    notes: str = Form(""),
    report_interval_seconds: int = Form(60),
    offline_after_seconds: int = Form(150),
    session: Session = Depends(get_session),
):
    device = Device(
        name=name,
        notes=notes,
        report_interval_seconds=report_interval_seconds,
        offline_after_seconds=offline_after_seconds,
    )
    session.add(device)
    session.commit()
    session.refresh(device)
    return RedirectResponse(url=f"/devices/{device.id}", status_code=303)


@router.get("/devices/{device_id}", response_class=HTMLResponse)
def device_detail(request: Request, device_id: int, session: Session = Depends(get_session)):
    device = session.get(Device, device_id)
    alerts = session.exec(
        select(AlertEvent).where(AlertEvent.device_id == device_id).order_by(AlertEvent.created_at.desc()).limit(20)
    ).all()
    apt_alert_count = len(session.exec(select(Device).where(Device.apt_needs_update == True)).all())  # noqa: E712
    return templates.TemplateResponse(
        "device_detail.html",
        {
            "request": request,
            "device": serialize_device(device),
            "api_key": device.api_key,
            "alerts": alerts,
            "apt_alert_count": apt_alert_count,
        },
    )


@router.get("/devices/{device_id}/history.json")
def device_history(device_id: int, session: Session = Depends(get_session)):
    rows = session.exec(
        select(ReportHistory)
        .where(ReportHistory.device_id == device_id)
        .order_by(ReportHistory.timestamp.desc())
        .limit(500)
    ).all()
    rows = list(reversed(rows))  # newest 500, back in chronological order
    return [
        {
            "timestamp": r.timestamp.isoformat(),
            "cpu_percent": r.cpu_percent,
            "mem_percent": r.mem_percent,
            "disk_percent": r.disk_percent,
        }
        for r in rows
    ]


@router.get("/devices/{device_id}/containers/history.json")
def container_history(device_id: int, session: Session = Depends(get_session)):
    # Most-recent rows first, grouped per container, capped to keep the
    # background sparklines light. We over-fetch then trim per container.
    points_per_container = 60
    rows = session.exec(
        select(ContainerHistory)
        .where(ContainerHistory.device_id == device_id)
        .order_by(ContainerHistory.timestamp.desc())
        .limit(2000)
    ).all()

    grouped: dict[str, list] = {}
    for r in rows:
        series = grouped.setdefault(r.container_name, [])
        if len(series) >= points_per_container:
            continue
        series.append(
            {
                "timestamp": r.timestamp.isoformat(),
                "cpu_percent": r.cpu_percent,
                "mem_percent": r.mem_percent,
            }
        )

    # rows came newest-first; reverse each series to chronological order
    return {name: list(reversed(series)) for name, series in grouped.items()}


def ssh_candidates(device: Device) -> list[str]:
    """Ordered SSH targets to try: Tailscale first, then local addresses.
    The terminal attempts each until one connects.
      1. explicit override (if set) — used alone
      2. Tailscale IPs (100.x…), then the MagicDNS name
      3. the device's reported LAN IP
      4. the address its last report came from
    """
    if device.ssh_host:
        return [device.ssh_host]

    out: list[str] = []
    report = json.loads(device.last_report_json) if device.last_report_json else {}
    report = report or {}
    ts = report.get("tailscale") or {}
    out.extend(ts.get("ips") or [])
    if ts.get("dns_name"):
        out.append(ts["dns_name"])
    system = report.get("system") or {}
    if system.get("local_ip"):
        out.append(system["local_ip"])
    if device.last_report_ip:
        out.append(device.last_report_ip)

    seen = set()
    ordered = []
    for addr in out:
        if addr and addr not in seen:
            seen.add(addr)
            ordered.append(addr)
    return ordered


def derive_ssh_host(device: Device) -> Optional[str]:
    candidates = ssh_candidates(device)
    return candidates[0] if candidates else None


@router.get("/devices/{device_id}/ssh-users.json")
def ssh_users(device_id: int, session: Session = Depends(get_session)):
    device = session.get(Device, device_id)
    if device is None:
        return {"ssh_enabled": False, "users": [], "host": None}
    report = json.loads(device.last_report_json) if device.last_report_json else {}
    return {
        "ssh_enabled": device.ssh_enabled,
        "users": (report or {}).get("ssh_users", []),
        "host": derive_ssh_host(device),
        "port": device.ssh_port,
    }


@router.post("/devices/{device_id}/ssh")
def update_ssh(
    device_id: int,
    ssh_enabled: bool = Form(False),
    ssh_host: str = Form(""),
    ssh_port: int = Form(22),
    session: Session = Depends(get_session),
):
    device = session.get(Device, device_id)
    if device:
        device.ssh_enabled = ssh_enabled
        device.ssh_host = ssh_host.strip() or None
        device.ssh_port = ssh_port or 22
        session.add(device)
        session.commit()
    return RedirectResponse(url=f"/devices/{device_id}", status_code=303)


@router.post("/devices/{device_id}/rotate-key")
def rotate_key(device_id: int, session: Session = Depends(get_session)):
    device = session.get(Device, device_id)
    device.api_key = generate_api_key()
    session.add(device)
    session.commit()
    return RedirectResponse(url=f"/devices/{device_id}", status_code=303)


@router.post("/devices/{device_id}/delete")
def delete_device(device_id: int, session: Session = Depends(get_session)):
    session.exec(delete(ReportHistory).where(ReportHistory.device_id == device_id))
    session.exec(delete(AlertEvent).where(AlertEvent.device_id == device_id))
    device = session.get(Device, device_id)
    if device:
        session.delete(device)
    session.commit()
    return RedirectResponse(url="/", status_code=303)
