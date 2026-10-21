""""What's using space?" — scan a drive over SSH, and run a few safe cleanups.

Linux: `du -xk -d 2` on the mount (root sees everything; -x stays on that
filesystem). Windows: robocopy's list-only mode per folder, which sums a tree
far faster than walking it in PowerShell; folders inside the 4 largest
top-level folders are sized too, so they can be expanded.

Cleanups are a fixed, whitelisted set per OS, sized during the scan and only
offered for the system drive, where their data lives.
"""
import json
import re
import shlex
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlmodel import Session

from .. import remote
from ..database import engine
from ..models import Device
from .terminal import _ssh_run

router = APIRouter()

SCAN_TIMEOUT = 300  # a full Windows drive can take a minute or two
CLEAN_TIMEOUT = 600

# key -> (what, how, note). How is shown verbatim in the confirmation.
CLEANUPS = {
    "docker-build": ("Docker build cache", "docker builder prune -af",
                     "Images and containers are untouched; the next image build just takes longer."),
    "docker-images": ("Unused Docker images", "docker image prune -af",
                      "Only images no container uses, running or stopped."),
    "apt": ("apt download cache", "apt-get clean", "Downloaded .deb files; installed packages are untouched."),
    "journal": ("Old system logs", "journalctl --vacuum-size=200M", "Keeps the newest 200 MB of the systemd journal."),
    "temp": ("Temp files", r"%TEMP% and C:\Windows\Temp", "Files that are in use are skipped."),
    "recycle": ("Recycle Bin", "Clear-RecycleBin -DriveLetter C", "Permanently deletes everything in the Recycle Bin."),
}
LINUX_CLEAN_CMDS = {
    "docker-build": "docker builder prune -af",
    "docker-images": "docker image prune -af",
    "apt": "apt-get clean",
    "journal": "journalctl --vacuum-size=200M",
}
WIN_CLEAN_SCRIPTS = {
    "docker-build": "docker builder prune -af; exit $LASTEXITCODE",
    "docker-images": "docker image prune -af; exit $LASTEXITCODE",
    "temp": ("Get-ChildItem -LiteralPath ([IO.Path]::GetTempPath()), \"$env:windir\\Temp\" -Force -ErrorAction SilentlyContinue | "
             "Remove-Item -Recurse -Force -ErrorAction SilentlyContinue; exit 0"),
    "recycle": "Clear-RecycleBin -DriveLetter C -Force -ErrorAction SilentlyContinue; exit 0",
}


# --- commands -----------------------------------------------------------------
def linux_scan(mount: str, system_drive: bool) -> str:
    out = [f"m={shlex.quote(mount)}",
           'du -xk -d 2 "$m" 2>/dev/null | sort -rn | head -n 80 | sed "s/^/DU\\t/"']
    if system_drive:
        out += [
            # docker's own estimate of what each prune would free
            "command -v docker >/dev/null 2>&1 && docker system df --format '{{.Type}}|{{.Reclaimable}}' 2>/dev/null | sed 's/^/DOCKER\\t/'",
            "[ -d /var/cache/apt/archives ] && printf 'CLEAN\\tapt\\t%s\\n' \"$(du -sk /var/cache/apt/archives 2>/dev/null | cut -f1)\"",
            "command -v journalctl >/dev/null 2>&1 && printf 'JOURNAL\\t%s\\n' \"$(journalctl --disk-usage 2>/dev/null)\"",
        ]
    return "(" + "\n".join(out) + "\n) 2>/dev/null; exit 0"


def windows_scan(letter: str, system_drive: bool) -> str:
    root = f"{letter}:\\"
    script = f"""
$ErrorActionPreference = 'SilentlyContinue'
function Size($p) {{
  foreach ($l in (robocopy $p "$env:TEMP\\dm-null" /L /S /NJH /BYTES /NFL /NDL /XJ /R:0 /W:0 /NP)) {{
    if ($l -match '^\\s*Bytes\\s*:\\s*(\\d+)') {{ return [int64]$Matches[1] }}
  }}
  return 0
}}
function Dirs($p) {{ Get-ChildItem -LiteralPath $p -Force -Directory | Where-Object {{ -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) }} }}
$top = foreach ($d in (Dirs '{root}')) {{ [pscustomobject]@{{ p = $d.FullName; b = (Size $d.FullName) }} }}
foreach ($t in $top) {{ "1`t$($t.b)`t$($t.p)" }}
Get-ChildItem -LiteralPath '{root}' -Force -File | ForEach-Object {{ "1`t$($_.Length)`t$($_.FullName)" }}
foreach ($t in ($top | Sort-Object b -Descending | Select-Object -First 4)) {{
  foreach ($c in (Dirs $t.p)) {{ "2`t$(Size $c.FullName)`t$($c.FullName)" }}
  Get-ChildItem -LiteralPath $t.p -Force -File | Sort-Object Length -Descending | Select-Object -First 5 | ForEach-Object {{ "2`t$($_.Length)`t$($_.FullName)" }}
}}
"""
    if system_drive:
        script += """
"CLEAN`ttemp`t$((Size ([IO.Path]::GetTempPath())) + (Size "$env:windir\\Temp"))"
"CLEAN`trecycle`t$(Size 'C:\\$Recycle.Bin')"
if (Get-Command docker -ErrorAction SilentlyContinue) { docker system df --format '{{.Type}}|{{.Reclaimable}}' | ForEach-Object { "DOCKER`t$_" } }
"""
    return remote.powershell(script + "\nexit 0")


