import logging
from datetime import datetime

import httpx

from .config import settings

logger = logging.getLogger("devicemanager.notifier")


def send(message: str, title: str = "Pulse", priority: str | None = None, tags: str | None = None,
         click: str | None = None, device_id: int | None = None, key: str | None = None) -> bool:
    """Push to ntfy; every attempt is written to the notification log.
    click: a Pulse path ("/") the notification opens when tapped, if the
    dashboard's public URL is set (Users page, Dashboard URL).
    device_id (+ key, the problem's key such as "offline" or "check:5") adds
    Acknowledge and Mute 1h buttons, which call back to Pulse with a signed link.
    Returns whether ntfy accepted it."""
    if not settings.ntfy_url:
        logger.warning("NTFY_URL not configured, skipping notification: %s", message)
        return False

    headers = {
        "Title": title,
        "Priority": priority or settings.ntfy_default_priority,
    }
    if tags:
        headers["Tags"] = tags
    base = settings.public_url.rstrip("/")
    if base:
        if click:
            headers["Click"] = base + click
        elif device_id:
            headers["Click"] = f"{base}/devices/{device_id}"
        if device_id:
            from .routers.hooks import action_url  # here: routers import this module
            headers["Actions"] = "; ".join([
                f"http, Acknowledge, {action_url('ack', device_id, key)}, method=POST, clear=true",
                f"http, Mute 1h, {action_url('mute', device_id, key)}, method=POST, clear=true",
            ])

    ok, error = True, ""
    try:
        r = httpx.post(settings.ntfy_url, content=message.encode("utf-8"), headers=headers, timeout=10)
        if r.status_code >= 400:
            ok, error = False, f"ntfy answered {r.status_code}: {r.text[:200]}"
    except httpx.HTTPError as exc:
        ok, error = False, str(exc) or type(exc).__name__
    if not ok:
        logger.error("Failed to send ntfy notification: %s", error)
    _log(title, message, headers["Priority"], ok, error)
    return ok


def _log(title: str, message: str, priority: str, ok: bool, error: str) -> None:
    from sqlmodel import Session

    from .database import engine
    from .models import NotificationLog
    try:
        with Session(engine) as s:
            s.add(NotificationLog(created_at=datetime.utcnow(), title=title, message=message[:2000],
                                  priority=priority, ok=ok, error=error[:500]))
            s.commit()
    except Exception:  # noqa: BLE001 — the log must never stop an alert
        logger.exception("couldn't write the notification log")


def last_failed() -> bool:
    """Did the most recent push fail? (The watchdog holds off while it did.)"""
    from sqlmodel import Session, select

    from .database import engine
    from .models import NotificationLog
    with Session(engine) as s:
        last = s.exec(select(NotificationLog).order_by(NotificationLog.id.desc()).limit(1)).first()
    return bool(last and not last.ok)
