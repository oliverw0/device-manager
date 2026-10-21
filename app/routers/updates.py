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
# Linux client update, POSIX sh run as root over SSH. It finds the client however
# it was installed and fails loudly (set -e) instead of reporting success:
#  - Docker: the container by name, or by compose service label (older compose
#    named it <project>-devicemanager-client-1). `git pull` in its compose dir,
#    with local edits (custom mounts in docker-compose.yml) set aside and put
#    back; if they clash, everything is restored and the job fails with advice.
#  - Native (install-native.sh): copies files, it's not a git checkout, so the
#    latest code is fetched (git clone, else the GitHub tarball) and copied in.
# safe.directory: root pulls a checkout owned by whoever cloned it.
CLIENT_CMD = f"""set -e
G='git -c safe.directory=*'
REPO='{remote.CLIENT_REPO}'
pull() {{
  cd "$1"
  before=$($G rev-parse HEAD)
  stashed=
  if [ -n "$($G status --porcelain --untracked-files=no)" ]; then
    echo "Keeping your local edits to: $($G status --porcelain --untracked-files=no | cut -c4- | tr '\\n' ' ')"
    $G stash push -q -m 'devicemanager update' && stashed=1
  fi
  $G pull --ff-only
  if [ -n "$stashed" ] && ! $G stash pop -q; then
    $G reset -q --hard "$before"
    $G stash pop -q
    echo "Your local edits clash with this update, so nothing was changed. Move custom mounts into docker-compose.override.yml (updates never touch it), then update again."
    exit 1
  fi
}}
cid=$( (docker ps -aq --filter 'name=^devicemanager-client$'; docker ps -aq --filter 'label=com.docker.compose.service=devicemanager-client') 2>/dev/null | head -n 1)
if [ -n "$cid" ]; then
  wd=$(docker inspect "$cid" -f '{{{{index .Config.Labels "com.docker.compose.project.working_dir"}}}}')
  [ -n "$wd" ] || {{ echo "The client container wasn't started with docker compose, so there's no checkout to update."; exit 1; }}
  echo "STAGE:git"; pull "$wd"
  echo "STAGE:build"
  if docker compose --progress plain version >/dev/null 2>&1; then docker compose --progress plain up -d --build
  elif docker compose version >/dev/null 2>&1; then docker compose up -d --build
  else docker-compose up -d --build; fi
elif systemctl cat devicemanager-client >/dev/null 2>&1; then
  dir=$(systemctl show -p WorkingDirectory --value devicemanager-client 2>/dev/null || true)
  [ -n "$dir" ] && [ -d "$dir" ] || dir=/opt/devicemanager-client
  echo "STAGE:git"
  if [ -d "$dir/.git" ]; then
    pull "$dir"
  else
    tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
    if command -v git >/dev/null 2>&1; then git clone -q --depth 1 "$REPO" "$tmp/src"
    else
      mkdir "$tmp/src"
      {{ curl -fsSL "$REPO/archive/refs/heads/main.tar.gz" 2>/dev/null || wget -qO- "$REPO/archive/refs/heads/main.tar.gz"; }} | tar xz -C "$tmp/src" --strip-components=1
    fi
    [ -f "$tmp/src/client/main.py" ] || {{ echo "Downloaded code doesn't contain the client."; exit 1; }}
    rm -rf "$dir/client"; cp -r "$tmp/src/client" "$dir/"; cp "$tmp/src/requirements.txt" "$dir/"
    echo "Copied the latest client into $dir"
  fi
  echo "STAGE:deps"; "$dir/venv/bin/pip" install -q --disable-pip-version-check -r "$dir/requirements.txt"
  echo "STAGE:restart"; systemctl restart devicemanager-client
else
  echo "Couldn't find the client on this machine: no devicemanager-client Docker container and no devicemanager-client systemd service. Reinstall it with install.sh or install-native.sh."
  exit 1
fi
echo "STAGE:done"
"""
CLIENT_STAGES = {
    "git": (5, "Pulling latest code…"),
    "build": (15, "Building image…"),
    "deps": (40, "Installing dependencies…"),  # native Linux and Windows install.ps1 -Update
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
    # Windows PowerShell started with -EncodedCommand also serializes Write-Host
    # output to stderr as CLIXML; -OutputFormat Text already put it on stdout.
    if not line or line.startswith(("#< CLIXML", "<Objs ")):
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

    if job["kind"] == "client" and "Already up to date" in line:  # git: nothing new was pulled
        job["note"] = "Already on the latest code"

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
                    done = "Client updated" if job["kind"] == "client" else "Up to date"
                    job.update(status="done", percent=100, stage=job.get("note") or done, eta_seconds=None)
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
CLIENT_VERSION_URL = f"{remote.CLIENT_RAW}/client/__init__.py"
_latest_cache = {"at": 0.0, "version": None, "ttl": 0}


def _vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:3])


def latest_client_version(reported: list) -> str | None:
    """Newest of: main branch on GitHub and any version a device already reports.
    None when GitHub has never answered (the dashboard then offers "update all"
    without version info). A good answer is kept an hour, a failure 5 minutes."""
    if time.time() - _latest_cache["at"] > _latest_cache["ttl"]:
        _latest_cache["at"] = time.time()
        try:
            r = httpx.get(CLIENT_VERSION_URL, timeout=5, follow_redirects=True)
            m = re.search(r'__version__\s*=\s*"([^"]+)"', r.text)
            if not m:
                raise ValueError(f"HTTP {r.status_code}, no __version__ in response")
            _latest_cache.update(version=m.group(1), ttl=3600)
        except (httpx.HTTPError, ValueError) as exc:
            _latest_cache["ttl"] = 300
            logger.warning("Couldn't read the latest client version from %s: %s", CLIENT_VERSION_URL, exc)
    if not _latest_cache["version"]:
        return None
    return max([_latest_cache["version"], *[v for v in reported if v]], key=_vtuple)


def is_outdated(version: str | None, latest: str | None) -> bool:
    return bool(latest) and _vtuple(version or "1.0.0") < _vtuple(latest)


@router.post("/updates/client-all")
async def update_all_clients(force: bool = False):
    """Start a client update on every online, SSH-enabled device behind the latest
    release, or on all of them when the latest release can't be looked up or
    force is set (the Settings tab's "Update all clients")."""
    with Session(engine) as s:
        devices = s.exec(select(Device)).all()
        versions = {d.id: _agent_version(d) for d in devices}
        latest = None if force else latest_client_version(list(versions.values()))
        targets = [d.id for d in devices if d.ssh_enabled and d.is_online
                   and (latest is None or is_outdated(versions[d.id], latest))]
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
    _parse_line(w, "#< CLIXML")
    _parse_line(w, '<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04"><Obj S="information">')
    assert not any(t.startswith(("#<", "<Objs")) for t in w["tail"]), w["tail"]
    assert is_outdated(None, "1.1.0") and is_outdated("1.9.0", "1.10.0")
    assert not is_outdated("1.1.0", "1.1.0") and not is_outdated("1.0.0", None)
    print("ok")
