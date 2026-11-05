"""/metrics for Prometheus, and /api/v1/summary for a Homepage widget.

Both need METRICS_TOKEN, as `Authorization: Bearer <token>` (or ?token= for
clients that can't set headers). With no token set, both answer 404.
"""
import hmac
import json
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlmodel import Session, select

from ..config import settings
from ..database import get_session
from ..models import Device, Problem, ServiceCheck
from .views import check_group

router = APIRouter()


def _authorized(request: Request) -> bool:
    if not settings.metrics_token:
        return False
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or request.query_params.get("token", "")
    return hmac.compare_digest(given.encode(), settings.metrics_token.encode())


def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class Exposition:
    """Prometheus text format, one HELP/TYPE block per metric name."""

    def __init__(self):
        self.metrics: dict[str, tuple[str, str, list]] = {}

    def add(self, name: str, value, help_: str, kind: str = "gauge", **labels):
        if value is None:
            return
        self.metrics.setdefault(name, (help_, kind, []))[2].append((labels, float(value)))

    def render(self) -> str:
        out = []
        for name, (help_, kind, samples) in self.metrics.items():
            out += [f"# HELP {name} {help_}", f"# TYPE {name} {kind}"]
            for labels, value in samples:
                lab = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
                out.append(f"{name}{{{lab}}} {value:g}" if lab else f"{name} {value:g}")
        return "\n".join(out) + "\n"


def collect(session: Session, now: datetime | None = None) -> str:
    now = now or datetime.utcnow()
    m = Exposition()
    devices = {d.id: d for d in session.exec(select(Device)).all()}
    for d in devices.values():
        rep = json.loads(d.last_report_json) if d.last_report_json else {}
        sys = rep.get("system") or {}
        dev = {"device": d.name}
        m.add("pulse_device_up", d.is_online, "1 when the device is reporting.", **dev)
        if d.last_seen_at:
            m.add("pulse_device_last_seen_seconds", (now - d.last_seen_at).total_seconds(), "Seconds since the last report.", **dev)
        if not rep:
            continue
        m.add("pulse_cpu_percent", sys.get("cpu_percent"), "CPU use, percent.", **dev)
        m.add("pulse_memory_percent", sys.get("mem_percent"), "Memory use, percent.", **dev)
        m.add("pulse_memory_used_bytes", sys.get("mem_used_bytes"), "Memory used.", **dev)
        m.add("pulse_uptime_seconds", sys.get("uptime_seconds"), "Seconds since boot.", **dev)
        for dk in sys.get("disks") or []:
            lab = {**dev, "mount": dk.get("mount") or ""}
            m.add("pulse_disk_used_percent", dk.get("percent"), "Drive use, percent.", **lab)
            m.add("pulse_disk_used_bytes", dk.get("used_bytes"), "Drive bytes used.", **lab)
            m.add("pulse_disk_size_bytes", dk.get("total_bytes"), "Drive size.", **lab)
        apt = rep.get("apt") or {}
        m.add("pulse_updates_pending", apt.get("upgradable"), "OS updates waiting.", **dev)
        if rep.get("power"):
            m.add("pulse_power_watts", rep["power"].get("wall_w"), "Wall power, watts.", **dev)
        ts = rep.get("tailscale") or {}
        if ts and ts.get("backend_state") != "not_installed":
            m.add("pulse_tailscale_connected", bool(ts.get("connected")) and d.is_online, "1 when Tailscale is connected.", **dev)
        for c in rep.get("docker_containers") or []:
            lab = {**dev, "container": c.get("name") or "", "stack": c.get("stack") or ""}
            m.add("pulse_container_up", d.is_online and c.get("status") == "running", "1 when the container is running.", **lab)
            if c.get("health"):
                m.add("pulse_container_healthy", c["health"] == "healthy", "1 when its Docker healthcheck passes.", **lab)
            m.add("pulse_container_cpu_percent", c.get("cpu_percent"), "Container CPU, percent.", **lab)
            m.add("pulse_container_memory_percent", c.get("mem_percent"), "Container memory, percent of its limit.", **lab)
            m.add("pulse_container_restarts_total", c.get("restart_count"), "Restarts by the restart policy.", "counter", **lab)
    for c in session.exec(select(ServiceCheck).where(ServiceCheck.enabled == True)).all():  # noqa: E712
        lab = {"check": c.name, "kind": c.kind, "group": check_group(c),
               "device": devices[c.device_id].name if c.device_id in devices else ""}
        if c.last_status in ("up", "down"):
            m.add("pulse_check_up", c.last_status == "up", "1 when the check passes.", **lab)
        m.add("pulse_check_latency_ms", c.last_latency_ms if c.last_status == "up" else None, "Check response time, ms.", **lab)
        if c.cert_expires_at:
            m.add("pulse_cert_expiry_timestamp_seconds", (c.cert_expires_at - datetime(1970, 1, 1)).total_seconds(),
                  "When the https certificate expires (Unix time).", **lab)
    open_ = session.exec(select(Problem).where(Problem.resolved_at == None)).all()  # noqa: E711
    for sev in ("high", "warning"):
        for acked in (False, True):
            n = sum(1 for p in open_ if p.severity == sev and bool(p.acked_at) == acked)
            m.add("pulse_problems_open", n, "Open problems.", severity=sev, acknowledged=str(acked).lower())
    return m.render()


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(request: Request, session: Session = Depends(get_session)):
    if not _authorized(request):
        return PlainTextResponse("Not found\n", status_code=404)
    return PlainTextResponse(collect(session), media_type="text/plain; version=0.0.4")


@router.get("/api/v1/summary")
def summary(request: Request, session: Session = Depends(get_session)):
    """For Homepage's customapi widget: a handful of counts."""
    if not _authorized(request):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    devices = session.exec(select(Device)).all()
    checks = session.exec(select(ServiceCheck).where(ServiceCheck.enabled == True)).all()  # noqa: E712
    open_ = session.exec(select(Problem).where(Problem.resolved_at == None, Problem.acked_at == None)).all()  # noqa: E711
    return {
        "devices_online": sum(1 for d in devices if d.is_online), "devices_total": len(devices),
        "checks_up": sum(1 for c in checks if c.last_status == "up"), "checks_total": len(checks),
        "problems": len(open_), "problems_high": sum(1 for p in open_ if p.severity == "high"),
    }
