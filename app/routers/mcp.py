"""Pulse as an MCP server, for Willow and Claude Code sessions.

POST /mcp speaks MCP's streamable HTTP transport in its simplest form: one
JSON-RPC message in, one JSON answer out. No sessions, no server-sent events,
no batches. GET and DELETE answer 405, which tells clients there's no stream.

Needs MCP_TOKEN as `Authorization: Bearer <token>`; with no token set the
endpoint answers 404, like /metrics. Requests carrying an Origin header are
refused: browsers send one and MCP clients don't, so a web page can't drive
Pulse through someone's browser.

Most tools only read. Two change something and say so in their annotations:
pulse_retry_download (the Health page's Remove & search again) and
pulse_acknowledge.
"""
import hmac
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlmodel import Session, select

from .. import arr, downloads, problems
from ..config import settings
from ..database import engine
from ..models import Device, Problem, ServiceCheck
from .views import check_group

logger = logging.getLogger("devicemanager.mcp")
router = APIRouter()

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "pulse", "title": "Pulse", "version": "1.0.0"}
INSTRUCTIONS = (
    "Pulse monitors Oliver's home lab: every machine reports to it each minute, it runs service checks "
    "(web, TCP, DNS, backups, Proxmox, Sonarr/Radarr downloads) and raises problems. Start with pulse_status "
    "for 'is anything broken?'. Times are UTC. Problem ids from pulse_status / pulse_downloads are what "
    "pulse_retry_download and pulse_acknowledge take."
)


# --- helpers ---------------------------------------------------------------

def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


def _ago(dt: datetime | None, now: datetime) -> str | None:
    return problems.ago((now - dt).total_seconds()) + " ago" if dt else None


def _report(d: Device) -> dict:
    try:
        return json.loads(d.last_report_json) if d.last_report_json else {}
    except ValueError:
        return {}


def _disks(rep: dict) -> list[dict]:
    disks = (rep.get("system") or {}).get("disks") or []
    # older Docker clients list Docker's bind-mounted single files as drives
    return [dk for dk in disks if not (dk.get("mount") or "").startswith("/etc/")]


def _problem(p: Problem, devices: dict, now: datetime) -> dict:
    d = devices.get(p.device_id)
    return {"id": p.id, "severity": p.severity, "device": d.name if d else None, "source": p.source,
            "title": p.title, "detail": p.detail, "opened_at": _iso(p.opened_at), "open_for": _ago(p.opened_at, now),
            "acknowledged": bool(p.acked_at), "ack_note": p.ack_note or None,
            "is_download": bool(arr.split_key(p.key))}


def _open_problems(s: Session) -> list[Problem]:
    return s.exec(select(Problem).where(Problem.resolved_at == None).order_by(Problem.opened_at)).all()  # noqa: E711


def _device_brief(d: Device, now: datetime) -> dict:
    rep = _report(d)
    sys_ = rep.get("system") or {}
    disks = _disks(rep)
    containers = rep.get("docker_containers") or []
    fullest = max(disks, key=lambda dk: dk.get("percent") or 0, default=None)
    return {
        "name": d.name, "online": d.is_online, "last_seen": _ago(d.last_seen_at, now),
        "expected_off": d.offline_ok, "alerts_muted": d.alerts_muted(now),
        "cpu_percent": sys_.get("cpu_percent"), "memory_percent": sys_.get("mem_percent"),
        "fullest_disk": {"mount": fullest.get("mount"), "percent": fullest.get("percent")} if fullest else None,
        "updates_pending": (rep.get("apt") or {}).get("upgradable"),
        "containers_running": sum(1 for c in containers if c.get("status") == "running") if containers else None,
        "containers_total": len(containers) if containers else None,
    }


def _find_device(s: Session, name: str) -> Device | str:
    """Exact name (any case), else the one device whose name contains it."""
    want = (name or "").strip().lower()
    if not want:
        return "Give a device name (pulse_devices lists them)."
    devices = s.exec(select(Device)).all()
    for d in devices:
        if d.name.lower() == want:
            return d
    close = [d for d in devices if want in d.name.lower()]
    if len(close) == 1:
        return close[0]
    names = ", ".join(sorted(d.name for d in (close or devices)))
    return f"No single device matches '{name}'. {'Did you mean' if close else 'Devices are'}: {names}."


class ToolError(Exception):
    """A tool's answer to a bad request: reported to the model, not a protocol error."""


# --- tools -----------------------------------------------------------------

