"""Local UI preview with fake data — no client, no real DB, no login.

Run from the host/ folder:

    pip install -r requirements.txt   # once
    python demo.py

Then open http://127.0.0.1:8000 . Seeds a throwaway data/demo.db (wiped on
every run) with template devices so you can eyeball design changes before
pushing to the real host. Auth is bypassed here; the real app still requires it.
"""

import json
import os
import random
from datetime import datetime, timedelta
from pathlib import Path

# Must happen before importing anything that reads settings/engine.
os.chdir(Path(__file__).parent)
os.environ["DATABASE_URL"] = "sqlite:///./data/demo.db"

# Bypass the login wall for the preview. Patch the class before app import so the
# middleware instance main.py constructs picks up the no-op dispatch.
from app import auth_middleware  # noqa: E402


async def _no_auth(self, request, call_next):
    if "session" in request.scope:  # static files skip the session middleware
        request.session["is_admin"] = True  # act as a logged-in admin (nav, logout, ssh buttons)
    return await call_next(request)


auth_middleware.AdminAuthMiddleware.dispatch = _no_auth

from app import monitor  # noqa: E402


async def _no_monitor():  # keep the seeded state: no sweeps marking fake devices offline or running fake checks
    return None


monitor.run_forever = _no_monitor

from app.database import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (AlertEvent, CheckDay, ContainerHistory, Device, HourlyHistory, NotificationLog, Problem,  # noqa: E402
                        ReportHistory, ServiceCheck, User)
from sqlmodel import Session, delete, select  # noqa: E402

GIB = 1073741824
NOW = datetime.utcnow()


def report(hostname, cpu, mem, disk, cores, mem_total_g, disk_total_g, uptime_s,
           tailscale=None, ssh_auth=None, containers=None, apt=None, ssh_users=None,
           os="linux", os_version="Ubuntu 24.04.1 LTS", disks=None, agent_version="1.1.0", power=None):
    return {
        "system": {
            "hostname": hostname, "os": os, "os_version": os_version, "disks": disks or [],
            "cpu_percent": cpu, "cpu_cores": cores,
            "mem_percent": mem, "mem_used_bytes": int(mem_total_g * GIB * mem / 100),
            "mem_total_bytes": mem_total_g * GIB,
            "disk_percent": disk, "disk_used_bytes": int(disk_total_g * GIB * disk / 100),
            "disk_total_bytes": disk_total_g * GIB,
            "uptime_seconds": uptime_s, "local_ip": "192.168.1." + str(random.randint(10, 99)),
        },
        "tailscale": tailscale,
        "ssh_auth": ssh_auth,
        "docker_containers": containers or [],
        "ssh_users": ssh_users or ["root", "deploy"],
        "apt": apt,
        "agent_version": agent_version,
        "power": power,
    }


def disk(mount, label, percent, total_g):
    return {"mount": mount, "label": label, "fstype": "NTFS" if ":" in mount else "ext4", "percent": percent,
            "used_bytes": int(total_g * GIB * percent / 100), "total_bytes": total_g * GIB}


def container(name, image, running=True, cpu=None, mem=None, stack=None, health=None, restarts=0, ports=()):
    return {
        "name": name, "image": image,
        "status": "running" if running else "exited (1) 3 hours ago",
        "started_at": (NOW - timedelta(days=2)).isoformat(),
        "stack": stack, "cpu_percent": cpu, "mem_percent": mem,
        "health": health, "restart_count": restarts, "ports": list(ports),
    }


