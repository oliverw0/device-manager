from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from ..database import get_session
from ..models import ServiceCheck

router = APIRouter()


@router.post("/devices/{device_id}/checks")
def create_check(
    device_id: int,
    name: str = Form(...),
    kind: str = Form("http"),
    target: str = Form(...),
    interval_seconds: int = Form(60),
    session: Session = Depends(get_session),
):
    session.add(ServiceCheck(
        device_id=device_id,
        name=name.strip(),
        kind="tcp" if kind == "tcp" else "http",
        target=target.strip(),
        interval_seconds=max(10, interval_seconds),
    ))
    session.commit()
    return RedirectResponse(url=f"/devices/{device_id}", status_code=303)


@router.post("/checks/{check_id}/toggle")
def toggle_check(check_id: int, session: Session = Depends(get_session)):
    check = session.get(ServiceCheck, check_id)
    if check:
        check.enabled = not check.enabled
        check.last_status = "unknown"
        session.add(check)
        session.commit()
    return RedirectResponse(url=f"/devices/{check.device_id}" if check else "/", status_code=303)


@router.post("/checks/{check_id}/delete")
def delete_check(check_id: int, session: Session = Depends(get_session)):
    check = session.get(ServiceCheck, check_id)
    device_id = check.device_id if check else None
    if check:
        session.delete(check)
        session.commit()
    return RedirectResponse(url=f"/devices/{device_id}" if device_id else "/", status_code=303)
