import asyncio
import json
import logging
import os

import asyncssh
from fastapi import APIRouter, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlmodel import Session

from .. import ssh_keys
from ..database import engine
from ..models import Device
from .dashboard import ssh_candidates

logger = logging.getLogger("devicemanager.terminal")

router = APIRouter()

CONTAINER_ACTIONS = {"start", "stop", "restart"}


def _known_container(last_report_json, name) -> bool:
    """Whitelist the name against the last report so it can't inject into the
    remote shell command (docker names are a safe charset, but the URL isn't)."""
    report = json.loads(last_report_json) if last_report_json else {}
    return name in {c.get("name") for c in (report.get("docker_containers") or [])}


async def _ssh_run(device: Device, user: str, cmd: str):
    """Run one command over SSH, trying each candidate address. Returns (exit, output)."""
    key = ssh_keys.private_key_path()
    err = "no address"
    for addr in ssh_candidates(device):
        try:
            async with asyncssh.connect(
                addr, port=device.ssh_port, username=user,
                client_keys=[key], known_hosts=None, connect_timeout=8,
            ) as conn:
                r = await conn.run(cmd, check=False, timeout=25)
                return r.exit_status, (r.stdout or "") + (r.stderr or "")
        except (OSError, asyncssh.Error) as exc:
            err = str(exc)
            continue
    return None, f"could not reach device ({err})"


def _load_device(device_id: int, name: str):
    with Session(engine) as s:
        d = s.get(Device, device_id)
    if not d or not d.ssh_enabled:
        return None, "SSH is not enabled for this device"
    if not _known_container(d.last_report_json, name):
        return None, "unknown container"
    return d, None


@router.post("/devices/{device_id}/container/{name}/{action}")
async def container_action(device_id: int, name: str, action: str, user: str = "root"):
    if action not in CONTAINER_ACTIONS:
        return JSONResponse({"ok": False, "output": "bad action"}, status_code=400)
    device, err = _load_device(device_id, name)
    if device is None:
        return JSONResponse({"ok": False, "output": err}, status_code=400)
    code, out = await _ssh_run(device, user, f"docker {action} {name}")
    return JSONResponse({"ok": code == 0, "output": out.strip()})


@router.get("/devices/{device_id}/container/{name}/logs", response_class=PlainTextResponse)
async def container_logs(device_id: int, name: str, user: str = "root"):
    device, err = _load_device(device_id, name)
    if device is None:
        return PlainTextResponse(err, status_code=400)
    code, out = await _ssh_run(device, user, f"docker logs --tail 200 {name} 2>&1")
    return PlainTextResponse(out or "(no output)")


async def _ssh_upload(device: Device, user: str, filename: str, data: bytes):
    """SFTP a blob into the user's home dir, trying each candidate address."""
    key = ssh_keys.private_key_path()
    err = "no address"
    for addr in ssh_candidates(device):
        try:
            async with asyncssh.connect(
                addr, port=device.ssh_port, username=user,
                client_keys=[key], known_hosts=None, connect_timeout=8,
            ) as conn:
                async with conn.start_sftp_client() as sftp:
                    async with sftp.open(filename, "wb") as f:
                        await f.write(data)
                    abspath = await sftp.realpath(filename)  # resolve ~ to a full path
                    if isinstance(abspath, bytes):
                        abspath = abspath.decode("utf-8", "replace")
                return True, abspath
        except (OSError, asyncssh.Error) as exc:
            err = str(exc)
            continue
    return False, f"could not upload ({err})"


@router.post("/devices/{device_id}/upload")
async def upload(device_id: int, user: str = "", file: UploadFile = File(...)):
    with Session(engine) as s:
        device = s.get(Device, device_id)
        if device is None or not device.ssh_enabled:
            return JSONResponse({"ok": False, "output": "SSH is not enabled"}, status_code=400)
        report = json.loads(device.last_report_json) if device.last_report_json else {}
        allowed = set((report or {}).get("ssh_users", []))
    if not user or (allowed and user not in allowed):
        return JSONResponse({"ok": False, "output": f"user '{user}' is not available"}, status_code=400)
    # basename only: land in home dir, no traversal surprises (admin has a shell anyway).
    name = os.path.basename(file.filename or "") or "upload.bin"
    data = await file.read()  # ponytail: whole file in memory; stream if huge configs show up
    ok, out = await _ssh_upload(device, user, name, data)
    return JSONResponse({"ok": ok, "output": out})


async def _send(ws: WebSocket, message: str) -> None:
    """Send a human-readable notice into the terminal (as text, CRLF for xterm)."""
    try:
        await ws.send_text("\r\n" + message + "\r\n")
    except Exception:
        pass


@router.websocket("/devices/{device_id}/terminal")
async def terminal(websocket: WebSocket, device_id: int, user: str = "", container: str = ""):
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
        candidates = ssh_candidates(device)
        port = device.ssh_port
        report = json.loads(device.last_report_json) if device.last_report_json else {}
        allowed_users = set((report or {}).get("ssh_users", []))
        container_known = _known_container(device.last_report_json, container) if container else False
        device_name = device.name

    if not candidates:
        await _send(websocket, "No SSH address for this device (no Tailscale IP or local IP reported, and no override set).")
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

    if container and not container_known:
        await _send(websocket, "Unknown container.")
        await websocket.close()
        return

    # Try each candidate (Tailscale first, then local addresses). A network
    # failure moves on to the next; an auth failure stops immediately since the
    # key/user won't differ by address.
    conn = None
    auth_failed = False
    for addr in candidates:
        logger.info("terminal connect attempt: device=%s user=%s target=%s:%s", device_name, user, addr, port)
        try:
            conn = await asyncssh.connect(
                addr,
                port=port,
                username=user,
                client_keys=[ssh_keys.private_key_path()],
                known_hosts=None,  # trusted Tailscale network; TOFU hardening is a follow-up
                connect_timeout=8,
            )
            await _send(websocket, f"Connected to {addr}.")
            break
        except asyncssh.PermissionDenied as exc:
            await _send(websocket, f"Authentication failed on {addr}: {exc}")
            auth_failed = True
            break
        except (OSError, asyncssh.Error) as exc:
            await _send(websocket, f"Could not reach {addr}: {exc} — trying next…")
            continue

    if conn is None:
        if auth_failed:
            await _send(websocket, "The host's key isn't authorized for this user. Run provision-ssh.sh on the target.")
        else:
            await _send(websocket, "All addresses failed. Check the device is reachable and sshd is running.")
        await websocket.close()
        return

    # container name is whitelisted against the last report above, so it's a safe
    # docker-name charset — no shell-injection risk in the exec command.
    exec_cmd = None
    if container:
        exec_cmd = f"docker exec -it {container} /bin/bash 2>/dev/null || docker exec -it {container} /bin/sh"
        await _send(websocket, f"docker exec into {container}…")
    try:
        proc = await conn.create_process(
            exec_cmd,  # None -> login shell; set -> exec into the container
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


if __name__ == "__main__":  # ponytail: injection-guard self-check
    j = '{"docker_containers":[{"name":"plex"}]}'
    assert _known_container(j, "plex")
    assert not _known_container(j, "evil; rm -rf /")
    assert not _known_container(None, "plex")
    print("ok")
