import json
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

AEST = ZoneInfo("Australia/Sydney")  # handles AEST/AEDT automatically

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, delete, select

from .. import remote
from ..database import get_session
from ..models import AlertEvent, ContainerHistory, Device, ReportHistory, ServiceCheck, generate_api_key
from ..monitor import _project_disk_full_days

from ..assets import STATIC_VERSION

router = APIRouter()

# "Mute always" is just a mute window that never ends; alerts_muted() needs no special case.
MUTED_FOREVER = datetime(9999, 1, 1)
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


# Distros with their own icon; anything else Linux gets Tux (see .os-* in style.css).
_DISTROS = ("ubuntu", "debian", "raspbian", "fedora", "arch")


def os_key(system: Optional[dict]) -> Optional[str]:
    if not system:
        return None
    os_name = system.get("os") or "linux"  # clients from before OS reporting are all Linux (see remote.device_os)
    if os_name != "linux":
        return {"windows": "windows", "darwin": "macos"}.get(os_name)
    version = (system.get("os_version") or "").lower()
    if "raspberry" in version or "raspbian" in version:
        return "raspbian"
    # Asahi before fedora/debian/arch: "Fedora Linux Asahi Remix 40", or any distro
    # on Apple Silicon ("Debian 13" with kernel 6.17.9+deb13-asahi). The kernel is
    # the reliable tell for Docker clients, which can't read the hardware model.
    kernel = (system.get("kernel") or "").lower()
    model = (system.get("hardware_model") or "").lower()
    if "asahi" in version or "asahi" in kernel or model.startswith("apple"):
        return "asahi"
    return next((d for d in _DISTROS if d in version), "linux")


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
        "os": remote.device_os(device),
        "apt_needs_update": bool(device.apt_needs_update),
        "alerts_muted": device.alerts_muted(),
        "alerts_muted_until": device.alerts_muted_until.isoformat() if device.alerts_muted_until else None,
        "alerts_muted_forever": device.alerts_muted_until == MUTED_FOREVER,
        "os_key": os_key(report and report.get("system")),
        "agent_version": report.get("agent_version") if report else None,
        "report": report,
    }


@router.get("/", response_class=HTMLResponse)
def index(request: Request, session: Session = Depends(get_session)):
    return _render_index(request, session)


def _fleet(session: Session) -> list[dict]:
    """Devices for the front page cards: serialized plus a short CPU sparkline."""
    out = []
    for d in session.exec(select(Device).order_by(Device.name)).all():
        s = serialize_device(d)
        rows = session.exec(
            select(ReportHistory.cpu_percent).where(ReportHistory.device_id == d.id)
            .order_by(ReportHistory.timestamp.desc()).limit(30)
        ).all()
        s["cpu_spark"] = list(reversed(rows))
        out.append(s)
    return out


def _render_index(request: Request, session: Session, add_error: str = "", add_name: str = ""):
    from .updates import latest_client_version  # updates imports this module

    serialized = _fleet(session)
    apt_alert_count = sum(1 for d in serialized if d["apt_needs_update"])
    latest = latest_client_version([d["agent_version"] for d in serialized if d["report"]])
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "devices": serialized, "apt_alert_count": apt_alert_count,
         "add_error": add_error, "add_name": add_name, "latest_client": latest},
        status_code=400 if add_error else 200,
    )


@router.get("/devices.json")
def devices_json(session: Session = Depends(get_session)):
    return _fleet(session)


def _apt_count(session: Session) -> int:
    return len(session.exec(select(Device).where(Device.apt_needs_update == True)).all())  # noqa: E712


def _all_containers(session: Session) -> list[dict]:
    """Flatten every device's last-reported containers, tagged with the device."""
    rows = []
    for d in session.exec(select(Device).order_by(Device.name)).all():
        report = json.loads(d.last_report_json) if d.last_report_json else {}
        for c in (report or {}).get("docker_containers", []):
            rows.append({
                "device_id": d.id, "device_name": d.name,
                "device_online": d.is_online, "device_ssh_enabled": d.ssh_enabled, **c,
            })
    return rows


@router.get("/containers", response_class=HTMLResponse)
def containers_page(request: Request, session: Session = Depends(get_session)):
    rows = _all_containers(session)
    running = sum(1 for c in rows if "running" in (c.get("status") or "").lower())
    return templates.TemplateResponse(
        "containers.html",
        {"request": request, "containers": rows, "running": running, "apt_alert_count": _apt_count(session)},
    )


