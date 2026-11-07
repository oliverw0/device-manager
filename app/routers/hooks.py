"""Buttons on ntfy notifications: Acknowledge, Mute 1h, and Remove & search
again (stuck downloads).

The ntfy app calls these from the phone, without a Pulse session, so each URL
carries its own signed, week-long token naming the action, device and problem
(or a list of problem keys; "check:7:*" covers every download problem of check 7).
/hooks/ is outside the login (auth_middleware.PUBLIC_PREFIXES).
"""
from datetime import datetime, timedelta

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlmodel import Session, select

from .. import problems
from ..config import settings
from ..database import engine
from ..models import Device, Problem

router = APIRouter()
MAX_AGE = 7 * 86400


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.resolved_session_secret(), salt="ntfy-action")


def action_url(action: str, device_id: int, key: str | list[str] | None = None) -> str:
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
        keys = data.get("k")
        keys = [keys] if isinstance(keys, str) else keys or []
        if data.get("a") == "remove":
            return problems.remove_downloads(s, keys)
        q = select(Problem).where(Problem.device_id == device.id, Problem.resolved_at == None, Problem.acked_at == None)  # noqa: E711
        found = [p for p in s.exec(q).all() if not keys or problems.match_keys(p.key, keys)]
        for p in found:
            p.acked_at, p.ack_note = now, "from the notification"
            s.add(p)
        s.commit()
        return f"{device.name}: acknowledged" if found else f"{device.name}: nothing open to acknowledge"


@router.api_route("/hooks/{token}", methods=["POST", "GET"], response_class=PlainTextResponse)
def hook(token: str):
    return apply(token) + "\n"