# --- parsing ------------------------------------------------------------------
_UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


def _human_bytes(text: str) -> int:
    """'1.2GB (40%)' (docker, decimal) or '1.2G' (journalctl, binary) -> bytes."""
    m = re.search(r"([\d.]+)\s*([kmgt]?b?)", text.strip(), re.I)
    if not m:
        return 0
    return int(float(m.group(1)) * _UNITS.get(m.group(2).lower() or "b", 1))


def _cleanup_rows(lines: list[str]) -> list[dict]:
    sizes: dict[str, int] = {}
    for line in lines:
        parts = line.split("\t")
        if parts[0] == "CLEAN" and len(parts) == 3 and parts[2].strip().isdigit():
            n = int(parts[2])
            sizes[parts[1]] = n * 1024 if parts[1] == "apt" else n  # du -sk is KiB
        elif parts[0] == "DOCKER" and len(parts) == 2 and "|" in parts[1]:
            kind, reclaim = parts[1].split("|", 1)
            if kind.strip() == "Build Cache":
                sizes["docker-build"] = _human_bytes(reclaim)
            elif kind.strip() == "Images":
                sizes["docker-images"] = _human_bytes(reclaim)
        elif parts[0] == "JOURNAL" and len(parts) == 2:
            m = re.search(r"take up ([\d.]+\s*[KMGT]?)", parts[1])
            if m:
                sizes["journal"] = max(0, _human_bytes(m.group(1)) - 200 * 1024**2)
    rows = []
    for key, size in sizes.items():
        if size >= 50 * 1024**2:  # not worth a button below ~50 MB
            what, how, note = CLEANUPS[key]
            rows.append({"key": key, "what": what, "how": how, "note": note, "bytes": size})
    return sorted(rows, key=lambda r: -r["bytes"])


def parse_scan(output: str, mount: str, windows: bool) -> dict:
    """Scan output -> {items: [{path, bytes, children: [...]}], cleanups: [...]}"""
    lines = [l.rstrip("\r") for l in output.splitlines() if l.strip()]
    top: dict[str, dict] = {}
    kids: list[tuple[str, int]] = []
    if windows:
        for line in lines:
            parts = line.split("\t")
            if len(parts) == 3 and parts[0] in ("1", "2") and parts[1].isdigit():
                if parts[0] == "1":
                    top[parts[2]] = {"path": parts[2], "bytes": int(parts[1]), "children": []}
                else:
                    kids.append((parts[2], int(parts[1])))
        parent_of = lambda p: p.rsplit("\\", 1)[0]  # noqa: E731
    else:
        base = mount.rstrip("/") or "/"
        for line in lines:
            parts = line.split("\t")
            if len(parts) != 3 or parts[0] != "DU" or not parts[1].isdigit():
                continue
            path, size = parts[2], int(parts[1]) * 1024
            rel = path[len(base):].strip("/") if path.startswith(base) else None
            if not rel:  # the mount itself (du's total line), or outside it
                continue
            if "/" in rel:
                kids.append((path, size))
            else:
                top[path] = {"path": path, "bytes": size, "children": []}
        parent_of = lambda p: p.rsplit("/", 1)[0] or "/"  # noqa: E731
    for path, size in kids:
        parent = top.get(parent_of(path))
        if parent is not None:
            parent["children"].append({"path": path, "bytes": size})
    items = sorted(top.values(), key=lambda i: -i["bytes"])[:15]
    for i in items:
        i["children"] = sorted(i["children"], key=lambda c: -c["bytes"])[:10]
    return {"items": items, "cleanups": _cleanup_rows(lines)}


# --- endpoints ----------------------------------------------------------------
def _load(device_id: int):
    with Session(engine) as s:
        d = s.get(Device, device_id)
    if not d:
        return None, "Unknown device"
    if not d.ssh_enabled:
        return None, "Enable SSH for this device (Settings tab) to scan its drives."
    return d, None


