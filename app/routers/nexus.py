"""NEXUS at /nexus/: the page and its API (the same calls NEXUS's own server answered, now answered by Pulse).

  GET  /nexus/api/overview           everything the page shows (every 30 s)
  GET  /nexus/api/brief              {"issues": n}: the top bar's count on the other pages
  GET  /nexus/api/search             devices, containers and stacks, for the search box
  GET  /nexus/api/net                traffic per guest from Glances on pve (every 5 s)
  GET  /nexus/api/nav                the menu's badges, as on Pulse's pages: devices needing updates, open problems (every 60 s)
  GET  /nexus/api/pve                pve's own CPU, memory, drives and power, and 24 h of it a minute at a time (every 60 s)
  GET  /nexus/api/img/plex?thumb=…   a poster from Plex, through Tautulli
  GET  /nexus/api/img/jf/<id>        a poster from Jellyfin
  GET  /nexus/api/pending/<id>       follow a slow Willow answer
  POST /nexus/api/act                {"do", "id"}: approve/decline, done, retry/ack, pass, seerr-approve/decline
  POST /nexus/api/chat               {"text"}: ask Willow

Behind Pulse's own sign-in (auth_middleware). POSTs also need the header X-Nexus: 1, which a page on another site
can't send without a CORS preflight. NEXUS_DEMO=true serves made-up data (for working on the look)."""
import json
import re
import threading
import time
from pathlib import Path

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from sqlmodel import Session, select

from ..config import settings
from ..database import get_session
from ..models import Device, ReportHistory
from ..nexus import core

router = APIRouter()
STATIC = (Path(__file__).resolve().parent.parent / "nexus" / "static").resolve()
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml",
         ".png": "image/png"}


def _fail(e: Exception, status: int = 502) -> JSONResponse:
    return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=status, headers={"Cache-Control": "no-store"})


def _json(obj) -> JSONResponse:
    return JSONResponse(obj, headers={"Cache-Control": "no-store"})


@router.on_event("startup")
def _warm_at_start() -> None:
    """Build the board's data once as Pulse starts, so the first page after a restart doesn't wait ~4 s for it."""
    def go():
        try:
            v = core.build(core.demo_sources() if settings.nexus_demo else core.fetch_all())
            with _lock:
                _cache.update(v=v, at=time.time())
        except Exception:  # noqa: BLE001  the first request builds it instead
            pass
    threading.Thread(target=go, daemon=True, name="nexus-first").start()


@router.get("/nexus")
def nexus_root():
    return RedirectResponse(url="/nexus/", status_code=302)


_cache = {"at": 0.0, "v": None, "used": 0.0}
_lock = threading.Lock()
_warm = {"on": False}
WARM_EVERY_S = 20          # while a page is open, the next answer is always ready
WARM_IDLE_S = 600          # nobody's looked for ten minutes: stop asking the sources


def _warmer() -> None:
    while time.time() - _cache["used"] < WARM_IDLE_S:
        time.sleep(WARM_EVERY_S)
        try:
            v = core.build(core.demo_sources() if settings.nexus_demo else core.fetch_all())
            with _lock:
                _cache.update(v=v, at=time.time())
        except Exception:  # noqa: BLE001  the next request builds it instead
            pass
    _warm["on"] = False


def _overview(max_age: float) -> dict:
    """build() asks ten sources, so one result serves every open page. A background thread rebuilds it every
    20 s while anyone's using NEXUS, so a page never waits on a slow source (Seerr's first answer takes ~2 s)."""
    _cache["used"] = time.time()
    if not _warm["on"]:
        _warm["on"] = True
        threading.Thread(target=_warmer, daemon=True, name="nexus-warm").start()
    with _lock:
        if _cache["v"] is None or time.time() - _cache["at"] > max_age:
            _cache["v"] = core.build(core.demo_sources() if settings.nexus_demo else core.fetch_all())
            _cache["at"] = time.time()
        return _cache["v"]


@router.get("/nexus/api/overview")
def overview():
    try:
        return _json(_overview(30))
    except Exception as e:  # noqa: BLE001
        return _fail(e)


@router.get("/nexus/api/brief")
def brief():
    """What the NEXUS top bar counts as issues (approvals and warnings), for the top bar on Pulse's other pages."""
    try:
        v = _overview(60)
        return _json({"issues": len(v.get("needs") or []) + len(v.get("warnings") or [])})
    except Exception as e:  # noqa: BLE001
        return _fail(e)


@router.get("/nexus/api/net")
def net():
    try:
        return _json(core.demo_sources()["net"] if settings.nexus_demo else core.net())
    except Exception as e:  # noqa: BLE001
        return _fail(e)


PVE_DEVICE = "proxmox-host"  # pve's name in Pulse
MIN_DRIVE = 5 * 10**9  # drives smaller than this (the EFI partition) aren't worth a gauge


