import logging

import httpx

from .config import settings

logger = logging.getLogger("devicemanager.notifier")


def send(message: str, title: str = "DeviceManager", priority: str | None = None, tags: str | None = None) -> None:
    if not settings.ntfy_url:
        logger.warning("NTFY_URL not configured, skipping notification: %s", message)
        return

    headers = {
        "Title": title,
        "Priority": priority or settings.ntfy_default_priority,
    }
    if tags:
        headers["Tags"] = tags

    try:
        httpx.post(settings.ntfy_url, content=message.encode("utf-8"), headers=headers, timeout=10)
    except httpx.HTTPError:
        logger.exception("Failed to send ntfy notification")
