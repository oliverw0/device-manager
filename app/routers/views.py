"""Read-only overview pages: Status (checks by group, and a public version),
Timeline (what went wrong when), Sent (every push) and Map (what runs where)."""
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from .. import monitor, problems, topology
from ..config import settings
from ..database import get_session
from ..models import CheckDay, Device, NotificationLog, Problem, ServiceCheck
from .dashboard import AEST, serialize_device, templates

router = APIRouter()

KIND_GROUP = {"http": "Services", "tcp": "Services", "dns": "DNS", "heartbeat": "Jobs", "pbs": "Backups", "proxmox": "Proxmox", "arr": "Downloads"}
GROUP_ORDER = ["DNS", "Proxmox", "Backups", "Services", "Downloads", "Jobs"]


def check_group(c: ServiceCheck) -> str:
    return c.group or KIND_GROUP.get(c.kind, "Services")


def status_groups(session: Session, now: datetime) -> tuple[list[dict], dict]:
    """[{name, state, checks: [...]}] in a stable order, and overall counts."""
    devices = {d.id: d for d in session.exec(select(Device)).all()}
    days = session.exec(select(CheckDay).where(CheckDay.day >= (now - timedelta(days=30)).strftime("%Y-%m-%d"))).all()
    groups: dict[str, list] = {}
    for c in session.exec(select(ServiceCheck).order_by(ServiceCheck.name)).all():
        bars, overall = problems.uptime([r for r in days if r.check_id == c.id], now.date())
        issues = json.loads(c.issues) if c.issues else []
        groups.setdefault(check_group(c), []).append({
            "c": c, "device": devices.get(c.device_id), "bars": bars, "uptime": overall,
            "state": "paused" if not c.enabled else c.last_status,
            "issues": issues,
            "cert_days": int((c.cert_expires_at - now).total_seconds() // 86400) if c.cert_expires_at else None,
        })
    out = []
    for name in sorted(groups, key=lambda g: (GROUP_ORDER.index(g) if g in GROUP_ORDER else len(GROUP_ORDER), g.lower())):
        rows = groups[name]
        on = [r for r in rows if r["state"] != "paused"]
        down = sum(1 for r in on if r["state"] == "down")
        out.append({"name": name, "checks": rows, "down": down, "on": len(on),
                    "state": "down" if down == len(on) and on else "part" if down else "up" if on else "unknown"})
    on = sum(g["on"] for g in out)
    down = sum(g["down"] for g in out)
    return out, {"on": on, "down": down, "up": on - down}


@router.get("/status")
def status_page():
    """Checks live on the Health page now."""
    return RedirectResponse(url="/health#checks", status_code=301)


@router.get("/status/public", response_class=HTMLResponse)
def public_status(request: Request, session: Session = Depends(get_session)):
    """Outside the login (auth_middleware): names and uptime only."""
    if not settings.public_status:
        return HTMLResponse("Not found\n", status_code=404)
    groups, totals = status_groups(session, datetime.utcnow())
    return templates.TemplateResponse("status.html", {"request": request, "groups": groups, "totals": totals, "public": True})


# ---------- timeline ----------
SPANS = {"24h": (timedelta(hours=24), timedelta(hours=3), "%H:%M"),
         "7d": (timedelta(days=7), timedelta(days=1), "%a %d"),
         "30d": (timedelta(days=30), timedelta(days=5), "%d %b")}


def _local(utc: datetime) -> datetime:
    return utc.replace(tzinfo=timezone.utc).astimezone(AEST)


def lanes(rows: list[Problem], start: datetime, end: datetime) -> list[list[dict]]:
    """Pack one device's problems into as few rows as don't overlap.
    Each bar: left/width in percent of [start, end]."""
    span = (end - start).total_seconds()
    bars = []
    for p in sorted(rows, key=lambda p: p.opened_at):
        a, b = max(p.opened_at, start), min(p.resolved_at or end, end)
        if b < a:
            continue
        bars.append({"p": p, "a": a, "b": b, "left": (a - start).total_seconds() / span * 100,
                     "width": max(0.35, (b - a).total_seconds() / span * 100),
                     "lasted": problems.ago(((p.resolved_at or end) - p.opened_at).total_seconds())})
    packed: list[list[dict]] = []
    for bar in bars:
        for lane in packed:
            if lane[-1]["left"] + lane[-1]["width"] <= bar["left"]:
                lane.append(bar)
                break
        else:
            packed.append([bar])
    return packed[:6]


@router.get("/problems/timeline")
@router.get("/problems/sent")
def old_urls(request: Request):
    return RedirectResponse(url=request.url.path.replace("/problems/", "/health/", 1)
                            + (f"?{request.url.query}" if request.url.query else ""), status_code=301)


@router.get("/health/timeline", response_class=HTMLResponse)
def timeline(request: Request, span: str = "7d", session: Session = Depends(get_session)):
    span = span if span in SPANS else "7d"
    length, tick, fmt = SPANS[span]
    end = datetime.utcnow()
    start = end - length
    devices = {d.id: d for d in session.exec(select(Device)).all()}
    rows = session.exec(select(Problem).where((Problem.resolved_at == None) | (Problem.resolved_at >= start))  # noqa: E711
                        .where(Problem.opened_at <= end)).all()
    by_dev: dict[int, list] = {}
    for p in rows:
        if p.device_id in devices and p.key != "updates":  # pending updates would paint every lane
            by_dev.setdefault(p.device_id, []).append(p)
    lanes_out = [{"d": devices[i], "rows": lanes(ps, start, end), "count": len(ps)}
                 for i, ps in sorted(by_dev.items(), key=lambda kv: devices[kv[0]].name.lower())]
    # tick marks on local-time boundaries
    t = _local(start)
    t = t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    if tick >= timedelta(days=1):
        t = t.replace(hour=0) + timedelta(days=1)
    ticks = []
    while t < _local(end):
        utc = t.astimezone(timezone.utc).replace(tzinfo=None)
        if tick < timedelta(days=1) and t.hour % 3:
            t += timedelta(hours=1)
            continue
        ticks.append({"left": (utc - start).total_seconds() / length.total_seconds() * 100, "label": t.strftime(fmt)})
        t += tick
    incidents = sorted((p for ps in by_dev.values() for p in ps), key=lambda p: p.opened_at, reverse=True)[:150]
    return templates.TemplateResponse("timeline.html", {
        "request": request, "span": span, "spans": list(SPANS), "lanes": lanes_out, "ticks": ticks,
        "quiet": len(devices) - len(by_dev), "now_label": _local(end).strftime("%H:%M"),
        "incidents": [{"p": p, "d": devices[p.device_id], "when": _local(p.opened_at),
                       "lasted": problems.ago(((p.resolved_at or end) - p.opened_at).total_seconds())} for p in incidents],
    })


@router.get("/health/sent", response_class=HTMLResponse)
def sent(request: Request, session: Session = Depends(get_session)):
    now = datetime.utcnow()
    rows = session.exec(select(NotificationLog).order_by(NotificationLog.id.desc()).limit(200)).all()
    day = [r for r in rows if r.created_at >= now - timedelta(hours=24)]
    return templates.TemplateResponse("sent.html", {
        "request": request, "rows": rows, "ntfy_on": bool(settings.ntfy_url),
        "day_total": len(day), "day_failed": sum(1 for r in day if not r.ok),
        "watchdog": monitor.watchdog_status(), "public_url": settings.public_url,
    })


# ---------- map ----------
def _container_state(c: dict, online: bool) -> str:
    s = (c.get("status") or "").lower()
    if not online or "exited" in s or "dead" in s:
        return "bad"
    if "running" not in s or c.get("health") == "unhealthy":
        return "warn"
    return "ok"


@router.get("/map", response_class=HTMLResponse)
def map_page(request: Request, session: Session = Depends(get_session)):
    devices = sorted(session.exec(select(Device)).all(), key=lambda d: d.name.lower())
    parent_of = topology.parents(devices)
    open_ = session.exec(select(Problem).where(Problem.resolved_at == None, Problem.acked_at == None)).all()  # noqa: E711
    checks = session.exec(select(ServiceCheck).where(ServiceCheck.enabled == True)).all()  # noqa: E712
    nodes = {}
    for d in devices:
        s = serialize_device(d)
        rep = s["report"] or {}
        sys = rep.get("system") or {}
        mine = [p for p in open_ if p.device_id == d.id]
        dc = [c for c in checks if c.device_id == d.id]
        nodes[d.id] = {
            "d": s, "children": [],
            "cpu": sys.get("cpu_percent"), "mem": sys.get("mem_percent"),
            "problems": len(mine), "sev": "high" if any(p.severity == "high" for p in mine) else "warning" if mine else None,
            "containers": [{"name": c.get("name"), "state": _container_state(c, d.is_online), "status": c.get("status"),
                            "health": c.get("health")} for c in sorted(rep.get("docker_containers") or [], key=lambda c: c.get("name") or "")],
            "checks_up": sum(1 for c in dc if c.last_status == "up"), "checks": len(dc),
            "auto": d.parent_id is None and d.id in parent_of,
        }
    roots = []
    for d in devices:
        parent = parent_of.get(d.id)
        (nodes[parent]["children"] if parent in nodes else roots).append(nodes[d.id])
    # hosts with guests first, then standalone machines
    roots.sort(key=lambda n: (not n["children"], n["d"]["name"].lower()))
    return templates.TemplateResponse("map.html", {"request": request, "roots": roots})
