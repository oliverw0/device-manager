import asyncio
import logging
import re
import time
import uuid

import asyncssh
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlmodel import Session

from .. import ssh_keys
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


async def _run_job(job: dict, candidates: list[str], port: int):
    key = ssh_keys.private_key_path()
    cmd = APT_CMD if job["kind"] == "apt" else CLIENT_CMD
    cmd = f"({cmd}) 2>&1"
    err = "no address"
    for addr in candidates:
        try:
            async with asyncssh.connect(
                addr, port=port, username="root",
                client_keys=[key], known_hosts=None, connect_timeout=8,
            ) as conn:
                job["status"] = "running"
                job["stage"] = "Checking for updates…"
                proc = await conn.create_process(cmd, encoding="utf-8", errors="replace")
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
                    job.update(status="done", percent=100, stage="Up to date", eta_seconds=None)
                else:
                    job.update(status="error", stage="Failed",
                               error="\n".join(job["tail"][-4:]) or f"exit {proc.exit_status}")
                logger.info("update job %s (%s) finished: %s", job["id"], job["kind"], job["status"])
                return
        except asyncssh.PermissionDenied:
            job.update(status="error", stage="Failed",
                       error=f"Authentication failed on {addr} — is root authorized for SSH on this device?")
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
    if not candidates:
        return JSONResponse({"detail": "No SSH address for this device"}, status_code=400)

    jid = uuid.uuid4().hex[:12]
    job = JOBS[jid] = {
        "id": jid, "device_id": device_id, "kind": kind,
        "status": "starting", "percent": None, "stage": "Connecting…",
        "eta_seconds": None, "error": None, "tail": [], "started_at": time.time(),
    }
    asyncio.create_task(_run_job(job, candidates, port))
    return {"job_id": jid}


def _public(job: dict) -> dict:
    return {k: job[k] for k in
            ("id", "kind", "status", "percent", "stage", "eta_seconds", "error", "tail")}


@router.get("/devices/{device_id}/update/{job_id}.json")
def update_status(device_id: int, job_id: str):
    job = JOBS.get(job_id)
    if not job or job["device_id"] != device_id:
        return JSONResponse({"detail": "unknown job"}, status_code=404)
    return _public(job)


@router.get("/devices/{device_id}/updates.json")
def active_updates(device_id: int):
    """Running jobs for this device, so a page revisit can re-attach its progress."""
    return {"jobs": [_public(j) for j in JOBS.values()
                     if j["device_id"] == device_id and j["status"] in ("starting", "running")]}


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
    print("ok")