# --- template devices ------------------------------------------------------
DEVICES = [
    dict(
        name="web-vm-01", notes="Primary reverse proxy + app front end", online=True,
        ssh_enabled=True, apt_needs_update=False, tailscale_connected=True,
        report=report(
            "web-vm-01", 34.0, 61.0, 67.0, 4, 8, 60, 14 * 86400 + 3 * 3600,
            tailscale={"connected": True, "backend_state": "Running",
                       "ips": ["100.101.102.103"], "dns_name": "web-vm-01.tuna-cod.ts.net",
                       "tailnet": "tuna-cod.ts.net", "exit_node": None},
            ssh_auth={"recent_accepted_count": 3, "recent_failed_count": 0,
                      "last_accepted_user": "deploy", "last_accepted_ip": "100.64.0.5",
                      "last_accepted_at": NOW.isoformat(), "last_failed_ip": None},
            containers=[
                container("caddy", "caddy:2", cpu=1.2, mem=3.4, stack="edge", ports=(80, 443)),
                container("app", "ghcr.io/acme/app:latest", cpu=12.5, mem=22.0, stack="edge", health="unhealthy", ports=(8080,)),
                container("redis", "redis:7", cpu=0.4, mem=1.1, stack="edge"),
                container("watchtower", "containrrr/watchtower", cpu=0.1, mem=0.6),
            ],
            apt={"available": True, "last_update_age_seconds": 2 * 86400, "upgradable": 4},
        ),
    ),
    dict(
        name="db-vm-01", notes="Postgres primary", online=True,
        ssh_enabled=True, apt_needs_update=True, tailscale_connected=True,
        report=report(
            "db-vm-01", 78.0, 84.0, 91.0, 8, 32, 500, 92 * 86400, os_version="Debian GNU/Linux 12 (bookworm)", agent_version="1.0.0",
            disks=[disk("/", None, 91.0, 500), disk("/var/lib/postgresql", None, 58.0, 2000)],
            tailscale={"connected": True, "backend_state": "Running",
                       "ips": ["100.101.102.104"], "dns_name": "db-vm-01.tuna-cod.ts.net",
                       "tailnet": "tuna-cod.ts.net", "exit_node": None},
            ssh_auth={"recent_accepted_count": 1, "recent_failed_count": 17,
                      "last_accepted_user": "root", "last_accepted_ip": "100.64.0.5",
                      "last_accepted_at": NOW.isoformat(), "last_failed_ip": "185.23.11.9"},
            containers=[container("postgres", "postgres:16", cpu=41.0, mem=63.0)],
            apt={"available": True, "last_update_age_seconds": 45 * 86400, "upgradable": 72},
        ),
    ),
    dict(
        name="pi-livingroom", notes="Raspberry Pi — Home Assistant", online=True,
        ssh_enabled=False, apt_needs_update=False, tailscale_connected=False,
        report=report(
            "homeassistant", 8.0, 39.0, 22.0, 4, 4, 32, 6 * 86400,
            os_version="Raspbian GNU/Linux 12 (bookworm)",
            tailscale={"connected": False, "backend_state": "Stopped", "ips": [],
                       "dns_name": None, "tailnet": "tuna-cod.ts.net"},
            ssh_auth={"recent_accepted_count": 0, "recent_failed_count": 0,
                      "last_accepted_user": None, "last_failed_ip": None},
            containers=[
                container("homeassistant", "ghcr.io/home-assistant/home-assistant:stable", cpu=6.0, mem=18.0, health="healthy", ports=(8123,)),
                container("mosquitto", "eclipse-mosquitto:2", running=False),
            ],
            apt={"available": True, "last_update_age_seconds": 5 * 86400, "upgradable": 2},
        ),
    ),
    dict(
        name="backup-nas", notes="Offline — last seen before maintenance window", online=False,
        ssh_enabled=False, apt_needs_update=False, tailscale_connected=None,
        last_seen_override=NOW - timedelta(hours=6),
        report=report(
            "backup-nas", 0.0, 12.0, 66.0, 2, 8, 4000, 40 * 86400, os_version="Arch Linux", agent_version="1.0.0",
            tailscale={"backend_state": "not_installed", "connected": False, "ips": []},
            ssh_auth={"recent_accepted_count": 0, "recent_failed_count": 0},
            containers=[],
            apt={"available": False},
        ),
    ),
    dict(
        name="proxmox-host", notes="Proxmox VE, i5-12600K", online=True,
        ssh_enabled=True, apt_needs_update=False, tailscale_connected=True,
        report=report(
            "pve", 5.0, 41.0, 38.0, 16, 32, 238, 21 * 86400, os_version="Debian GNU/Linux 12 (bookworm)",
            ssh_users=["root"], agent_version="1.1.9",
            # a real pve-power reading; web-vm-01 and db-vm-01 stand in as its LXCs
            power={"wall_w": 50.4, "energy_kwh": 12.84, "dc_load_w": 41.8, "psu_efficiency": 0.829,
                   "components": {"cpu_package": 17.15, "vrm_loss": 2.34, "ram": 3.93, "drive_nvme0n1": 0.5, "drive_sda": 5.48,
                                  "motherboard": 6.0, "nic_2_5gbe": 1.0, "wifi": 0.3, "cpu_cooler_fan": 1.5, "case_fans": 3.6,
                                  "extras": 0.0, "psu_loss": 8.63},
                   "guests": [{"name": "web-vm-01", "type": "lxc", "vmid": "110", "cpu_w": 17.31},
                              {"name": "db-vm-01", "type": "lxc", "vmid": "103", "cpu_w": 1.89},
                              {"name": "servarr", "type": "lxc", "vmid": "114", "cpu_w": 1.01}]},
            tailscale={"connected": True, "backend_state": "Running", "ips": ["100.101.102.106"],
                       "dns_name": "pve.tuna-cod.ts.net", "tailnet": "tuna-cod.ts.net", "exit_node": None},
        ),
    ),
    dict(
        name="gaming-pc", notes="Windows desktop", online=True,
        ssh_enabled=True, apt_needs_update=False, tailscale_connected=True,
        report=report(
            "OLIVE-PC", 18.0, 47.0, 93.3, 16, 32, 930, 3 * 86400,
            os="windows", os_version="Windows 11 Home (25H2)",
            disks=[disk("C:\\", None, 93.3, 930), disk("D:\\", "BIG SSD", 33.8, 1863),
                   disk("F:\\", "SSD 3", 59.6, 931), disk("S:\\", "SSD", 70.8, 477)],
            tailscale={"connected": True, "backend_state": "Running",
                       "ips": ["100.101.102.105"], "dns_name": "olive-pc.tuna-cod.ts.net",
                       "tailnet": "tuna-cod.ts.net", "exit_node": None},
            ssh_auth={"recent_accepted_count": 0, "recent_failed_count": 0},
            apt={"available": False}, ssh_users=["olive"],
        ),
    ),
]

