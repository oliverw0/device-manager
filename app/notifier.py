import logging

import httpx

from .config import settings

logger = logging.getLogger("devicemanager.notifier")


def send(message: str, title: str = "Pulse", priority: str | None = None, tags: str | None = None,
         click: str | None = None) -> None:
    """click: a Pulse path ("/") the notification opens when tapped, if the
    dashboard's public URL is set (Users page, Dashboard URL)."""
    if not settings.ntfy_url:
        logger.warning("NTFY_URL not configured, skipping notification: %s", message)
        return

    headers = {
        "Title": title,
        "Priority": priority or settings.ntfy_default_priority,
    }
    if tags:
        headers["Tags"] = tags
    if click and settings.public_url:
        headers["Click"] = settings.public_url.rstrip("/") + click

    try:
        httpx.post(settings.ntfy_url, content=message.encode("utf-8"), headers=headers, timeout=10)
    except httpx.HTTPError:
        logger.exception("Failed to send ntfy notification")
