"""Buttons on ntfy notifications: Acknowledge and Mute 1h.

The ntfy app calls these from the phone, without a Pulse session, so each URL
carries its own signed, week-long token naming the action, device and problem.
/hooks/ is outside the login (auth_middleware.PUBLIC_PREFIXES).
"""
from datetime import datetime, timedelta

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlmodel import Session, select

from ..config import settings
from ..database import engine
from ..models import Device, Problem

router = APIRouter()
MAX_AGE = 7 * 86400


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.resolved_session_secret(), salt="ntfy-action")


def action_url(action: str, device_id: int, key: str | None = None) -> str:
    token = _signer().dumps({"a": action, "d": device_id, "k": key})
    return f"{settings.public_url.rstrip('/')}/hooks/{token}"


def apply(token: str, now: datetime | None = None) -> str:
    """Do what the token says. Returns a short answer for ntfy."""
    now = now or datetime.utcnow()
    try:
        data = _signer().loads(token, max_age=MAX_AGE)
    except BadSignature:
        return "expired or invalid"
    with Session(engine) as s:
        device = s.get(Device, data.get("d"))
        if not device:
            return "unknown device"
        if data.get("a") == "mute":
            until = now + timedelta(hours=1)
            if not device.alerts_muted(now) or device.alerts_muted_until < until:
                device.alerts_muted_until = until
                s.add(device)
                s.commit()
            return f"{device.name}: muted for 1 hour"
        q = select(Problem).where(Problem.device_id == device.id, Problem.resolved_at == None, Problem.acked_at == None)  # noqa: E711
        if data.get("k"):
            q = q.where(Problem.key == data["k"])
        found = s.exec(q).all()
        for p in found:
            p.acked_at, p.ack_note = now, "from the notification"
            s.add(p)
        s.commit()
        return f"{device.name}: acknowledged" if found else f"{device.name}: nothing open to acknowledge"


@router.api_route("/hooks/{token}", methods=["POST", "GET"], response_class=PlainTextResponse)
def hook(token: str):
    return apply(token) + "\n"
