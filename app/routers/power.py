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
DISABLE_CMD = """systemctl disable --now pve-power 2>/dev/null || true
rm -f /etc/systemd/system/pve-power.service
systemctl daemon-reload
echo "Power tracking stopped. /usr/local/bin/pve_power.py is left in place (with any calibration)."
"""


@router.post("/devices/{device_id}/power/{action}")
async def set_power_tracking(device_id: int, action: str):
    if action not in ("enable", "disable"):
        return JSONResponse({"detail": "Unknown action"}, status_code=400)
    with Session(engine) as s:
        device = s.get(Device, device_id)
    if not device:
        return JSONResponse({"detail": "Unknown device"}, status_code=404)
    if remote.is_windows(device):
        return JSONResponse({"detail": "Power tracking needs Linux (it reads the CPU's RAPL counters)."}, status_code=400)
    if not device.ssh_enabled:
        return JSONResponse({"detail": "Enable SSH for this device first (Settings tab)."}, status_code=400)
    cmd = ENABLE_CMD if action == "enable" else DISABLE_CMD
    code, out = await _ssh_run(device, remote.update_user(device), f"({cmd}) 2>&1", timeout=60)
    if code is None:
        return JSONResponse({"detail": out}, status_code=502)
    if code != 0:
        return JSONResponse({"detail": out.strip()[-400:] or f"exit {code}"}, status_code=400 if code == NO_RAPL_EXIT else 502)
    return {"ok": True, "message": out.strip().splitlines()[-1] if out.strip() else "Done"}
