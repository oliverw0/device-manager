"""Turn power tracking on or off for a Linux device, over SSH as root.

On installs the pve-power exporter (client/power/pve_power.py, served on
:9105) as a systemd service; the client then reports its readings. It needs the
CPU's RAPL energy counters, which bare-metal Intel/AMD Linux has and LXCs/VMs
don't (a Proxmox host's readings already include each guest's share).
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlmodel import Session

from .. import remote
from ..database import engine
from ..models import Device
from .terminal import _ssh_run

router = APIRouter()

NO_RAPL_EXIT = 3  # not supported here: a guest, or no RAPL counters
# An existing /usr/local/bin/pve_power.py is kept: it may hold local calibration
# (CAL_SCALE, fan counts, drive specs).
# Bare metal only. A guest can't measure the wall: a privileged LXC may even see
# the host's RAPL counters, but estimating with the host's PSU there would count
# the same watts twice. Guests get their share from the host's report instead.
ENABLE_CMD = f"""set -e
export PATH="$PATH:/usr/local/bin:/usr/local/sbin"
virt=$(systemd-detect-virt 2>/dev/null || true)
if [ -z "$virt" ]; then
  if grep -qa 'container=' /proc/1/environ 2>/dev/null || [ -f /.dockerenv ]; then virt=container
  elif grep -qw hypervisor /proc/cpuinfo 2>/dev/null; then virt=vm; fi
fi
if [ -n "$virt" ] && [ "$virt" != none ]; then
  echo "This is a virtual machine or container ($virt), not bare metal, so it can't measure wall power. Turn power tracking on for the physical host (e.g. your Proxmox host): its report includes each guest's share, which shows on this device's page."
  exit {NO_RAPL_EXIT}
fi
if [ ! -r /sys/class/powercap/intel-rapl:0/energy_uj ]; then
  echo "No CPU power counters here (RAPL at /sys/class/powercap/intel-rapl:0). LXCs and VMs can't read them: turn power tracking on for the Proxmox host instead, whose readings include each guest's share."
  exit {NO_RAPL_EXIT}
fi
command -v python3 >/dev/null 2>&1 || {{ echo "python3 is needed for the power exporter."; exit 1; }}
RAW='{remote.CLIENT_RAW}/power'
fetch() {{ curl -fsSL "$1" -o "$2" 2>/dev/null || wget -qO "$2" "$1"; }}
if [ ! -f /usr/local/bin/pve_power.py ]; then fetch "$RAW/pve_power.py" /usr/local/bin/pve_power.py; fi
chmod 755 /usr/local/bin/pve_power.py
fetch "$RAW/pve-power.service" /etc/systemd/system/pve-power.service
systemctl daemon-reload
systemctl enable pve-power >/dev/null 2>&1
systemctl restart pve-power
i=0
while [ $i -lt 15 ]; do
  sleep 1; i=$((i + 1))
  if curl -fs localhost:9105/json >/dev/null 2>&1 || wget -qO- localhost:9105/json >/dev/null 2>&1; then
    echo "Power tracking is running."; exit 0
  fi
done
echo "Installed, but the exporter isn't answering yet:"
journalctl -u pve-power -n 5 --no-pager 2>/dev/null || true
exit 1
"""
# Off means nothing runs on the machine: the service is stopped and disabled
# (so it won't come back at boot). Its files stay, so turning it on again is quick.
DISABLE_CMD = """systemctl disable --now pve-power 2>/dev/null || true
if systemctl is-active --quiet pve-power; then echo "pve-power is still running"; exit 1; fi
echo "Power tracking stopped: the pve-power service is stopped and won't start at boot."
"""

# --- Windows: LibreHardwareMonitor's library reads CPU power through the signed
# PawnIO driver; client/power/win_power.ps1 turns that (plus nvidia-smi for
# NVIDIA GPUs) into a wall estimate using the chosen PSU's curve, as a SYSTEM
# scheduled task writing %ProgramData%\DeviceManagerClient\power.json.
LHM_ZIP = "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/v0.9.6/LibreHardwareMonitor.zip"
PAWNIO_SETUP = "https://github.com/namazso/PawnIO.Setup/releases/download/2.2.0/PawnIO_setup.exe"
WIN_TASK = "DeviceManagerPower"


def windows_enable_script(psu: str) -> str:
    return f"""$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 'Tls12'
