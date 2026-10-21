"""Self-check for the add-device dialog's POST. Run from host/:  python test_add_device.py

Covers what the form relies on: SSH settings saved at creation, the platform
carried to the detail page, and bad input re-opening the dialog (400) instead
of a 500 from the unique name constraint.
"""
import os
import tempfile
os.environ.update(ADMIN_USERNAME="admin", ADMIN_PASSWORD="hunter2", SESSION_HTTPS_ONLY="false",
                  DATABASE_URL=f"sqlite:///{tempfile.mkdtemp()}/t.db")

from fastapi.testclient import TestClient
from sqlmodel import Session, select
from app.database import engine
from app.main import app
from app.models import Device

form = {"name": "win-01", "platform": "windows", "report_interval_seconds": "60",
        "offline_after_seconds": "150", "ssh_enabled": "true", "ssh_port": "2222"}

with TestClient(app, base_url="http://testserver") as c:
    c.post("/login", data={"username": "admin", "password": "hunter2", "next": "/"})

    r = c.post("/devices", data=form, follow_redirects=False)
    assert r.status_code == 303 and "platform=windows" in r.headers["location"], r.headers.get("location")
    with Session(engine) as s:
        d = s.exec(select(Device).where(Device.name == "win-01")).one()
        assert d.ssh_enabled and d.ssh_port == 2222 and d.ssh_host is None

    r = c.post("/devices", data=form)
    assert r.status_code == 400 and "already exists" in r.text, r.status_code

    r = c.post("/devices", data={**form, "name": "x", "offline_after_seconds": "30"})
    assert r.status_code == 400 and "longer than the report interval" in r.text, r.status_code

from app.routers.dashboard import os_key
from app.schemas import SystemInfo
assert os_key({"os": "windows"}) == "windows"
assert os_key({"os": "linux", "os_version": "Raspbian GNU/Linux 12"}) == "raspbian"
assert os_key({"os": "linux", "os_version": "Ubuntu 24.04 LTS"}) == "ubuntu"
assert os_key({"os": "linux", "os_version": "Alpine Linux"}) == "linux"
assert os_key({"os": "linux", "os_version": "Fedora Linux Asahi Remix 40 (Forty)"}) == "asahi"
# mac-deb: Debian on an M1, seen by the Docker client (kernel only) and natively (model too)
assert os_key({"os": "linux", "os_version": "Debian GNU/Linux 13 (trixie)", "kernel": "6.17.9+deb13-asahi"}) == "asahi"
assert os_key({"os": "linux", "os_version": "Debian GNU/Linux 13 (trixie)", "hardware_model": "Apple MacBook Air (M1, 2020)"}) == "asahi"
assert os_key({"os": "linux", "os_version": "Debian GNU/Linux 13 (trixie)", "kernel": "6.12.48+deb13-amd64"}) == "debian"
# Wiles-NAS: DSM version from /etc.defaults/VERSION, or just the model from synoinfo.conf
assert os_key({"os": "linux", "os_version": "DSM 7.3.1-86003 Update 1"}) == "synology"
assert os_key({"os": "linux", "os_version": "Debian GNU/Linux 12", "hardware_model": "Synology DS916+"}) == "synology"
assert os_key({"hostname": "old-client"}) == "linux"  # pre-os clients are Linux: Tux
assert os_key(None) is None  # never reported: no icon
sysinfo = SystemInfo(hostname="h", cpu_percent=1, mem_percent=1, disk_percent=1, uptime_seconds=1,
                     os="windows", disks=[{"mount": "D:\\", "label": "BIG SSD", "percent": 33.8}])
assert sysinfo.disks[0].label == "BIG SSD" and sysinfo.os == "windows"  # not dropped on ingest

print("add-device self-checks passed")
