import asyncio
import json
import logging
import re
import time
import uuid

import asyncssh
import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from .. import remote, ssh_keys
from ..database import engine
from ..models import Device
from .dashboard import ssh_candidates

logger = logging.getLogger("devicemanager.updates")

router = APIRouter()

# In-memory job store. ponytail: single-process only — jobs vanish on restart
# and aren't shared across workers. Fine for a foreground admin action; revisit
# with a table/redis only if this app ever runs multi-worker.
JOBS: dict[str, dict] = {}
JOB_TTL = 600  # keep finished jobs this long so a returning page can still read them

# apt writes machine-readable progress to APT::Status-Fd — dlstatus/pmstatus
# lines carry a real 0-100 percent, so the bar isn't guessed. 2>&1 merges
# docker's build output (stderr) so we can parse its step counter too.
APT_CMD = (
    "export DEBIAN_FRONTEND=noninteractive; "
    "apt-get update -qq && apt-get -y -o APT::Status-Fd=1 upgrade"
)
CLIENT_CMD = (
    'if docker inspect devicemanager-client >/dev/null 2>&1; then '
    'wd=$(docker inspect devicemanager-client -f \'{{index .Config.Labels "com.docker.compose.project.working_dir"}}\'); '
    'echo "STAGE:git"; cd "$wd" && git pull --ff-only && '
    'echo "STAGE:build"; { docker compose up -d --build --progress plain || docker-compose up -d --build; }; '
    'else '
    'echo "STAGE:git"; cd /opt/devicemanager-client && git pull --ff-only && '
    'echo "STAGE:restart"; systemctl restart devicemanager-client; '
    'fi; echo "STAGE:done"'
)
CLIENT_STAGES = {
    "git": (5, "Pulling latest code…"),
    "build": (15, "Building image…"),
    "deps": (40, "Installing dependencies…"),  # Windows install.ps1 -Update
    "restart": (90, "Restarting client…"),
    "done": (100, "Done"),
}


def _prune():
    now = time.time()
    for jid in [j for j, v in JOBS.items()
                if v["status"] in ("done", "error") and now - v["started_at"] > JOB_TTL]:
        JOBS.pop(jid, None)


def _active_job(device_id: int, kind: str):
    return next((j for j in JOBS.values()
                 if j["device_id"] == device_id and j["kind"] == kind
                 and j["status"] in ("starting", "running")), None)


def _set_percent(job: dict, p: float):
    p = max(0, min(99, round(p)))  # 100 only when we mark it done
    if job["percent"] is None or p > job["percent"]:
        job["percent"] = p
    # ETA from elapsed / percent — only once there's enough signal to be meaningful.
    if job["percent"] and job["percent"] >= 3:
        elapsed = time.time() - job["started_at"]
        total = elapsed / (job["percent"] / 100.0)
        job["eta_seconds"] = max(0, round(total - elapsed))


def _parse_line(job: dict, line: str):
    if not line:
        return
    job["tail"] = (job["tail"] + [line])[-8:]

    if line.startswith(("pmstatus:", "dlstatus:")):
        parts = line.split(":", 3)
        if len(parts) < 4:
            return
        try:
            p = float(parts[2])
        except ValueError:
            return
        if parts[0] == "dlstatus":
            _set_percent(job, p * 0.30)          # download = first 30%
            job["stage"] = "Downloading updates…"
        else:
            _set_percent(job, 30 + p * 0.70)     # unpack/configure = last 70%
            job["stage"] = parts[3] or "Installing…"
        return

    # Windows scripts (windows-update.ps1): "PROGRESS:<pct>:<stage>" and a
    # "NOTE:<text>" summary shown on the finished card (e.g. reboot required).
    if line.startswith("PROGRESS:"):
        parts = line.split(":", 2)
        try:
            _set_percent(job, float(parts[1]))
        except (IndexError, ValueError):
            return
        if len(parts) > 2 and parts[2]:
            job["stage"] = parts[2]
        return
    if line.startswith("NOTE:"):
        job["note"] = line[5:].strip()
        return

    if line.startswith("STAGE:"):
        st = line.split(":", 1)[1].strip()
        if st in CLIENT_STAGES:
            pct, stage = CLIENT_STAGES[st]
            _set_percent(job, pct)
            job["stage"] = stage
        return

    # docker buildkit step counter: "#12 [ 5/10] RUN ..." → within the 15-85% build band
    if job["kind"] == "client":
        m = re.search(r"\[\s*(\d+)/(\d+)\]", line)
        if m and int(m.group(2)):
            _set_percent(job, 15 + (int(m.group(1)) / int(m.group(2))) * 70)