def t_status(s: Session, a: dict) -> dict:
    now = datetime.utcnow()
    devices = {d.id: d for d in s.exec(select(Device)).all()}
    checks = s.exec(select(ServiceCheck).where(ServiceCheck.enabled == True)).all()  # noqa: E712
    include_acked = bool(a.get("include_acknowledged"))
    open_ = [p for p in _open_problems(s) if p.device_id in devices and p.severity != "info"]
    shown = [p for p in open_ if include_acked or not p.acked_at]
    order = {"high": 0, "warning": 1}
    shown.sort(key=lambda p: (order.get(p.severity, 2), p.opened_at))
    offline = [d.name for d in devices.values() if not d.is_online and not d.offline_ok]
    down = [c.name for c in checks if c.last_status == "down"]
    new = [p for p in open_ if not p.acked_at]
    if not new and not offline and not down:
        summary = (f"All good: {sum(d.is_online for d in devices.values())} of {len(devices)} machines online, "
                   f"{sum(c.last_status == 'up' for c in checks)} of {len(checks)} checks passing, nothing new open.")
    else:
        high = sum(p.severity == "high" for p in new)
        bits = [f"{len(new)} open problem{'s' if len(new) != 1 else ''}" + (f" ({high} high)" if high else "")]
        if offline:
            bits.append("offline: " + ", ".join(sorted(offline)))
        if down:
            bits.append("checks failing: " + ", ".join(sorted(down)))
        summary = "; ".join(bits) + "."
    return {
        "summary": summary,
        "machines_online": sum(d.is_online for d in devices.values()), "machines_total": len(devices),
        "machines_offline": sorted(offline),
        "checks_passing": sum(c.last_status == "up" for c in checks), "checks_total": len(checks),
        "checks_failing": sorted(down),
        "acknowledged_hidden": 0 if include_acked else sum(1 for p in open_ if p.acked_at),
        "problems": [_problem(p, devices, now) for p in shown],
    }


def t_devices(s: Session, a: dict) -> dict:
    now = datetime.utcnow()
    devices = sorted(s.exec(select(Device)).all(), key=lambda d: d.name.lower())
    rows = [_device_brief(d, now) for d in devices]
    off = [r["name"] for r in rows if not r["online"]]
    return {"summary": f"{len(rows) - len(off)} of {len(rows)} online" + (f"; offline: {', '.join(off)}" if off else ""),
            "devices": rows}


def t_device(s: Session, a: dict) -> dict:
    d = _find_device(s, a.get("name", ""))
    if isinstance(d, str):
        raise ToolError(d)
    now = datetime.utcnow()
    rep = _report(d)
    sys_ = rep.get("system") or {}
    devices = {x.id: x for x in s.exec(select(Device)).all()}
    checks = s.exec(select(ServiceCheck).where(ServiceCheck.device_id == d.id)).all()
    return {
        **_device_brief(d, now),
        "notes": d.notes or None,
        "parent": devices[d.parent_id].name if d.parent_id in devices else None,
        "uptime": problems.ago(sys_["uptime_seconds"]) if sys_.get("uptime_seconds") else None,
        "disks": [{"mount": dk.get("mount"), "percent": dk.get("percent"),
                   "free_gb": round((dk["total_bytes"] - dk["used_bytes"]) / problems.GIB, 1)
                   if dk.get("total_bytes") and dk.get("used_bytes") is not None else None} for dk in _disks(rep)],
        "containers": [{"name": c.get("name"), "status": c.get("status"), "health": c.get("health"),
                        "stack": c.get("stack"), "restarts": c.get("restart_count")}
                       for c in rep.get("docker_containers") or []],
        "tailscale_connected": (rep.get("tailscale") or {}).get("connected"),
        "checks": [{"name": c.name, "kind": c.kind, "status": "paused" if not c.enabled else c.last_status,
                    "error": c.last_error if c.last_status == "down" else None} for c in checks],
        "open_problems": [_problem(p, devices, now) for p in _open_problems(s) if p.device_id == d.id],
    }