ALERTS = [
    ("db-vm-01", "apt_stale", "72 packages upgradable"),
    ("db-vm-01", "disk_high", "Disk at 91% (over 90% for 10m)"),
    ("db-vm-01", "service_down", "postgres TCP is DOWN"),
    ("backup-nas", "offline", "No report for 6h"),
    ("web-vm-01", "online", "Back online after 2m"),
    ("db-vm-01", "tailscale_up", "Tailscale reconnected"),
]

# device, check name, kind, target, last_status, last_latency_ms, last_error
CHECKS = [
    ("web-vm-01", "app HTTP", "http", "http://100.101.102.103:8080", "up", 42.3, None),
    ("web-vm-01", "redis TCP", "tcp", "100.101.102.103:6379", "up", 3.1, None),
    ("db-vm-01", "postgres TCP", "tcp", "100.101.102.104:5432", "down", None, "[Errno 111] Connect call failed"),
]


def seed():
    init_db()
    with Session(engine) as s:
        for t in (AlertEvent, CheckDay, ContainerHistory, HourlyHistory, NotificationLog, Problem, ReportHistory, ServiceCheck, Device, User):
            s.exec(delete(t))
        s.commit()

        s.add(User(username="admin", source="local", created_at=NOW - timedelta(days=40), last_login_at=NOW - timedelta(days=3)))
        s.add(User(username="oliver", sub="demo-1", source="oidc", email="oliver@example.com", name="Oliver",
                   created_at=NOW - timedelta(days=2), last_login_at=NOW - timedelta(minutes=5)))
        s.add(User(username="guest", sub="demo-2", source="oidc", email="guest@example.com", disabled=True,
                   created_at=NOW - timedelta(days=1), last_login_at=NOW - timedelta(hours=20)))
        by_name = {}
        for d in DEVICES:
            rep = d["report"]
            last_seen = d.get("last_seen_override", NOW - timedelta(seconds=random.randint(2, 40)))
            dev = Device(
                name=d["name"], notes=d["notes"], is_online=d["online"],
                last_seen_at=last_seen, last_report_json=json.dumps(rep),
                last_report_ip=rep["system"]["local_ip"],
                ssh_enabled=d["ssh_enabled"], apt_needs_update=d["apt_needs_update"],
                tailscale_connected=d["tailscale_connected"],
            )
            s.add(dev)
            s.commit()
            s.refresh(dev)
            by_name[dev.name] = dev

            # history: 60 points over 5h wandering around the current value;
            # proxmox-host was off for 40 minutes, so its graphs show a gap
            sys = rep["system"]
            jit = lambda base: max(0.0, min(100.0, base + random.uniform(-8, 8)))
            for i in range(60):
                if rep.get("power") and 44 <= i < 52:
                    continue
                ts = NOW - timedelta(minutes=(60 - i) * 5)
                s.add(ReportHistory(device_id=dev.id, timestamp=ts,
                                    cpu_percent=jit(sys["cpu_percent"]),
                                    mem_percent=jit(sys["mem_percent"]),
                                    disk_percent=jit(sys["disk_percent"]),
                                    power_w=(rep["power"]["wall_w"] + random.uniform(-6, 14)) if rep.get("power") else None))
            for i in range(60):
                ts = NOW - timedelta(minutes=(60 - i) * 5)
                for c in rep["docker_containers"]:
                    if c["cpu_percent"] is None:
                        continue
                    s.add(ContainerHistory(device_id=dev.id, container_name=c["name"], timestamp=ts,
                                           cpu_percent=jit(c["cpu_percent"]),
                                           mem_percent=jit(c["mem_percent"] or 0)))
        s.commit()

        for i, (name, kind, msg) in enumerate(ALERTS):
            dev = by_name[name]
            s.add(AlertEvent(device_id=dev.id, device_name=name, kind=kind, message=msg,
                             created_at=NOW - timedelta(minutes=i * 37)))
        for name, cname, kind, target, status, latency, error in CHECKS:
            s.add(ServiceCheck(device_id=by_name[name].id, name=cname, kind=kind, target=target,
                               last_status=status, last_latency_ms=latency, last_error=error,
                               last_checked_at=NOW - timedelta(seconds=20)))
        # heartbeats: one on time, one missed; a site whose certificate is close to expiry
        s.add(ServiceCheck(device_id=by_name["db-vm-01"].id, name="nightly-backup", kind="heartbeat",
                           interval_seconds=86400, grace_seconds=900, token="demo-nightly", last_status="down",
                           last_checked_at=NOW - timedelta(hours=27)))
        s.add(ServiceCheck(device_id=by_name["web-vm-01"].id, name="cert renew", kind="heartbeat",
                           interval_seconds=7 * 86400, grace_seconds=3600, token="demo-cert", last_status="up",
                           last_checked_at=NOW - timedelta(days=2)))
        s.add(ServiceCheck(device_id=by_name["web-vm-01"].id, name="site", kind="http", target="https://example.com",
                           last_status="up", last_latency_ms=88.0, last_checked_at=NOW,
                           cert_expires_at=NOW + timedelta(days=9), cert_checked_at=NOW))
        s.commit()
        # 30 days of uptime with a few bad days, and some problems that already cleared
        # checks for the Status page: DNS, Proxmox and PBS with findings
        pve, nas = by_name["proxmox-host"].id, by_name["backup-nas"].id
        s.add(ServiceCheck(device_id=pve, name="adguard primary", kind="dns", target="pulse.wiles.au@192.168.50.146",
                           last_status="up", last_latency_ms=4.0, last_checked_at=NOW))
        s.add(ServiceCheck(device_id=pve, name="adguard secondary", kind="dns", target="pulse.wiles.au@192.168.50.225",
                           last_status="up", last_latency_ms=6.0, last_checked_at=NOW))
        s.add(ServiceCheck(device_id=pve, name="authentik", kind="http", target="https://auth.wiles.au", expect="authentik",
                           group="Auth", last_status="up", last_latency_ms=120.0, last_checked_at=NOW))
        s.add(ServiceCheck(device_id=pve, name="pve", kind="proxmox", target="https://192.168.50.30:8006", secret="x",
                           interval_seconds=300, last_status="down", last_checked_at=NOW,
                           summary="9/11 guests running, fullest storage 95%, 1 stopped that should run",
                           last_error="Storage local-lvm is 95% full; adguard (105) is stopped",
                           issues=json.dumps([
                               {"key": "storage:pve/local-lvm", "severity": "high", "title": "Storage local-lvm is 95% full",
                                "detail": "lvmthin on pve: 7 GiB left of 141 GiB."},
                               {"key": "guest:pve:105", "severity": "warning", "title": "adguard (105) is stopped",
                                "detail": "Set to start at boot on pve, but not running."}])))
        s.add(ServiceCheck(device_id=nas, name="pbs", kind="pbs", target="https://192.168.50.81:8007", secret="x",
                           interval_seconds=300, last_status="down", last_checked_at=NOW,
                           summary="9 backups, 1 GC, all OK; 1 guest late",
                           issues=json.dumps([{"key": "late:nas:ct/101", "severity": "warning",
                                               "title": "authentik: last backup 31h ago", "detail": "ct/101 on datastore nas."}])))
        s.commit()
        for c in s.exec(select(ServiceCheck)).all():
            for i in range(30):
                bad = random.random() < 0.08
                s.add(CheckDay(check_id=c.id, day=(NOW - timedelta(days=i)).strftime("%Y-%m-%d"),
                               ok=random.randint(1300, 1400) if bad else 1440, total=1440))
        for name, key, sev, src, title, opened, lasted in [
            ("web-vm-01", "offline", "high", "Device", "Offline", 9, 4),
            ("db-vm-01", "container:pgadmin", "warning", "Container", "pgadmin exited", 14, 6),
            ("gaming-pc", "disk:C:\\", "high", "Disk C:", "C: is 93% full", 30, 27 * 60),
        ]:
            s.add(Problem(device_id=by_name[name].id, key=key, severity=sev, source=src, title=title,
                          opened_at=NOW - timedelta(hours=opened, minutes=lasted),
                          resolved_at=NOW - timedelta(hours=opened)))
        # a week of incidents for the Timeline
        for name, key, sev, title, hours_ago, minutes in [
            ("proxmox-host", "offline", "high", "Offline", 70, 25), ("web-vm-01", "offline", "high", "Offline", 70, 26),
            ("db-vm-01", "cpu", "warning", "CPU high", 50, 95), ("pi-livingroom", "container:mosquitto", "warning", "mosquitto exited", 120, 300),
            ("db-vm-01", "check:1", "high", "postgres TCP is down", 26, 12), ("gaming-pc", "restarts:steam", "high", "steam keeps restarting", 100, 40),
            ("web-vm-01", "health:app", "warning", "app unhealthy", 3, 0),
        ]:
            s.add(Problem(device_id=by_name[name].id, key=key, severity=sev, source="Demo", title=title,
                          opened_at=NOW - timedelta(hours=hours_ago, minutes=minutes),
                          resolved_at=None if minutes == 0 else NOW - timedelta(hours=hours_ago)))
        # pushes for Sent alerts, one that failed
        for i, (title, msg, prio, ok) in enumerate([
            ("Container unhealthy", "web-vm-01: app unhealthy. Running, but its Docker healthcheck is failing.", "default", True),
            ("Proxmox", "proxmox-host: pve: Storage local-lvm is 95% full. lvmthin on pve: 7 GiB left of 141 GiB.", "high", True),
            ("Daily summary", "Backups\n· pbs: 9 backups, 1 GC, all OK", "default", False),
            ("Device offline", "proxmox-host stopped responding. Its guests are unreachable too: db-vm-01, web-vm-01", "high", True),
        ]):
            s.add(NotificationLog(created_at=NOW - timedelta(hours=i * 9 + 1), title=title, message=msg, priority=prio, ok=ok,
                                  error="" if ok else "ntfy answered 502: Bad Gateway"))
        # 30 days of hourly averages for the long charts
        for h in range(30 * 24):
            hour = (NOW - timedelta(hours=h)).replace(minute=0, second=0, microsecond=0)
            for dev in by_name.values():
                sys = json.loads(dev.last_report_json)["system"]
                wave = 10 * __import__("math").sin(h / 24 * 6.283)
                s.add(HourlyHistory(device_id=dev.id, hour=hour, samples=12,
                                    cpu_percent=max(0, min(100, sys["cpu_percent"] + wave + random.uniform(-4, 4))),
                                    mem_percent=max(0, min(100, sys["mem_percent"] + random.uniform(-3, 3))),
                                    disk_percent=max(0, sys["disk_percent"] - h / 100)))
        s.commit()


if __name__ == "__main__":
    seed()
    from app import problems as _problems
    with Session(engine) as _s:
        _problems.sync(_s)  # open problems from the seeded state (the monitor loop is off here)
    import uvicorn
    print("\n  Demo UI:  http://127.0.0.1:8000   (auth bypassed, fake data)\n")
    uvicorn.run(app, host="127.0.0.1", port=8000)
