"""NEXUS, Pulse's home tab: the lab's action board (the network map, what needs Oliver, and Willow to ask).

core.py is NEXUS's own server code, copied in from the homelab repo (proxmox/nexus/nexus.py at 7ba15cb, 16 Nov 2026).
This copy is the source now: the standalone NEXUS on LXC 106 is retired once Pulse serves it, so edit here. Only four things differ inside Pulse, and setup() patches them:
- secrets come from Pulse's settings (.env), not files;
- Pulse's own data is read in-process (the MCP tools' functions), not over HTTP;
- Willow is reached over the LAN with Pulse's token (willow-core's secrets/pulse-token), not on localhost;
- the Claude runner is the one Pulse's daily summary already uses.
The page (static/) is NEXUS's, also unchanged; routers/nexus.py serves it at /nexus/ with its API under /nexus/api/.
"""
import json

from ..config import settings
from . import core

_SECRETS = {
    "runner-token": lambda: settings.claude_runner_token,
    "tautulli-key": lambda: settings.tautulli_key,
    "seerr-key": lambda: settings.seerr_key,
    "jellyfin-key": lambda: settings.jellyfin_key,
    "ha-token": lambda: settings.ha_token,
}


def _secret(name: str) -> str | None:
    fn = _SECRETS.get(name)
    return (fn() or None) if fn else None


def _pulse(tool: str, **args) -> dict:
    """What NEXUS used to ask Pulse's MCP over HTTP, answered from Pulse's own database."""
    from ..routers import mcp  # here: the routers import the app's modules
    r = mcp._call(tool, args)
    if r.get("isError"):
        raise RuntimeError(r["content"][0]["text"][:200])
    return r["structuredContent"]


def _containers() -> dict:
    """Every container each device's client last reported, as the old Map page drew them (one square each)."""
    from sqlmodel import Session, select

    from ..database import engine
    from ..models import Device
    from ..routers.views import _container_state
    out = {}
    with Session(engine) as s:
        for d in s.exec(select(Device)).all():
            try:
                rep = json.loads(d.last_report_json) if d.last_report_json else {}
            except ValueError:
                rep = {}
            cs = sorted(rep.get("docker_containers") or [], key=lambda c: c.get("name") or "")
            if cs:
                out[d.name] = [{"name": c.get("name"), "state": _container_state(c, d.is_online), "status": c.get("status"),
                                "health": c.get("health")} for c in cs]
    return out


def setup() -> None:
    core.secret = _secret
    core.pulse = _pulse
    core.containers = _containers
    if settings.willow_url:
        core.WILLOW = settings.willow_url.rstrip("/")
    core.WILLOW_HEADERS = {**core.WILLOW_HEADERS, "Authorization": f"Bearer {settings.willow_token}"}
    if settings.claude_runner_url:
        core.RUNNER = settings.claude_runner_url.rstrip("/")


setup()