def t_checks(s: Session, a: dict) -> dict:
    which = a.get("status") or "failing"
    if which not in ("failing", "all"):
        raise ToolError("status is 'failing' or 'all'.")
    devices = {d.id: d.name for d in s.exec(select(Device)).all()}
    checks = s.exec(select(ServiceCheck).order_by(ServiceCheck.name)).all()
    rows = [{"name": c.name, "kind": c.kind, "group": check_group(c), "device": devices.get(c.device_id),
             "status": "paused" if not c.enabled else c.last_status,
             "latency_ms": round(c.last_latency_ms) if c.last_latency_ms is not None and c.last_status == "up" else None,
             "error": c.last_error if c.last_status == "down" else None,
             "summary": c.summary or None,
             "cert_expires": _iso(c.cert_expires_at)}
            for c in checks if which == "all" or (c.enabled and c.last_status == "down")]
    on = [c for c in checks if c.enabled]
    failing = sum(c.last_status == "down" for c in on)
    return {"summary": f"{len(on) - failing} of {len(on)} checks passing" + (f", {failing} failing" if failing else ""),
            "checks": rows}


def t_downloads(s: Session, a: dict) -> dict:
    now = datetime.utcnow()
    devices = {d.id: d for d in s.exec(select(Device)).all()}
    stuck = [p for p in _open_problems(s) if arr.split_key(p.key)]
    queues = [{"name": c.name, "status": c.last_status, "summary": c.summary or None,
               "error": c.last_error if c.last_status == "down" else None}
              for c in s.exec(select(ServiceCheck).where(ServiceCheck.kind == "arr", ServiceCheck.enabled == True)).all()]  # noqa: E712
    watches = downloads.recent()
    summary = (f"{len(stuck)} stuck download{'s' if len(stuck) != 1 else ''}" if stuck else "No stuck downloads")
    running = [w for w in watches if not w["finished"]]
    if running:
        summary += f"; {len(running)} re-download{'s' if len(running) != 1 else ''} in progress"
    return {"summary": summary + ".", "queues": queues, "stuck": [_problem(p, devices, now) for p in stuck],
            "redownloads": watches}