def pve_metal(session: Session, now: datetime | None = None) -> dict | None:
    """The board's Resources: pve's latest report (CPU, memory, every real drive, wall power) and its last 24 h of
    per-minute history as [unix seconds, cpu %, memory %, watts or None]. None when Pulse has no pve."""
    d = session.exec(select(Device).where(Device.name == PVE_DEVICE)).first()
    if not d:
        return None
    rep = json.loads(d.last_report_json) if d.last_report_json else {}
    sysi, power = rep.get("system") or {}, rep.get("power") or {}
    since = (now or datetime.utcnow()) - timedelta(hours=24)
    rows = session.exec(select(ReportHistory.timestamp, ReportHistory.cpu_percent, ReportHistory.mem_percent, ReportHistory.power_w)
                        .where(ReportHistory.device_id == d.id, ReportHistory.timestamp >= since).order_by(ReportHistory.timestamp)).all()
    epoch = lambda t: int((t - datetime(1970, 1, 1)).total_seconds())  # noqa: E731  stored as naive UTC
    return {
        "online": d.is_online, "seen": epoch(d.last_seen_at) if d.last_seen_at else None,
        "cpu": sysi.get("cpu_percent"), "cores": sysi.get("cpu_cores"), "uptime": sysi.get("uptime_seconds"),
        "mem": {"pct": sysi.get("mem_percent"), "used": sysi.get("mem_used_bytes"), "total": sysi.get("mem_total_bytes")},
        "drives": [{"mount": x["mount"], "pct": x["percent"], "used": x.get("used_bytes"), "total": x.get("total_bytes")}
                   for x in sysi.get("disks") or [] if (x.get("total_bytes") or 0) >= MIN_DRIVE],
        "watts": power.get("wall_w"),
        "history": [[epoch(t), round(c, 1), round(m, 1), None if w is None else round(w, 1)] for t, c, m, w in rows],
    }


@router.get("/nexus/api/pve")
def pve(session: Session = Depends(get_session)):
    try:
        v = pve_metal(session)
        return _json(v) if v else _fail(RuntimeError(f"Pulse has no {PVE_DEVICE}"), 404)
    except Exception as e:  # noqa: BLE001
        return _fail(e)


@router.get("/nexus/api/nav")
def nav(session: Session = Depends(get_session)):
    from .problems import open_count  # here, not at the top: problems imports the shared templates at load
    updates = len(session.exec(select(Device.id).where(Device.apt_needs_update == True)).all())  # noqa: E712
    return _json({"updates": updates, "problems": open_count()})


def _image(kind: str, ident: str) -> Response:
    if settings.nexus_demo:
        return Response(status_code=404)
    try:
        body, ctype = core.image(kind, ident)
    except Exception as e:  # noqa: BLE001
        return _fail(e)
    return Response(body, media_type=ctype if ctype.startswith("image/") else "image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})


@router.get("/nexus/api/img/plex")
def img_plex(thumb: str = ""):
    if not core.PLEX_THUMB.fullmatch(thumb):
        return Response(status_code=404)
    return _image("plex", thumb)


@router.get("/nexus/api/img/jf/{ident}")
def img_jf(ident: str):
    if not core.JF_ID.fullmatch(ident):
        return Response(status_code=404)
    return _image("jf", ident)


@router.get("/nexus/api/search")
def search_index():
    """What the search box can find beyond apps and the map's machines: every device, container and stack Pulse
    knows, each with where it opens (a device's page, its Containers tab, the stack on the Stacks page)."""
    from sqlmodel import Session, select

    from ..database import engine
    from ..models import Device
    from .dashboard import _all_containers
    with Session(engine) as s:
        devices = [{"id": d.id, "name": d.name, "online": d.is_online} for d in s.exec(select(Device).order_by(Device.name)).all()]
        rows = _all_containers(s)
    up = lambda c: "running" in (c.get("status") or "").lower()
    containers = [{"name": c.get("name") or "?", "device": c["device_name"], "device_id": c["device_id"], "stack": c.get("stack"),
                   "up": up(c), "status": c.get("status") or ""} for c in rows]
    stacks: dict = {}
    for c in containers:
        if c["stack"]:
            st = stacks.setdefault((c["device_id"], c["stack"]), {"name": c["stack"], "device": c["device"], "device_id": c["device_id"],
                                                                  "running": 0, "total": 0})
            st["total"] += 1
            st["running"] += c["up"]
    return _json({"devices": devices, "containers": containers, "stacks": list(stacks.values())})


@router.get("/nexus/api/pending/{pid}")
def pending(pid: str, after: str = "0"):
    if not re.fullmatch(r"[A-Za-z0-9_-]{16}", pid):
        return Response(status_code=404)
    try:
        return _json(core.pending(pid, int(after) if after.isdigit() else 0))
    except Exception as e:  # noqa: BLE001
        return _fail(e)


async def _body(request: Request) -> dict | None:
    if request.headers.get("X-Nexus") != "1":
        return None
    raw = (await request.body())[:8192]
    try:
        d = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


@router.post("/nexus/api/act")
async def act(request: Request):
    d = await _body(request)
    if d is None:
        return JSONResponse({"error": "not allowed"}, status_code=403)
    if settings.nexus_demo:
        return _json({"ok": True, "say": f"(demo) {d.get('do')} {d.get('id')}"})
    from starlette.concurrency import run_in_threadpool
    try:
        return _json(await run_in_threadpool(core.act, d.get("do"), d.get("id")))
    except ValueError as e:
        return _fail(e, 400)
    except Exception as e:  # noqa: BLE001
        return _fail(e)


@router.post("/nexus/api/chat")
async def chat(request: Request):
    d = await _body(request)
    if d is None:
        return JSONResponse({"error": "not allowed"}, status_code=403)
    from starlette.concurrency import run_in_threadpool
    try:
        return _json(core.demo_chat(d.get("text")) if settings.nexus_demo else await run_in_threadpool(core.chat, d.get("text")))
    except ValueError as e:
        return _fail(e, 400)
    except Exception as e:  # noqa: BLE001
        return _fail(e)


@router.get("/nexus/{path:path}")
def page(path: str):
    """The page and its own files. Anything outside static/ is a 404."""
    if path.startswith("api/"):
        return JSONResponse({"error": "not found"}, status_code=404)
    f = (STATIC / (path or "index.html")).resolve()
    if STATIC not in f.parents or not f.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(f, media_type=TYPES.get(f.suffix, "application/octet-stream"),
                        headers={"Cache-Control": "no-cache" if f.suffix == ".html" else "public, max-age=3600",
                                 "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff"})