async def _run_job(job: dict, candidates: list[str], port: int, user: str, cmd: str):
    key = ssh_keys.private_key_path()
    err = "no address"
    for addr in candidates:
        try:
            async with asyncssh.connect(
                addr, port=port, username=user,
                client_keys=[key], known_hosts=None, connect_timeout=8,
            ) as conn:
                job["status"] = "running"
                job["stage"] = "Checking for updates…"
                # stderr merged in so failures land in the tail (Linux also does 2>&1).
                proc = await conn.create_process(cmd, encoding="utf-8", errors="replace", stderr=asyncssh.STDOUT)
                buf = ""
                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        _parse_line(job, line.rstrip("\r"))
                if buf:
                    _parse_line(job, buf.rstrip("\r"))
                await proc.wait()
                if proc.exit_status == 0:
                    job.update(status="done", percent=100, stage=job.get("note") or "Up to date", eta_seconds=None)
                else:
                    job.update(status="error", stage="Failed",
                               error="\n".join(job["tail"][-4:]) or f"exit {proc.exit_status}")
                logger.info("update job %s (%s) finished: %s", job["id"], job["kind"], job["status"])
                return
        except asyncssh.PermissionDenied:
            job.update(status="error", stage="Failed",
                       error=f"Authentication failed on {addr} — is {user} authorized for SSH on this device?")
            return
        except (OSError, asyncssh.Error) as exc:
            err = str(exc)
            continue
    job.update(status="error", stage="Failed", error=f"Could not reach device ({err})")


@router.post("/devices/{device_id}/update/{kind}")
async def start_update(device_id: int, kind: str):
    if kind not in ("apt", "client"):
        return JSONResponse({"detail": "unknown update kind"}, status_code=400)
    _prune()
    existing = _active_job(device_id, kind)
    if existing:
        return {"job_id": existing["id"]}  # dedupe double-clicks / concurrent tabs

    with Session(engine) as s:
        device = s.get(Device, device_id)
        if device is None or not device.ssh_enabled:
            return JSONResponse({"detail": "SSH is not enabled for this device"}, status_code=400)
        candidates = ssh_candidates(device)
        port = device.ssh_port
        device_name = device.name
        user = remote.update_user(device)
        if remote.is_windows(device):
            cmd = remote.windows_update_command(kind)
        else:
            cmd = f"({APT_CMD if kind == 'apt' else CLIENT_CMD}) 2>&1"
    if not candidates:
        return JSONResponse({"detail": "No SSH address for this device"}, status_code=400)

    jid = uuid.uuid4().hex[:12]
    job = JOBS[jid] = {
        "id": jid, "device_id": device_id, "device_name": device_name, "kind": kind,
        "status": "starting", "percent": None, "stage": "Connecting…",
        "eta_seconds": None, "error": None, "tail": [], "started_at": time.time(),
    }
    asyncio.create_task(_run_job(job, candidates, port, user, cmd))
    return {"job_id": jid}


# --- client release tracking -------------------------------------------------
# The client's version lives in client/__init__.py on the repo's main branch.
CLIENT_VERSION_URL = "https://raw.githubusercontent.com/oliverw0/device-manager-client/main/client/__init__.py"
_latest_cache = {"at": 0.0, "version": None}


def _vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:3])


