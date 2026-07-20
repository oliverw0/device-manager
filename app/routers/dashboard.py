import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, delete, select

from ..database import get_session
from ..models import AlertEvent, Device, ReportHistory, generate_api_key

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


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
        "report": report,
    }


@router.get("/", response_class=HTMLResponse)
def index(request: Request, session: Session = Depends(get_session)):
    devices = session.exec(select(Device).order_by(Device.name)).all()
    return templates.TemplateResponse(
        "index.html", {"request": request, "devices": [serialize_device(d) for d in devices]}
    )


@router.get("/devices.json")
def devices_json(session: Session = Depends(get_session)):
    devices = session.exec(select(Device).order_by(Device.name)).all()
    return [serialize_device(d) for d in devices]


@router.post("/devices")
def create_device(
    name: str = Form(...),
    notes: str = Form(""),
    report_interval_seconds: int = Form(60),
    offline_after_seconds: int = Form(150),
    session: Session = Depends(get_session),
):
    device = Device(
        name=name,
        notes=notes,
        report_interval_seconds=report_interval_seconds,
        offline_after_seconds=offline_after_seconds,
    )
    session.add(device)
    session.commit()
    session.refresh(device)
    return RedirectResponse(url=f"/devices/{device.id}", status_code=303)


@router.get("/devices/{device_id}", response_class=HTMLResponse)
def device_detail(request: Request, device_id: int, session: Session = Depends(get_session)):
    device = session.get(Device, device_id)
    alerts = session.exec(
        select(AlertEvent).where(AlertEvent.device_id == device_id).order_by(AlertEvent.created_at.desc()).limit(20)
    ).all()
    return templates.TemplateResponse(
        "device_detail.html",
        {"request": request, "device": serialize_device(device), "api_key": device.api_key, "alerts": alerts},
    )


@router.get("/devices/{device_id}/history.json")
def device_history(device_id: int, session: Session = Depends(get_session)):
    rows = session.exec(
        select(ReportHistory).where(ReportHistory.device_id == device_id).order_by(ReportHistory.timestamp).limit(500)
    ).all()
    return [
        {
            "timestamp": r.timestamp.isoformat(),
            "cpu_percent": r.cpu_percent,
            "mem_percent": r.mem_percent,
            "disk_percent": r.disk_percent,
        }
        for r in rows
    ]


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