def t_daily_summary(s: Session, a: dict) -> dict:
    """The same text as the daily ntfy summary, for the last 24 hours. Doesn't
    move the storage snapshot, so tomorrow's real summary is unchanged."""
    try:
        before = json.loads((settings.data_dir() / "digest_disks.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        before = {}
    text, _ = problems.digest(s, datetime.utcnow(), before)
    return {"summary": text}


def t_retry_download(s: Session, a: dict) -> dict:
    p = s.get(Problem, a.get("problem_id"))
    if not p or not arr.split_key(p.key):
        raise ToolError("That isn't an open download problem (pulse_downloads lists them).")
    if p.resolved_at:
        raise ToolError("That download problem is already resolved.")
    answer, watches = problems.remove_downloads(s, [p.key], notify=True)
    return {"summary": answer + ". Pulse pushes to Oliver's phone when it ends.", "redownloads": [downloads.public(w) for w in watches]}


def t_acknowledge(s: Session, a: dict) -> dict:
    p = s.get(Problem, a.get("problem_id"))
    if not p or p.resolved_at:
        raise ToolError("No open problem with that id (pulse_status lists them).")
    if a.get("acknowledge", True):
        p.acked_at, p.ack_note = p.acked_at or datetime.utcnow(), str(a.get("note") or "").strip()[:200]
        what = f"Acknowledged: {p.title}. It stays on the Health page, out of pushes and the summary."
    else:
        p.acked_at, p.ack_note = None, ""
        what = f"Un-acknowledged: {p.title}."
    s.add(p)
    s.commit()
    return {"summary": what}


READ = {"readOnlyHint": True, "openWorldHint": False}
NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}
PROBLEM_ID = {"type": "integer", "description": "The problem's id, from pulse_status or pulse_downloads."}

TOOLS = {
    "pulse_status": (t_status, {
        "title": "Is anything broken?",
        "description": "Overall health of the lab: machines offline, checks failing and open problems, worst first. "
                       "Acknowledged problems are left out unless asked for.",
        "inputSchema": {"type": "object", "properties": {"include_acknowledged": {
            "type": "boolean", "description": "Also list problems Oliver has acknowledged.", "default": False}},
            "additionalProperties": False},
        "annotations": READ}),
    "pulse_devices": (t_devices, {
        "title": "Machines",
        "description": "Every machine Pulse watches: online, last seen, CPU, memory, fullest drive, OS updates waiting, containers.",
        "inputSchema": NO_ARGS, "annotations": READ}),
    "pulse_device": (t_device, {
        "title": "One machine",
        "description": "Everything about one machine: drives, every container, its checks and its open problems.",
        "inputSchema": {"type": "object", "properties": {"name": {
            "type": "string", "description": "Device name, e.g. 'mac-deb' or 'servarr' (any case; part of a name works if only one matches)."}},
            "required": ["name"], "additionalProperties": False},
        "annotations": READ}),
    "pulse_checks": (t_checks, {
        "title": "Service checks",
        "description": "Service checks (web, TCP, DNS, heartbeats, backups, Proxmox, downloads) with their status and error.",
        "inputSchema": {"type": "object", "properties": {"status": {
            "type": "string", "enum": ["failing", "all"], "default": "failing"}}, "additionalProperties": False},
        "annotations": READ}),
    "pulse_downloads": (t_downloads, {
        "title": "Downloads",
        "description": "Sonarr/Radarr: downloads stuck in the queue (fake or bad releases, failed imports) and how any "
                       "re-downloads Pulse started are going.",
        "inputSchema": NO_ARGS, "annotations": READ}),
    "pulse_daily_summary": (t_daily_summary, {
        "title": "Last 24 hours",
        "description": "The daily summary for the last 24 hours: backups, errors, downloads, outages, restarts, storage growth.",
        "inputSchema": NO_ARGS, "annotations": READ}),
    "pulse_retry_download": (t_retry_download, {
        "title": "Remove & search again",
        "description": "For a stuck download problem: remove it from the queue and the torrent client, blocklist the "
                       "release and search again. Pulse follows the search and pushes to Oliver's phone when it ends.",
        "inputSchema": {"type": "object", "properties": {"problem_id": PROBLEM_ID}, "required": ["problem_id"],
                        "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}}),
    "pulse_acknowledge": (t_acknowledge, {
        "title": "Acknowledge a problem",
        "description": "Mark an open problem as seen, so it stops pushing and leaves the daily summary (it stays on the "
                       "Health page until it clears). acknowledge=false undoes it.",
        "inputSchema": {"type": "object", "properties": {
            "problem_id": PROBLEM_ID,
            "acknowledge": {"type": "boolean", "default": True},
            "note": {"type": "string", "description": "Why, shown on the Health page.", "maxLength": 200}},
            "required": ["problem_id"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}}),
}


# --- JSON-RPC --------------------------------------------------------------

def _authorized(request: Request) -> bool:
    if not settings.mcp_token:
        return False
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    return hmac.compare_digest(given.encode(), settings.mcp_token.encode())


def _ok(mid, result) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": mid, "result": result})


def _err(mid, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}, status_code=status)


def _call(name: str, args: dict) -> dict:
    fn, _ = TOOLS[name]
    try:
        with Session(engine) as s:
            out = fn(s, args)
    except ToolError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except Exception:  # noqa: BLE001 — tell the model it failed; the log has the trace
        logger.exception("mcp tool %s", name)
        return {"content": [{"type": "text", "text": f"{name} failed inside Pulse; the details are in Pulse's log."}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=1)}], "structuredContent": out}


def handle(msg) -> JSONResponse | Response:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _err(None, -32600, "Invalid request (one JSON-RPC 2.0 message per POST; no batches)", 400)
    method, mid, params = msg["method"], msg.get("id"), msg.get("params") or {}
    if "id" not in msg:  # a notification (initialized, cancelled, ...): nothing to answer
        return Response(status_code=202)
    if not isinstance(params, dict):
        return _err(mid, -32602, "params must be an object")
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _ok(mid, {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                         "capabilities": {"tools": {"listChanged": False}},
                         "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS})
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        return _ok(mid, {"tools": [{"name": n, **spec} for n, (_, spec) in TOOLS.items()]})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in TOOLS:
            return _err(mid, -32602, f"Unknown tool: {name}")
        if not isinstance(args, dict):
            return _err(mid, -32602, "arguments must be an object")
        logger.info("mcp tool %s %s", name, json.dumps(args)[:200])
        return _ok(mid, _call(name, args))
    return _err(mid, -32601, f"Method not found: {method}")


@router.post("/mcp")
async def mcp_post(request: Request):
    if not _authorized(request):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    if request.headers.get("origin"):
        return JSONResponse({"detail": "Browser requests aren't allowed"}, status_code=403)
    try:
        msg = json.loads(await request.body())
    except ValueError:
        return _err(None, -32700, "Parse error", 400)
    # the tools use blocking SQLite (and remove_downloads blocking HTTP): keep them off the event loop
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(handle, msg)


@router.api_route("/mcp", methods=["GET", "DELETE"])
def mcp_no_stream(request: Request):
    if not _authorized(request):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return Response(status_code=405, headers={"Allow": "POST"})