def _drive(device: Device, mount: str):
    """Validate the requested mount against what the device reported.
    Returns (mount, system_drive) or None."""
    report = json.loads(device.last_report_json or "{}") or {}
    system = report.get("system") or {}
    reported = [d.get("mount") for d in (system.get("disks") or []) if d.get("mount")]
    if remote.is_windows(device):
        m = re.fullmatch(r"([A-Za-z]):\\?", mount or "C:")
        if not m:
            return None
        letter = m.group(1).upper()
        known = {r.rstrip("\\").upper() for r in reported} | {"C:"}
        return (letter, letter == "C") if f"{letter}:" in known else None
    mount = mount or "/"
    if mount != "/" and mount not in reported:
        return None
    return mount, mount == "/"


@router.post("/devices/{device_id}/disk-usage")
async def disk_usage(device_id: int, mount: str = ""):
    device, err = _load(device_id)
    if err:
        return JSONResponse({"detail": err}, status_code=400)
    drive = _drive(device, mount)
    if not drive:
        return JSONResponse({"detail": "That drive isn't one this device reported."}, status_code=400)
    target, system_drive = drive
    windows = remote.is_windows(device)
    cmd = windows_scan(target, system_drive) if windows else linux_scan(target, system_drive)
    started = time.time()
    code, out = await _ssh_run(device, remote.update_user(device), cmd, timeout=SCAN_TIMEOUT)
    if code is None:
        return JSONResponse({"detail": out}, status_code=502)
    result = parse_scan(out, f"{target}:\\" if windows else target, windows)
    if not result["items"]:
        return JSONResponse({"detail": "The scan returned nothing. " + out.strip()[-300:]}, status_code=502)
    result.update(mount=f"{target}:" if windows else target, took=round(time.time() - started, 1))
    return result


@router.post("/devices/{device_id}/cleanup/{key}")
async def cleanup(device_id: int, key: str):
    device, err = _load(device_id)
    if err:
        return JSONResponse({"detail": err}, status_code=400)
    if remote.is_windows(device):
        script = WIN_CLEAN_SCRIPTS.get(key)
        cmd = remote.powershell(script) if script else None
    else:
        cmd = LINUX_CLEAN_CMDS.get(key)
    if not cmd:
        return JSONResponse({"detail": "Unknown cleanup"}, status_code=400)
    code, out = await _ssh_run(device, remote.update_user(device), cmd, timeout=CLEAN_TIMEOUT)
    if code is None:
        return JSONResponse({"detail": out}, status_code=502)
    if code != 0:
        return JSONResponse({"detail": f"Cleanup failed (exit {code}): {out.strip()[-300:]}"}, status_code=502)
    return {"ok": True, "output": out.strip()[-500:]}


if __name__ == "__main__":  # ponytail: parser self-check against captured output shapes
    linux_out = "\n".join([
        "DU\t976562500\t/", "DU\t409600000\t/var", "DU\t390000000\t/var/lib", "DU\t15000000\t/var/log",
        "DU\t300000000\t/home", "DU\t290000000\t/home/oliver", "DU\t20000000\t/usr",
        "DOCKER\tImages|12.4GB (63%)", "DOCKER\tBuild Cache|1.5GB", "DOCKER\tContainers|0B (0%)",
        "CLEAN\tapt\t204800", "JOURNAL\tArchived and active journals take up 1.2G in the file system.",
    ])
    r = parse_scan(linux_out, "/", False)
    assert [i["path"] for i in r["items"]] == ["/var", "/home", "/usr"], r["items"]
    assert r["items"][0]["bytes"] == 409600000 * 1024 and r["items"][0]["children"][0]["path"] == "/var/lib"
    keys = {c["key"]: c["bytes"] for c in r["cleanups"]}
    assert keys["docker-images"] == int(12.4e9) and keys["docker-build"] == int(1.5e9), keys
    assert keys["apt"] == 204800 * 1024 and 0 < keys["journal"] < 1.2 * 1024**3, keys
    win_out = "\r\n".join([
        "1\t212000000000\tC:\\Users", "1\t31000000000\tC:\\Windows", "1\t17179869184\tC:\\pagefile.sys",
        "2\t200000000000\tC:\\Users\\olive", "2\t9000000000\tC:\\Windows\\WinSxS",
        "CLEAN\ttemp\t6200000000", "CLEAN\trecycle\t10", "DOCKER\tBuild Cache|18.4GB",
    ])
    r = parse_scan(win_out, "C:\\", True)
    assert [i["path"] for i in r["items"]] == ["C:\\Users", "C:\\Windows", "C:\\pagefile.sys"], r["items"]
    assert r["items"][0]["children"] == [{"path": "C:\\Users\\olive", "bytes": 200000000000}]
    assert [c["key"] for c in r["cleanups"]] == ["docker-build", "temp"], r["cleanups"]   # recycle too small
    assert _human_bytes("0B (0%)") == 0 and _human_bytes("512kB") == 512000
    print("ok")
