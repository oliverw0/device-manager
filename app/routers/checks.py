import secrets

from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from .. import pbs, proxmox
from ..database import get_session
from ..models import ServiceCheck

router = APIRouter()

KINDS = ("http", "tcp", "dns", "heartbeat", "pbs", "proxmox")


def _target(kind: str, target: str) -> str:
    target = target.strip()
    if kind == "pbs":
        return pbs.normalize_url(target)
    if kind == "proxmox":
        return proxmox.normalize_url(target)
    return target


@router.post("/devices/{device_id}/checks")
def create_check(
    device_id: int,
    name: str = Form(...),
    kind: str = Form("http"),
    target: str = Form(""),
    interval_seconds: int = Form(60),
    grace_minutes: int = Form(15),
    secret: str = Form(""),
    expect: str = Form(""),
    group: str = Form(""),
    session: Session = Depends(get_session),
):
    kind = kind if kind in KINDS else "http"
    common = {"device_id": device_id, "name": name.strip(), "kind": kind, "group": group.strip()[:40]}
    if kind == "heartbeat":
        # interval: how often the job runs; it's late after interval + grace
        session.add(ServiceCheck(**common, target="", interval_seconds=max(60, interval_seconds),
                                 grace_seconds=max(0, grace_minutes) * 60, token=secrets.token_urlsafe(16)))
    elif kind in ("pbs", "proxmox"):
        if target.strip() and secret.strip():  # these change slowly: at most every 5 min
            session.add(ServiceCheck(**common, target=_target(kind, target), secret=secret.strip(),
                                     interval_seconds=max(300, interval_seconds)))
    elif target.strip():
        session.add(ServiceCheck(**common, target=target.strip(), expect=expect.strip()[:200],
                                 interval_seconds=max(10, interval_seconds)))
    session.commit()
    return RedirectResponse(url=f"/devices/{device_id}#checks", status_code=303)


@router.post("/checks/{check_id}/edit")
def edit_check(
    check_id: int,
    name: str = Form(...),
    target: str = Form(""),
    expect: str = Form(""),
    group: str = Form(""),
    secret: str = Form(""),
    interval_seconds: int = Form(0),
    back: str = Form(""),
    session: Session = Depends(get_session),
):
    """Name, Status-page group, and (where the type has them) target, expect,
    interval. A blank token keeps the saved one."""
    check = session.get(ServiceCheck, check_id)
    if not check:
        return RedirectResponse(url="/", status_code=303)
    check.name = name.strip() or check.name
    check.group = group.strip()[:40]
    if check.kind != "heartbeat" and target.strip():
        new_target = _target(check.kind, target)
        if new_target != check.target:
            check.target, check.last_status, check.cert_expires_at, check.cert_checked_at = new_target, "unknown", None, None
            check.last_checked_at = None  # run it on the next sweep
    if check.kind in ("http", "dns"):
        check.expect = expect.strip()[:200]
        check.last_checked_at = None
    if check.kind in ("pbs", "proxmox") and secret.strip():
        check.secret = secret.strip()
        check.last_checked_at = None
    if interval_seconds and check.kind != "heartbeat":
        check.interval_seconds = max(300 if check.kind in ("pbs", "proxmox") else 10, interval_seconds)
    session.add(check)
    session.commit()
    return RedirectResponse(url=back if back.startswith("/") else f"/devices/{check.device_id}#checks", status_code=303)


@router.post("/checks/{check_id}/toggle")
def toggle_check(check_id: int, session: Session = Depends(get_session)):
    check = session.get(ServiceCheck, check_id)
    if check:
        check.enabled = not check.enabled
        check.last_status = "unknown"
        session.add(check)
        session.commit()
    return RedirectResponse(url=f"/devices/{check.device_id}#checks" if check else "/", status_code=303)


@router.post("/checks/{check_id}/delete")
def delete_check(check_id: int, session: Session = Depends(get_session)):
    check = session.get(ServiceCheck, check_id)
    device_id = check.device_id if check else None
    if check:
        session.delete(check)
        session.commit()
    return RedirectResponse(url=f"/devices/{device_id}#checks" if device_id else "/", status_code=303)
