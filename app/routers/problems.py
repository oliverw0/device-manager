"""The Health page (what needs attention, every check, what resolved),
acknowledging, and heartbeat pings."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from sqlmodel import Session, select

from .. import arr, problems
from ..database import get_session
from ..models import Device, Problem, ServiceCheck
from .dashboard import serialize_device, templates

router = APIRouter()


def open_count() -> int:
    """Nav badge: open high + warning problems, not acknowledged."""
    from ..database import engine

    with Session(engine) as s:
        return len(s.exec(select(Problem.id).where(Problem.resolved_at == None, Problem.acked_at == None,  # noqa: E711
                                                   Problem.severity != "info")).all())


templates.env.globals["open_problem_count"] = open_count
templates.env.filters["dur"] = problems.ago


@router.get("/problems")
def old_problems_url():
    return RedirectResponse(url="/health", status_code=301)


@router.get("/health", response_class=HTMLResponse)
def health_page(request: Request, session: Session = Depends(get_session)):
    from ..config import settings
    from .views import status_groups  # views imports dashboard, like this module
    now = datetime.utcnow()
    devices = {d.id: d for d in session.exec(select(Device)).all()}
    info = {i: serialize_device(d) for i, d in devices.items()}
    open_ = session.exec(select(Problem).where(Problem.resolved_at == None).order_by(Problem.opened_at)).all()  # noqa: E711
    rows = [{"p": p, "d": info.get(p.device_id), "dev": devices.get(p.device_id), "download": bool(arr.split_key(p.key)),
             "since": problems.ago((now - p.opened_at).total_seconds())} for p in open_ if p.device_id in devices]
    # acknowledged ones go in a collapsed list of their own, still in severity order
    groups = [(sev, label, [r for r in rows if r["p"].severity == sev and not r["p"].acked_at]) for sev, label in problems.SEVERITIES]
    acked = [r for sev, _ in problems.SEVERITIES for r in rows if r["p"].severity == sev and r["p"].acked_at]
    resolved = session.exec(select(Problem).where(Problem.resolved_at >= now - timedelta(hours=24))
                            .order_by(Problem.resolved_at.desc())).all()
    check_groups, totals = status_groups(session, now)
    online = sum(1 for d in devices.values() if d.is_online)
    return templates.TemplateResponse("problems.html", {
        "request": request, "groups": groups, "acked": acked, "open_count": len(rows),
        "check_groups": check_groups, "public_on": settings.public_status,
        "resolved": [{"p": p, "name": devices[p.device_id].name if p.device_id in devices else "?",
                      "lasted": problems.ago((p.resolved_at - p.opened_at).total_seconds())} for p in resolved],
        "new_count": sum(1 for r in rows if not r["p"].acked_at),
        "checks_up": totals["up"], "checks_on": totals["on"],
        "online": online, "total": len(devices),
    })


@router.post("/problems/{problem_id}/ack")
def ack(problem_id: int, note: str = Form(""), session: Session = Depends(get_session)):
    p = session.get(Problem, problem_id)
    if not p:
        return JSONResponse({"detail": "Unknown problem"}, status_code=404)
    p.acked_at, p.ack_note = (None, "") if p.acked_at else (datetime.utcnow(), note.strip()[:200])
    session.add(p)
    session.commit()
    return {"ok": True}


@router.post("/problems/{problem_id}/remove")
def remove_download(problem_id: int, session: Session = Depends(get_session)):
    """Remove & search again for a stuck download (an arr check's problem)."""
    p = session.get(Problem, problem_id)
    if not p or not arr.split_key(p.key):
        return JSONResponse({"detail": "Not a download problem"}, status_code=404)
    answer, watches = problems.remove_downloads(session, [p.key])
    from .. import downloads
    return {"ok": True, "message": answer, "watches": [downloads.public(w) for w in watches]}


@router.get("/downloads/{watch_id}.json")
def download_progress(watch_id: str, session: Session = Depends(get_session)):
    """The progress card's poll for a Remove & search again."""
    from .. import downloads

    def secret_of(cid):
        c = session.get(ServiceCheck, cid)
        return c.secret if c else None
    job = downloads.poll(watch_id, secret_of)
    return job if job else JSONResponse({"detail": "unknown or finished"}, status_code=404)


@router.post("/problems/ack-all")
def ack_all(session: Session = Depends(get_session)):
    now = datetime.utcnow()
    for p in session.exec(select(Problem).where(Problem.resolved_at == None, Problem.acked_at == None)).all():  # noqa: E711
        p.acked_at = now
        session.add(p)
    session.commit()
    return {"ok": True}


@router.api_route("/ping/{token}", methods=["GET", "POST", "HEAD"], response_class=PlainTextResponse)
def ping(token: str, session: Session = Depends(get_session)):
    """Heartbeat: a job calls this when it finishes. Public: the token is the secret."""
    c = session.exec(select(ServiceCheck).where(ServiceCheck.token == token, ServiceCheck.kind == "heartbeat")).first() if token else None
    if not c:
        return PlainTextResponse("unknown check\n", status_code=404)
    c.last_checked_at, c.last_status, c.last_error = datetime.utcnow(), "up", None
    session.add(c)
    session.commit()
    return "ok\n"