def latest_client_version(reported: list) -> str | None:
    """Newest of: main branch on GitHub (cached for an hour, skipped if offline)
    and any version a device already reports."""
    if time.time() - _latest_cache["at"] > 3600:
        _latest_cache["at"] = time.time()
        try:
            m = re.search(r'__version__\s*=\s*"([^"]+)"', httpx.get(CLIENT_VERSION_URL, timeout=3).text)
            _latest_cache["version"] = m.group(1) if m else None
        except httpx.HTTPError:
            pass  # keep the last known value
    found = [v for v in [_latest_cache["version"], *reported] if v]
    return max(found, key=_vtuple) if found else None


def is_outdated(version: str | None, latest: str | None) -> bool:
    return bool(latest) and _vtuple(version or "1.0.0") < _vtuple(latest)


@router.post("/updates/client-all")
async def update_all_clients():
    """Start a client update on every online, SSH-enabled device behind the latest release."""
    with Session(engine) as s:
        devices = s.exec(select(Device)).all()
        versions = {d.id: _agent_version(d) for d in devices}
        latest = latest_client_version(list(versions.values()))
        targets = [d.id for d in devices if d.ssh_enabled and d.is_online and is_outdated(versions[d.id], latest)]
    started = []
    for device_id in targets:
        res = await start_update(device_id, "client")
        if isinstance(res, dict):
            started.append(res["job_id"])
    return {"started": len(started), "job_ids": started}


def _agent_version(device: Device) -> str | None:
    try:
        return json.loads(device.last_report_json or "{}").get("agent_version")
    except ValueError:
        return None


def _public(job: dict) -> dict:
    return {k: job[k] for k in
            ("id", "device_id", "device_name", "kind", "status", "percent",
             "stage", "eta_seconds", "error", "tail")}


@router.get("/devices/{device_id}/update/{job_id}.json")
def update_status(device_id: int, job_id: str):
    job = JOBS.get(job_id)
    if not job or job["device_id"] != device_id:
        return JSONResponse({"detail": "unknown job"}, status_code=404)
    return _public(job)


@router.get("/updates.json")
def active_updates():
    """Every running job across all devices, so the persistent overlay can
    re-attach after any navigation (boosted or a hard reload)."""
    _prune()
    return {"jobs": [_public(j) for j in JOBS.values()
                     if j["status"] in ("starting", "running")]}


if __name__ == "__main__":  # ponytail: progress-parser self-check
    j = {"kind": "apt", "percent": None, "eta_seconds": None, "stage": "", "tail": [],
         "started_at": time.time() - 10}
    _parse_line(j, "dlstatus:pkg:50.0:Retrieving")
    assert j["percent"] == 15, j["percent"]          # 50% of the 30% download band
    _parse_line(j, "pmstatus:pkg:100:Unpacking foo")
    assert j["percent"] == 99, j["percent"]          # 30 + 70 = 100 -> clamped to 99 while running
    c = {"kind": "client", "percent": None, "eta_seconds": None, "stage": "", "tail": [],
         "started_at": time.time()}
    _parse_line(c, "STAGE:build")
    assert c["percent"] == 15
    _parse_line(c, "#7 [ 5/10] RUN pip install")
    assert c["percent"] == round(15 + 0.5 * 70), c["percent"]
    w = {"kind": "apt", "percent": None, "eta_seconds": None, "stage": "", "tail": [],
         "started_at": time.time()}
    _parse_line(w, "PROGRESS:40:Installing 1/3: KB5031364")
    assert w["percent"] == 40 and w["stage"] == "Installing 1/3: KB5031364", w
    _parse_line(w, "NOTE:Installed 3 of 3 update(s) - reboot required to finish")
    assert w["note"].endswith("reboot required to finish")
    assert is_outdated(None, "1.1.0") and is_outdated("1.9.0", "1.10.0")
    assert not is_outdated("1.1.0", "1.1.0") and not is_outdated("1.0.0", None)
    print("ok")
