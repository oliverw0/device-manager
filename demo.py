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
    request.session["is_admin"] = True  # act as a logged-in admin (nav, logout, ssh buttons)
    return await call_next(request)


auth_middleware.AdminAuthMiddleware.dispatch = _no_auth

from app.database import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AlertEvent, ContainerHistory, Device, ReportHistory, ServiceCheck  # noqa: E402
from sqlmodel import Session, delete  # noqa: E402

GIB = 1073741824
NOW = datetime.utcnow()


def report(hostname, cpu, mem, disk, cores, mem_total_g, disk_total_g, uptime_s,
           tailscale=None, ssh_auth=None, containers=None, apt=None, ssh_users=None):
    return {
        "system": {
            "hostname": hostname,
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
        "agent_version": "1.0.0",
    }


def container(name, image, running=True, cpu=None, mem=None, stack=None):
    return {
        "name": name, "image": image,
        "status": "running" if running else "exited (1) 3 hours ago",
        "started_at": (NOW - timedelta(days=2)).isoformat(),
        "stack": stack, "cpu_percent": cpu, "mem_percent": mem,
    }


# --- template devices ------------------------------------------------------
DEVICES = [
    dict(
        name="web-vm-01", notes="Primary reverse proxy + app front end", online=True,
        ssh_enabled=True, apt_needs_update=False, tailscale_connected=True,
        # disk history climbs over 10 days so the "days to full" projection kicks in
        disk_trend=(52.0, 67.0, 10, 120),
        report=report(
            "web-vm-01", 34.0, 61.0, 67.0, 4, 8, 60, 14 * 86400 + 3 * 3600,
            tailscale={"connected": True, "backend_state": "Running",
                       "ips": ["100.101.102.103"], "dns_name": "web-vm-01.tuna-cod.ts.net",
                       "tailnet": "tuna-cod.ts.net", "exit_node": None},
            ssh_auth={"recent_accepted_count": 3, "recent_failed_count": 0,
                      "last_accepted_user": "deploy", "last_accepted_ip": "100.64.0.5",
                      "last_accepted_at": NOW.isoformat(), "last_failed_ip": None},
            containers=[
                container("caddy", "caddy:2", cpu=1.2, mem=3.4, stack="edge"),
                container("app", "ghcr.io/acme/app:latest", cpu=12.5, mem=22.0, stack="edge"),
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
            "db-vm-01", 78.0, 84.0, 91.0, 8, 32, 500, 92 * 86400,
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
            tailscale={"connected": False, "backend_state": "Stopped", "ips": [],
                       "dns_name": None, "tailnet": "tuna-cod.ts.net"},
            ssh_auth={"recent_accepted_count": 0, "recent_failed_count": 0,
                      "last_accepted_user": None, "last_failed_ip": None},
            containers=[
                container("homeassistant", "ghcr.io/home-assistant/home-assistant:stable", cpu=6.0, mem=18.0),
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
            "backup-nas", 0.0, 12.0, 66.0, 2, 8, 4000, 40 * 86400,
            tailscale={"backend_state": "not_installed", "connected": False, "ips": []},
            ssh_auth={"recent_accepted_count": 0, "recent_failed_count": 0},
            containers=[],
            apt={"available": False},
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
        for t in (AlertEvent, ContainerHistory, ReportHistory, ServiceCheck, Device):
            s.exec(delete(t))
        s.commit()

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

            # history: default 60 points over 5h wandering around the current value;
            # a disk_trend device instead climbs linearly over N days for the projection.
            sys = rep["system"]
            jit = lambda base: max(0.0, min(100.0, base + random.uniform(-8, 8)))
            trend = d.get("disk_trend")
            if trend:
                start, end, days, points = trend
                for i in range(points):
                    ts = NOW - timedelta(days=days * (points - 1 - i) / (points - 1))
                    disk = start + (end - start) * i / (points - 1) + random.uniform(-0.6, 0.6)
                    s.add(ReportHistory(device_id=dev.id, timestamp=ts,
                                        cpu_percent=jit(sys["cpu_percent"]),
                                        mem_percent=jit(sys["mem_percent"]),
                                        disk_percent=max(0.0, min(100.0, disk))))
            else:
                for i in range(60):
                    ts = NOW - timedelta(minutes=(60 - i) * 5)
                    s.add(ReportHistory(device_id=dev.id, timestamp=ts,
                                        cpu_percent=jit(sys["cpu_percent"]),
                                        mem_percent=jit(sys["mem_percent"]),
                                        disk_percent=jit(sys["disk_percent"])))
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
        s.commit()


if __name__ == "__main__":
    seed()
    import uvicorn
    print("\n  Demo UI:  http://127.0.0.1:8000   (auth bypassed, fake data)\n")
    uvicorn.run(app, host="127.0.0.1", port=8000)