$cs = Get-CimInstance Win32_ComputerSystem
if ("$($cs.Manufacturer) $($cs.Model)" -match 'Virtual|VMware|VirtualBox|QEMU|KVM|Xen|Parallels') {{
  Write-Output "This is a virtual machine ($($cs.Model)), not bare metal, so it can't measure wall power. Turn power tracking on for the physical host instead."
  exit {NO_RAPL_EXIT}
}}
$dir = Join-Path $env:ProgramFiles 'DeviceManagerClient\\power'
$data = Join-Path $env:ProgramData 'DeviceManagerClient'
New-Item -ItemType Directory -Force -Path $dir, $data | Out-Null
$tmp = Join-Path $env:TEMP ('dm-power-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {{
  if (-not (Test-Path "$dir\\lhm\\LibreHardwareMonitorLib.dll")) {{
    Write-Output 'Downloading LibreHardwareMonitor ...'
    Invoke-WebRequest -UseBasicParsing -Uri '{LHM_ZIP}' -OutFile "$tmp\\lhm.zip"
    Expand-Archive -Path "$tmp\\lhm.zip" -DestinationPath "$dir\\lhm" -Force
  }}
  if (-not (Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\PawnIO' -ErrorAction SilentlyContinue)) {{
    Write-Output 'Installing the PawnIO driver ...'
    Invoke-WebRequest -UseBasicParsing -Uri '{PAWNIO_SETUP}' -OutFile "$tmp\\PawnIO_setup.exe"
    $p = Start-Process -FilePath "$tmp\\PawnIO_setup.exe" -ArgumentList '-install', '-silent' -Wait -PassThru
    if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) {{ Write-Output "The PawnIO driver didn't install (exit $($p.ExitCode))."; exit 1 }}
  }}
  Invoke-WebRequest -UseBasicParsing -Uri '{remote.CLIENT_RAW}/power/win_power.ps1' -OutFile "$dir\\win_power.ps1"
}} finally {{ Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }}
@{{ psu = '{psu}' }} | ConvertTo-Json | Set-Content -Path "$data\\power-config.json" -Encoding ASCII
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$dir\\win_power.ps1`""
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Stop-ScheduledTask -TaskName '{WIN_TASK}' -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName '{WIN_TASK}' -Description 'DeviceManager power estimate' -Action $action `
  -Trigger (New-ScheduledTaskTrigger -AtStartup) -Principal $principal -Settings $settings -Force | Out-Null
$out = "$data\\power.json"
Remove-Item $out -Force -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName '{WIN_TASK}'
for ($i = 0; $i -lt 40; $i++) {{
  Start-Sleep -Seconds 1
  if (Test-Path $out) {{
    $j = Get-Content $out -Raw | ConvertFrom-Json
    $note = if (-not $j.cpu_measured) {{ ' (CPU power not readable yet; a restart may be needed to load the PawnIO driver)' }} else {{ '' }}
    Write-Output "Power tracking is running: $($j.wall_w) W$note"
    exit 0
  }}
}}
Write-Output 'Installed and started, but no reading yet.'
exit 1
"""


WINDOWS_DISABLE_SCRIPT = f"""Stop-ScheduledTask -TaskName '{WIN_TASK}' -ErrorAction SilentlyContinue
Disable-ScheduledTask -TaskName '{WIN_TASK}' -ErrorAction SilentlyContinue | Out-Null
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
  Where-Object {{ $_.CommandLine -like '*win_power.ps1*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}
Remove-Item (Join-Path $env:ProgramData 'DeviceManagerClient\\power.json') -Force -ErrorAction SilentlyContinue
Write-Output 'Power tracking stopped: the DeviceManagerPower task is stopped and disabled.'
exit 0
"""


@router.post("/devices/{device_id}/power/{action}")
async def set_power_tracking(device_id: int, action: str, psu: str = "generic-gold"):
    if action not in ("enable", "disable"):
        return JSONResponse({"detail": "Unknown action"}, status_code=400)
    if psu not in dict(remote.PSU_PRESETS):
        return JSONResponse({"detail": "Unknown power supply"}, status_code=400)
    with Session(engine) as s:
        device = s.get(Device, device_id)
    if not device:
        return JSONResponse({"detail": "Unknown device"}, status_code=404)
    if not device.ssh_enabled:
        return JSONResponse({"detail": "Enable SSH for this device first (Settings tab)."}, status_code=400)
    if remote.is_windows(device):
        script = windows_enable_script(psu) if action == "enable" else WINDOWS_DISABLE_SCRIPT
        cmd, timeout = remote.powershell(script), 300  # first install downloads ~10 MB
    else:
        cmd, timeout = f"({ENABLE_CMD if action == 'enable' else DISABLE_CMD}) 2>&1", 60
    code, out = await _ssh_run(device, remote.update_user(device), cmd, timeout=timeout)
    if code is None:
        return JSONResponse({"detail": out}, status_code=502)
    if code != 0:
        return JSONResponse({"detail": out.strip()[-400:] or f"exit {code}"}, status_code=400 if code == NO_RAPL_EXIT else 502)
    return {"ok": True, "message": out.strip().splitlines()[-1] if out.strip() else "Done"}