@router.get("/stacks", response_class=HTMLResponse)
def stacks_page(request: Request, session: Session = Depends(get_session)):
    grouped: dict = {}
    for c in _all_containers(session):
        if not c.get("stack"):
            continue
        g = grouped.setdefault(
            (c["device_id"], c["stack"]),
            {
                "device_id": c["device_id"], "device_name": c["device_name"],
                "device_ssh_enabled": c["device_ssh_enabled"], "stack": c["stack"], "members": [],
            },
        )
        g["members"].append(c)
    stacks = sorted(grouped.values(), key=lambda s: (s["device_name"], s["stack"]))
    for s in stacks:
        s["total"] = len(s["members"])
        s["running"] = sum(1 for m in s["members"] if "running" in (m.get("status") or "").lower())
    return templates.TemplateResponse(
        "stacks.html", {"request": request, "stacks": stacks, "apt_alert_count": _apt_count(session)}
    )


@router.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request, session: Session = Depends(get_session)):
    alerts = session.exec(select(AlertEvent).order_by(AlertEvent.created_at.desc()).limit(200)).all()
    return templates.TemplateResponse(
        "alerts.html", {"request": request, "alerts": alerts, "apt_alert_count": _apt_count(session)}
    )


@router.post("/devices")
def create_device(
    request: Request,
    name: str = Form(...),
    notes: str = Form(""),
    platform: str = Form("docker"),
    report_interval_seconds: int = Form(60),
    offline_after_seconds: int = Form(150),
    ssh_enabled: bool = Form(False),
    ssh_host: str = Form(""),
    ssh_port: int = Form(22),
    session: Session = Depends(get_session),
):
    name = name.strip()
    error = ""
    if not name:
        error = "Name is required."
    elif session.exec(select(Device).where(Device.name == name)).first():
        error = f"A device named '{name}' already exists."
    elif report_interval_seconds < 5 or offline_after_seconds <= report_interval_seconds:
        error = "Offline after must be longer than the report interval (min 5s)."
    if error:
        return _render_index(request, session, add_error=error, add_name=name)
    device = Device(
        name=name,
        notes=notes.strip(),
        report_interval_seconds=report_interval_seconds,
        offline_after_seconds=offline_after_seconds,
        ssh_enabled=ssh_enabled,
        ssh_host=ssh_host.strip() or None,
        ssh_port=ssh_port or 22,
    )
    session.add(device)
    session.commit()
    session.refresh(device)
    if platform not in ("docker", "native", "windows"):
        platform = "docker"
    return RedirectResponse(url=f"/devices/{device.id}?platform={platform}#settings", status_code=303)


@router.get("/devices/{device_id}", response_class=HTMLResponse)
def device_detail(request: Request, device_id: int, session: Session = Depends(get_session)):
    device = session.get(Device, device_id)
    alerts = session.exec(
        select(AlertEvent).where(AlertEvent.device_id == device_id).order_by(AlertEvent.created_at.desc()).limit(20)
    ).all()
    apt_alert_count = len(session.exec(select(Device).where(Device.apt_needs_update == True)).all())  # noqa: E712
    checks = session.exec(
        select(ServiceCheck).where(ServiceCheck.device_id == device_id).order_by(ServiceCheck.name)
    ).all()
    disk_rows = session.exec(
        select(ReportHistory).where(ReportHistory.device_id == device_id).order_by(ReportHistory.timestamp)
    ).all()
    disk_full_days = _project_disk_full_days([(r.timestamp.timestamp(), r.disk_percent) for r in disk_rows])
    from .updates import is_outdated, latest_client_version  # updates imports this module

    serialized = serialize_device(device)
    latest = latest_client_version([serialized["agent_version"]])
    return templates.TemplateResponse(
        "device_detail.html",
        {
            "request": request,
            "device": serialized,
            "latest_client": latest,
            "client_outdated": bool(device.last_report_json) and is_outdated(serialized["agent_version"], latest),
            "api_key": device.api_key,
            "alerts": alerts,
            "apt_alert_count": apt_alert_count,
            "checks": checks,
            "disk_full_days": disk_full_days,
        },
    )


@router.post("/devices/{device_id}/mute")
def mute_device(device_id: int, minutes: int = Form(...), session: Session = Depends(get_session)):
    """minutes > 0 mutes for that long, -1 mutes until unmuted, 0 unmutes."""
    device = session.get(Device, device_id)
    if device:
        if minutes < 0:
            device.alerts_muted_until = MUTED_FOREVER
        else:
            device.alerts_muted_until = datetime.utcnow() + timedelta(minutes=minutes) if minutes > 0 else None
        session.add(device)
        session.commit()
    return RedirectResponse(url=f"/devices/{device_id}", status_code=303)


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
    The terminal attempts each until one connects (and is the right machine,
    see remote.connect).
      1. explicit override (if set) — used alone
      2. Tailscale IPs (100.x…), then the MagicDNS name
      3. the address its last report came from — what the host actually sees
      4. the device's self-reported LAN IP (older Docker clients report their
         container's bridge IP here, which can belong to another machine)
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
    if device.last_report_ip:
        out.append(device.last_report_ip)
    system = report.get("system") or {}
    if system.get("local_ip"):
        out.append(system["local_ip"])

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
