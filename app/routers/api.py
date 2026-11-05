import ipaddress
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from sqlmodel import Session

from .. import apt_check, notifier, ssh_keys
from ..config import settings
from ..database import get_session
from ..models import AlertEvent, ContainerHistory, Device, ReportHistory
from ..schemas import ClientReport
from ..security import get_device_from_api_key

router = APIRouter(prefix="/api/v1", tags=["client"])


def _is_running(status) -> bool:
    return "running" in (status or "").lower()


def _notify(device: Device, message: str, key: str | None = None, **kw) -> None:
    """ntfy push unless the device is in a mute/maintenance window."""
    if not device.alerts_muted():
        notifier.send(message, device_id=device.id, key=key, **kw)


_TAILNET = ipaddress.ip_network("100.64.0.0/10")


def is_outside(ip: str | None) -> bool:
    """An address from outside the LAN, the tailnet and loopback."""
    try:
        a = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_link_local or a in _TAILNET)


def track_restarts(previous: str | None, containers, now: float) -> str | None:
    """Device.container_restarts: each container's restart_count over the last
    30 minutes, [[epoch, count], ...], for spotting restart loops."""
    from ..problems import RESTART_WINDOW
    old = json.loads(previous) if previous else {}
    out = {}
    for c in containers:
        if c.restart_count is None:
            continue
        pts = [p for p in old.get(c.name, []) if now - p[0] <= RESTART_WINDOW] + [[int(now), c.restart_count]]
        out[c.name] = pts[-40:]
    return json.dumps(out) if out else None


def ssh_alerts(device: Device, auth, state: dict) -> list[tuple[str, dict]]:
    """Updates alert_state for SSH and returns the pushes to send:
    a login from outside (once per login), and a burst of failed logins (once,
    until it calms down)."""
    out = []
    if auth is None:
        return out
    if settings.ssh_outside_login_alert and auth.last_accepted_ip and is_outside(auth.last_accepted_ip):
        sig = f"{auth.last_accepted_ip}|{auth.last_accepted_at}"
        if state.get("ssh_ok") != sig:
            state["ssh_ok"] = sig
            out.append((f"{device.name}: SSH login as {auth.last_accepted_user or '?'} from {auth.last_accepted_ip}, "
                        "an address outside your networks. If that wasn't you, act now.",
                        {"title": "SSH login from outside", "priority": "high", "tags": "key,warning"}))
    limit = settings.ssh_failed_alert
    if limit > 0 and auth.recent_failed_count >= limit and not state.get("ssh_fail"):
        state["ssh_fail"] = True
        out.append((f"{device.name}: {auth.recent_failed_count} failed SSH logins recently, latest from {auth.last_failed_ip or '?'}.",
                    {"title": "SSH login failures", "priority": "default", "tags": "key", "key": "ssh_fail"}))
    elif state.get("ssh_fail") and auth.recent_failed_count < max(1, limit // 2):
        del state["ssh_fail"]
    return out


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

    # capture the previous containers before overwriting, to spot running->exited
    prev_report = json.loads(device.last_report_json) if device.last_report_json else {}
    prev_status = {c.get("name"): c.get("status") for c in (prev_report.get("docker_containers") or [])}

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
        if device.offline_quiet:
            # its going offline was folded into its host's push, or a "lost contact" one
            device.offline_quiet = False
        else:
            _notify(device, event.message, title="Device back online", priority="default", tags="white_check_mark")

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
                _notify(
                    device,
                    message,
                    key="tailscale",
                    title="Tailscale status change",
                    priority="high" if not new_state else "default",
                    tags="satellite",
                )
            device.tailscale_connected = new_state
        # else: undetermined reading — leave the last known state untouched.

    if report.apt is not None:
        needs_update, label = apt_check.evaluate(
            report.apt.model_dump(), settings.apt_stale_days, settings.apt_upgradable_threshold
        )
        # Only alert on the transition into "needs update" (avoids re-pinging
        # every report cycle). Clearing back is silent — running updates is good news.
        if device.apt_needs_update is not True and needs_update:
            message = f"{device.name}: packages need updating ({label})"
            session.add(AlertEvent(device_id=device.id, device_name=device.name, kind="apt_stale", message=message))
            # low priority: not urgent like offline/down — no sound/vibration so it
            # won't wake anyone; still lands in the feed to action in the morning.
            _notify(device, message, key="updates", title="Packages out of date", priority="low", tags="package,warning")
        device.apt_needs_update = needs_update

    # container running -> exited/dead transitions (only for names in both reports)
    for container in report.docker_containers:
        was = prev_status.get(container.name)
        if was and _is_running(was) and not _is_running(container.status):
            message = f"{device.name}: container {container.name} is {container.status}"
            session.add(AlertEvent(device_id=device.id, device_name=device.name, kind="container_down", message=message))
            _notify(device, message, key=f"container:{container.name}", title="Container stopped", priority="high", tags="whale,warning")

    device.container_restarts = track_restarts(device.container_restarts, report.docker_containers,
                                               now.replace(tzinfo=timezone.utc).timestamp())

    state = json.loads(device.alert_state) if device.alert_state else {}
    for message, kw in ssh_alerts(device, report.ssh_auth, state):
        session.add(AlertEvent(device_id=device.id, device_name=device.name, kind="ssh", message=message))
        _notify(device, message, **kw)
    device.alert_state = json.dumps(state) if state else None

    session.add(device)
    session.add(
        ReportHistory(
            device_id=device.id,
            cpu_percent=report.system.cpu_percent,
            mem_percent=report.system.mem_percent,
            disk_percent=report.system.disk_percent,
            power_w=report.power.wall_w if report.power else None,
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
