import asyncio
import json
import logging

import asyncssh
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlmodel import Session

from .. import ssh_keys
from ..database import engine
from ..models import Device
from .dashboard import derive_ssh_host

logger = logging.getLogger("devicemanager.terminal")

router = APIRouter()


async def _send(ws: WebSocket, message: str) -> None:
    """Send a human-readable notice into the terminal (as text, CRLF for xterm)."""
    try:
        await ws.send_text("\r\n" + message + "\r\n")
    except Exception:
        pass


@router.websocket("/devices/{device_id}/terminal")
async def terminal(websocket: WebSocket, device_id: int, user: str = ""):
    await websocket.accept()

    # SessionMiddleware runs for websocket scope too, so the login cookie is here.
    if not websocket.session.get("is_admin"):
        await _send(websocket, "Not authenticated.")
        await websocket.close(code=4401)
        return

    with Session(engine) as session:
        device = session.get(Device, device_id)
        if device is None:
            await _send(websocket, "Unknown device.")
            await websocket.close()
            return
        if not device.ssh_enabled:
            await _send(websocket, "SSH is not enabled for this device.")
            await websocket.close()
            return
        target = derive_ssh_host(device)
        port = device.ssh_port
        report = json.loads(device.last_report_json) if device.last_report_json else {}
        allowed_users = set((report or {}).get("ssh_users", []))
        device_name = device.name

    if not target:
        await _send(websocket, "No SSH address for this device (no Tailscale IP reported and no override set).")
        await websocket.close()
        return

    # Only allow users the agent actually reported, so a crafted query can't
    # request an arbitrary account. If none were reported, fall back to the value.
    if allowed_users and user not in allowed_users:
        await _send(websocket, f"User '{user}' is not an available login on this device.")
        await websocket.close()
        return
    if not user:
        await _send(websocket, "No user selected.")
        await websocket.close()
        return

    logger.info("terminal session opening: device=%s user=%s target=%s:%s", device_name, user, target, port)

    try:
        conn = await asyncssh.connect(
            target,
            port=port,
            username=user,
            client_keys=[ssh_keys.private_key_path()],
            known_hosts=None,  # trusted Tailscale network; TOFU hardening is a follow-up
            connect_timeout=10,
        )
    except (OSError, asyncssh.Error) as exc:
        await _send(websocket, f"Connection failed: {exc}")
        await _send(websocket, "Check the device is reachable, sshd is running, and the host key was provisioned (install.sh).")
        await websocket.close()
        return

    try:
        proc = await conn.create_process(
            term_type="xterm-256color",
            term_size=(80, 24),
            encoding=None,  # raw bytes both ways
        )
    except asyncssh.Error as exc:
        await _send(websocket, f"Failed to start shell: {exc}")
        conn.close()
        await websocket.close()
        return

    async def ssh_to_ws() -> None:
        while True:
            data = await proc.stdout.read(4096)
            if not data:
                break
            await websocket.send_bytes(data)

    async def ws_to_ssh() -> None:
        while True:
            message = await websocket.receive_text()
            try:
                obj = json.loads(message)
            except ValueError:
                continue
            kind = obj.get("type")
            if kind == "input":
                proc.stdin.write(obj.get("data", "").encode("utf-8"))
            elif kind == "resize":
                try:
                    proc.change_terminal_size(int(obj["cols"]), int(obj["rows"]))
                except (KeyError, ValueError, asyncssh.Error):
                    pass

    tasks = [asyncio.create_task(ssh_to_ws()), asyncio.create_task(ws_to_ssh())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        conn.close()
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info("terminal session closed: device=%s user=%s", device_name, user)
