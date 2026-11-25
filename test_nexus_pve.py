"""Self-check for NEXUS's Resources panel (pve, the metal). Run from host/:  python test_nexus_pve.py

Covers: no pve in Pulse gives None; the latest report's CPU, memory and power come through; the EFI partition
is left out of the drives; history is only the last 24 h, oldest first, in unix seconds, with power kept as None
when it wasn't measured.
"""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

DB = Path("data/test_nexus_pve.db")
DB.unlink(missing_ok=True)
os.environ.update(DATABASE_URL=f"sqlite:///./{DB.as_posix()}")

from sqlmodel import Session  # noqa: E402

from app.database import engine, init_db  # noqa: E402
from app.models import Device, ReportHistory  # noqa: E402
from app.routers.nexus import pve_metal  # noqa: E402

init_db()
NOW = datetime(2026, 11, 25, 12, 0)
GB = 10**9

with Session(engine) as s:
    assert pve_metal(s, NOW) is None, "no pve yet"
    report = {"system": {"cpu_percent": 6.9, "cpu_cores": 16, "mem_percent": 43.4, "mem_used_bytes": 14 * GB, "mem_total_bytes": 33 * GB,
                         "uptime_seconds": 86400, "disks": [
                             {"mount": "/", "percent": 48.6, "used_bytes": 35 * GB, "total_bytes": 72 * GB},
                             {"mount": "/boot/efi", "percent": 0.9, "used_bytes": 10**7, "total_bytes": GB},
                             {"mount": "/mnt/hdd2tb", "percent": 79.8, "used_bytes": 1570 * GB, "total_bytes": 1968 * GB}]},
              "power": {"wall_w": 57.3}}
    d = Device(name="proxmox-host", is_online=True, last_seen_at=NOW, last_report_json=json.dumps(report))
    s.add(d)
    s.commit()
    s.refresh(d)
    s.add(ReportHistory(device_id=d.id, timestamp=NOW - timedelta(hours=30), cpu_percent=99, mem_percent=99))  # too old
    s.add(ReportHistory(device_id=d.id, timestamp=NOW - timedelta(minutes=1), cpu_percent=5.04, mem_percent=43.7, power_w=53.44))
    s.add(ReportHistory(device_id=d.id, timestamp=NOW - timedelta(minutes=2), cpu_percent=19.0, mem_percent=43.2))
    s.commit()

    v = pve_metal(s, NOW)
    assert v["cpu"] == 6.9 and v["cores"] == 16 and v["watts"] == 57.3 and v["online"]
    assert v["mem"] == {"pct": 43.4, "used": 14 * GB, "total": 33 * GB}
    assert [x["mount"] for x in v["drives"]] == ["/", "/mnt/hdd2tb"], v["drives"]
    t = int((NOW - datetime(1970, 1, 1)).total_seconds())
    assert v["history"] == [[t - 120, 19.0, 43.2, None], [t - 60, 5.0, 43.7, 53.4]], v["history"]

engine.dispose()  # Windows won't delete an open file
DB.unlink(missing_ok=True)
print("nexus pve self-checks passed")
