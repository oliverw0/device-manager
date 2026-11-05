"""Self-check for the monitoring additions. Run from host/:  python test_monitoring.py

Grouped offline alerts, restart loops and unhealthy containers, SSH alerts,
PBS/Proxmox findings as problems, reminders, hourly rollups, the ntfy buttons,
/metrics, and that every new page renders.
"""
import json
import os
import tempfile
from datetime import datetime, timedelta

os.environ.update(ADMIN_USERNAME="admin", ADMIN_PASSWORD="hunter2", SESSION_HTTPS_ONLY="false",
                  DATABASE_URL=f"sqlite:///{tempfile.mkdtemp()}/t.db", NTFY_URL="http://127.0.0.1:9/topic",
                  PUBLIC_URL="https://pulse.example", METRICS_TOKEN="m-token", REMINDER_HOURS="4")
os.chdir(tempfile.mkdtemp())  # data/ (storm.json, stamps) goes here
os.symlink(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"), "app")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import monitor, notifier, problems  # noqa: E402
from app.database import engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Device, HourlyHistory, NotificationLog, Problem, ReportHistory, ServiceCheck  # noqa: E402
from app.routers import hooks  # noqa: E402

SENT = []
real_send = notifier.send
notifier.send = lambda message, **kw: SENT.append((message, kw)) or True


async def _idle():
    return None


monitor.run_forever = _idle  # no background sweeps during the test


def report(c, key, **extra):
    body = {"system": {"hostname": extra.pop("hostname", "h"), "cpu_percent": 5, "mem_percent": 20, "disk_percent": 30,
                       "uptime_seconds": 1000, "disks": [{"mount": "/", "percent": 30}]}, "agent_version": "1.3.0", **extra}
    r = c.post("/api/v1/report", json=body, headers={"X-API-Key": key})
    assert r.status_code == 200, r.text


with TestClient(app, base_url="http://testserver") as c:
    c.post("/login", data={"username": "admin", "password": "hunter2", "next": "/"})
    for name in ("pve", "services", "plex-server", "mac-deb", "nas"):
        assert c.post("/devices", data={"name": name, "report_interval_seconds": "60", "offline_after_seconds": "150"},
                      follow_redirects=False).status_code == 303
    with Session(engine) as s:
        dev = {d.name: d for d in s.exec(select(Device)).all()}
    keys = {n: d.api_key for n, d in dev.items()}
    ids = {n: d.id for n, d in dev.items()}

    # --- reports: guests in pve's power report, containers, ssh ---
    report(c, keys["pve"], power={"wall_w": 40, "guests": [{"name": "services", "vmid": 103}, {"name": "plex-server", "vmid": 109}]})
    report(c, keys["services"], hostname="services", docker_containers=[
        {"name": "mqtt", "image": "eclipse-mosquitto", "status": "running", "restart_count": 2, "ports": [1883]},
        {"name": "web", "image": "nginx", "status": "running", "health": "unhealthy", "restart_count": 0}])
    report(c, keys["services"], hostname="services", docker_containers=[
        {"name": "mqtt", "image": "eclipse-mosquitto", "status": "running", "restart_count": 6, "ports": [1883]}])
    report(c, keys["plex-server"])
    report(c, keys["mac-deb"], ssh_auth={"recent_accepted_count": 1, "last_accepted_user": "root",
                                         "last_accepted_ip": "8.8.4.4", "last_accepted_at": "t1",
                                         "recent_failed_count": 25, "last_failed_ip": "198.51.100.4"})
    report(c, keys["mac-deb"], ssh_auth={"recent_accepted_count": 1, "last_accepted_user": "root",
                                         "last_accepted_ip": "8.8.4.4", "last_accepted_at": "t1",
                                         "recent_failed_count": 25})
    report(c, keys["nas"], ssh_auth={"recent_accepted_count": 1, "last_accepted_ip": "100.101.1.2", "last_accepted_at": "t"})
    titles = [kw.get("title") for _, kw in SENT]
    assert titles.count("SSH login from outside") == 1, titles  # once per login, not every report
    assert titles.count("SSH login failures") == 1, titles
    SENT.clear()

    with Session(engine) as s:
        problems.sync(s)
        open_ = {(p.device_id, p.key): p for p in s.exec(select(Problem)).all()}
    assert (ids["services"], "restarts:mqtt") in open_, open_.keys()
    assert (ids["mac-deb"], "ssh_fail") in open_
    pushed = [kw.get("key") for _, kw in SENT]
    assert "restarts:mqtt" in pushed and all(kw.get("device_id") for _, kw in SENT), SENT
    SENT.clear()

    # --- offline: a host and its guests fold into one push ---
    with Session(engine) as s:
        old = datetime.utcnow() - timedelta(minutes=10)
        for n in ("pve", "services", "plex-server"):
            d = s.get(Device, ids[n]); d.last_seen_at = old; s.add(d)
        s.commit()
        sent = monitor.check_offline_devices(s)
    assert len(sent) == 1 and sent[0].startswith("pve stopped responding") and "plex-server, services" in sent[0], sent
    report(c, keys["services"], hostname="services")  # its recovery is quiet: its outage never pushed
    assert not [m for m, _ in SENT if "services is responding" in m], SENT
    report(c, keys["pve"], power={"wall_w": 40, "guests": [{"name": "services"}, {"name": "plex-server"}]})
    assert any("pve is responding again" in m for m, _ in SENT)
    report(c, keys["plex-server"])
    SENT.clear()

    # --- offline: most of the fleet at once is Pulse's own network ---
    with Session(engine) as s:
        old = datetime.utcnow() - timedelta(minutes=10)
        for d in s.exec(select(Device)).all():
            d.last_seen_at = old; s.add(d)
        s.commit()
        sent = monitor.check_offline_devices(s)
        assert len(sent) == 1 and sent[0].startswith("Pulse lost contact with 5 of 5") or sent[0].startswith("Pulse lost contact with 3 of 3"), sent
        assert monitor.check_storm_over(s) is None
    for n in keys:
        report(c, keys[n], hostname=n)
    assert not [m for m, _ in SENT if "responding again" in m], SENT
    with Session(engine) as s:
        assert monitor.check_storm_over(s).startswith("Contact restored: all 5")
    SENT.clear()

    # --- checks: create each kind, PBS findings become problems ---
    for kind, extra in (("http", {"target": "http://x", "expect": "OK"}), ("dns", {"target": "a.b@10.0.0.1"}),
                        ("proxmox", {"target": "pve.lan", "secret": "root@pam!p=s"}), ("pbs", {"target": "pbs.lan", "secret": "t"})):
        r = c.post(f"/devices/{ids['pve']}/checks", data={"name": kind, "kind": kind, "group": "Core" if kind == "dns" else "", **extra},
                   follow_redirects=False)
        assert r.status_code == 303
    with Session(engine) as s:
        chk = {x.kind: x for x in s.exec(select(ServiceCheck)).all()}
        assert chk["proxmox"].target == "https://pve.lan:8006" and chk["http"].expect == "OK" and chk["dns"].group == "Core"
        chk["pbs"].last_status = "down"
        chk["pbs"].issues = json.dumps([{"key": "late:nas:ct/103", "severity": "high", "title": "services: last backup 30h ago"}])
        s.add(chk["pbs"]); s.commit()
        pbs_id, http_id = chk["pbs"].id, chk["http"].id
        problems.sync(s)
    assert any("pbs: services: last backup 30h ago" in m for m, _ in SENT), SENT
    r = c.post(f"/checks/{http_id}/edit", data={"name": "web", "target": "http://y", "expect": "200", "group": "Media"},
               follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        h = s.get(ServiceCheck, http_id)
        assert (h.name, h.target, h.expect, h.group) == ("web", "http://y", "200", "Media")

    # --- reminders: once per REMINDER_HOURS for unacknowledged high problems ---
    SENT.clear()
    with Session(engine) as s:
        p = s.exec(select(Problem).where(Problem.key == f"check:{pbs_id}:late:nas:ct/103")).one()
        p.opened_at = datetime.utcnow() - timedelta(hours=5); s.add(p); s.commit()
        devs = {d.id: d for d in s.exec(select(Device)).all()}
        assert len(problems.remind(s, devs, datetime.utcnow())) >= 1
        assert problems.remind(s, devs, datetime.utcnow()) == []

    # --- ntfy buttons ---
    url = hooks.action_url("ack", ids["pve"], f"check:{pbs_id}:late:nas:ct/103")
    assert url.startswith("https://pulse.example/hooks/")
    r = c.post("/hooks/" + url.rsplit("/", 1)[1])
    assert r.text.strip() == "pve: acknowledged", r.text
    r = c.post("/hooks/" + hooks.action_url("mute", ids["nas"]).rsplit("/", 1)[1])
    assert "muted for 1 hour" in r.text
    assert c.post("/hooks/forged").text.strip() == "expired or invalid"
    with Session(engine) as s:
        assert s.get(Device, ids["nas"]).alerts_muted()

    # --- per-device settings, rename ---
    r = c.post(f"/devices/{ids['nas']}/alerting", data={"parent": "0", "cpu": "", "mem": "95", "disk": "0"}, follow_redirects=False)
    assert r.status_code == 303
    assert c.post(f"/devices/{ids['nas']}/rename", data={"name": "pve"}).status_code == 400
    assert c.post(f"/devices/{ids['nas']}/rename", data={"name": "synology", "notes": "DS920+"}).json()["name"] == "synology"
    with Session(engine) as s:
        d = s.get(Device, ids["nas"])
        assert (d.parent_id, d.alert_cpu_percent, d.alert_mem_percent, d.alert_disk_percent, d.name) == (0, None, 95, 0, "synology")
        assert d.threshold("cpu") == 95 and d.threshold("disk") == 0  # global default, own override

    # --- hourly rollup + long history ---
    with Session(engine) as s:
        base = (datetime.utcnow() - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
        for i, cpu in enumerate((10, 30, 50)):
            s.add(ReportHistory(device_id=ids["pve"], timestamp=base + timedelta(minutes=10 * i), cpu_percent=cpu, mem_percent=40, disk_percent=30 + i))
        s.commit()
        monitor.rollup_hourly(s)
        rows = s.exec(select(HourlyHistory).where(HourlyHistory.device_id == ids["pve"], HourlyHistory.hour == base)).all()
        assert len(rows) == 1 and rows[0].cpu_percent == 30 and rows[0].disk_percent == 32, rows
        monitor.rollup_hourly(s)
        assert len(s.exec(select(HourlyHistory).where(HourlyHistory.device_id == ids["pve"], HourlyHistory.hour == base)).all()) == 1
    long = c.get(f"/devices/{ids['pve']}/history.json?range=30d").json()
    assert long and long[0]["cpu_percent"] == 30, long

    # --- metrics and summary ---
    assert c.get("/metrics").status_code == 404
    m = c.get("/metrics", headers={"Authorization": "Bearer m-token"})
    assert m.status_code == 200 and 'pulse_device_up{device="pve"} 1' in m.text and "pulse_problems_open{" in m.text and "pulse_check_up{" not in m.text.split("pulse_problems_open")[1], m.text[:500]
    assert c.get("/api/v1/summary?token=m-token").json()["devices_total"] == 5

    # --- pages ---
    report(c, keys["services"], hostname="services", docker_containers=[
        {"name": "mqtt", "image": "eclipse-mosquitto", "status": "running", "restart_count": 6, "ports": [1883]},
        {"name": "web", "image": "nginx", "status": "running", "health": "unhealthy"}])
    for path in ("/", "/status", "/problems", "/problems/timeline", "/problems/timeline?span=24h", "/problems/timeline?span=30d",
                 "/problems/sent", "/map", f"/devices/{ids['services']}", f"/devices/{ids['pve']}#checks",
                 f"/devices/{ids['pve']}?check_name=mqtt&check_target=10.0.0.2:1883"):
        r = c.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[-800:])
    page = c.get(f"/devices/{ids['services']}").text
    assert "unhealthy" in page and "+ check" in page
    assert "plex-server" in c.get("/map").text
    assert c.get("/status/public").status_code == 404  # off unless PUBLIC_STATUS
    c.cookies.clear()
    assert c.get("/status/public").status_code == 404 and c.get("/status", follow_redirects=False).status_code in (302, 307)

# --- the notification log records a failed push (nothing listens on port 9) ---
notifier.send = real_send
assert real_send("test", title="T") is False
with Session(engine) as s:
    last = s.exec(select(NotificationLog).order_by(NotificationLog.id.desc())).first()
    assert last.title == "T" and not last.ok and last.error
assert notifier.last_failed()
print("ok")
