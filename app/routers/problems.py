"""The Problems page, acknowledging, and heartbeat pings."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from sqlmodel import Session, select

from .. import problems
from ..database import get_session
from ..models import CheckDay, Device, Problem, ServiceCheck
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


@router.get("/problems", response_class=HTMLResponse)
def problems_page(request: Request, session: Session = Depends(get_session)):
    now = datetime.utcnow()
    devices = {d.id: d for d in session.exec(select(Device)).all()}
    info = {i: serialize_device(d) for i, d in devices.items()}
    open_ = session.exec(select(Problem).where(Problem.resolved_at == None).order_by(Problem.opened_at)).all()  # noqa: E711
    rows = [{"p": p, "d": info.get(p.device_id), "dev": devices.get(p.device_id),
             "since": problems.ago((now - p.opened_at).total_seconds())} for p in open_ if p.device_id in devices]
    # acknowledged ones go in a collapsed list of their own, still in severity order
    groups = [(sev, label, [r for r in rows if r["p"].severity == sev and not r["p"].acked_at]) for sev, label in problems.SEVERITIES]
    acked = [r for sev, _ in problems.SEVERITIES for r in rows if r["p"].severity == sev and r["p"].acked_at]
    resolved = session.exec(select(Problem).where(Problem.resolved_at >= now - timedelta(hours=24))
                            .order_by(Problem.resolved_at.desc())).all()
    checks = []
    days = session.exec(select(CheckDay).where(CheckDay.day >= (now - timedelta(days=30)).strftime("%Y-%m-%d"))).all()
    for c in session.exec(select(ServiceCheck).order_by(ServiceCheck.device_id, ServiceCheck.name)).all():
        bars, overall = problems.uptime([r for r in days if r.check_id == c.id], now.date())
        checks.append({"c": c, "device": devices.get(c.device_id), "bars": bars, "uptime": overall,
                       "cert_days": int((c.cert_expires_at - now).total_seconds() // 86400) if c.cert_expires_at else None})
    online = sum(1 for d in devices.values() if d.is_online)
    return templates.TemplateResponse("problems.html", {
        "request": request, "groups": groups, "acked": acked, "open_count": len(rows), "checks": checks,
        "resolved": [{"p": p, "name": devices[p.device_id].name if p.device_id in devices else "?",
                      "lasted": problems.ago((p.resolved_at - p.opened_at).total_seconds())} for p in resolved],
        "new_count": sum(1 for r in rows if not r["p"].acked_at),
        "checks_up": sum(1 for x in checks if x["c"].enabled and x["c"].last_status == "up"),
        "checks_on": sum(1 for x in checks if x["c"].enabled),
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
