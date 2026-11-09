"""Self-check for /mcp, Pulse's MCP server. Run from host/:  python test_mcp.py

The token and Origin guards, the JSON-RPC handshake, and every tool against
a small made-up lab: a machine with containers, one that's off on purpose,
one that's gone offline, a failing check, an acknowledged problem and a
stuck download.
"""
import json
import os
import tempfile
from datetime import datetime, timedelta

os.environ.update(ADMIN_USERNAME="admin", ADMIN_PASSWORD="hunter2", SESSION_HTTPS_ONLY="false",
                  DATABASE_URL=f"sqlite:///{tempfile.mkdtemp()}/t.db", MCP_TOKEN="mcp-secret")
os.chdir(tempfile.mkdtemp())
os.symlink(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"), "app")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app import monitor, problems  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Device, Problem, ServiceCheck  # noqa: E402


async def _idle():
    return None


monitor.run_forever = _idle
now = datetime.utcnow()
GIB = 1024 ** 3

with TestClient(app) as client:
    AUTH = {"Authorization": "Bearer mcp-secret"}

    def rpc(method, params=None, mid=1, headers=AUTH):
        msg = {"jsonrpc": "2.0", "id": mid, "method": method, **({"params": params} if params is not None else {})}
        return client.post("/mcp", json=msg, headers=headers)

    def call(name, args=None):
        r = rpc("tools/call", {"name": name, "arguments": args or {}})
        assert r.status_code == 200, r.text
        res = r.json()["result"]
        if not res.get("isError"):
            assert json.loads(res["content"][0]["text"]) == res["structuredContent"]
        return res

    # --- guards ---
    assert rpc("ping", headers={}).status_code == 404                                  # no token
    assert rpc("ping", headers={"Authorization": "Bearer nope"}).status_code == 404    # wrong token
    assert rpc("ping", headers={**AUTH, "Origin": "https://evil.example"}).status_code == 403
    assert client.get("/mcp", headers=AUTH).status_code == 405
    assert client.get("/mcp").status_code == 404
    settings.mcp_token = ""
    assert rpc("ping").status_code == 404                                              # off without a token
    settings.mcp_token = "mcp-secret"
    print("ok  token, Origin and GET are guarded")

    # --- handshake ---
    init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}).json()
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["capabilities"]["tools"] is not None and init["result"]["serverInfo"]["name"] == "pulse"
    assert rpc("initialize", {"protocolVersion": "1999-01-01"}).json()["result"]["protocolVersion"] == "2025-11-25"
    assert client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=AUTH).status_code == 202
    assert rpc("ping").json()["result"] == {}
    tools = {t["name"]: t for t in rpc("tools/list").json()["result"]["tools"]}
    assert set(tools) == {"pulse_status", "pulse_devices", "pulse_device", "pulse_checks", "pulse_downloads",
                          "pulse_daily_summary", "pulse_retry_download", "pulse_acknowledge"}
    assert all(t["inputSchema"]["type"] == "object" and t["description"] for t in tools.values())
    assert tools["pulse_status"]["annotations"]["readOnlyHint"] is True
    assert tools["pulse_retry_download"]["annotations"]["destructiveHint"] is True
    assert rpc("nope").json()["error"]["code"] == -32601
    assert rpc("tools/call", {"name": "rm_rf"}).json()["error"]["code"] == -32602
    assert client.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}], headers=AUTH).status_code == 400
    assert client.post("/mcp", content=b"{not json", headers=AUTH).status_code == 400
    print("ok  initialize, ping, tools/list, errors")

    # --- a small lab ---
    report = {"system": {"cpu_percent": 12.5, "mem_percent": 61.0, "uptime_seconds": 3 * 86400,
                         "disks": [{"mount": "/", "percent": 71.0, "used_bytes": 71 * GIB, "total_bytes": 100 * GIB},
                                   {"mount": "/etc/hosts", "percent": 99.0}]},
              "apt": {"upgradable": 4},
              "docker_containers": [{"name": "galley", "status": "running", "health": "healthy", "stack": "galley", "restart_count": 0},
                                    {"name": "mosquitto", "status": "exited", "restart_count": 2}]}
    with Session(engine) as s:
        macdeb = Device(name="mac-deb", is_online=True, last_seen_at=now, last_report_json=json.dumps(report))
        pc = Device(name="gaming-pc", is_online=False, offline_ok=True, last_seen_at=now - timedelta(hours=5))
        arrbox = Device(name="servarr", is_online=False, last_seen_at=now - timedelta(minutes=20))
        s.add_all([macdeb, pc, arrbox])
        s.commit()
        web = ServiceCheck(device_id=macdeb.id, name="Galley web", kind="http", target="http://x", last_status="up", last_latency_ms=12.3)
        dns = ServiceCheck(device_id=macdeb.id, name="AdGuard DNS", kind="dns", target="a@b", last_status="down", last_error="timeout")
        son = ServiceCheck(device_id=arrbox.id, name="Sonarr queue", kind="arr", target="http://s", last_status="up",
                           summary="1 blocked in 1 title")
        s.add_all([web, dns, son])
        s.commit()
        high = Problem(device_id=arrbox.id, key="offline", severity="high", source="Device", title="servarr is offline",
                       opened_at=now - timedelta(minutes=18))
        acked = Problem(device_id=macdeb.id, key="updates", severity="warning", source="Updates", title="4 updates waiting",
                        opened_at=now - timedelta(days=2), acked_at=now - timedelta(days=1), ack_note="after the weekend")
        dl = Problem(device_id=arrbox.id, key=f"check:{son.id}:blocked:s3", severity="warning", source="Sonarr queue",
                     title="Slow Horses S06E01–E03 failed to download", detail="bad file type (.exe)", opened_at=now - timedelta(hours=2))
        old = Problem(device_id=macdeb.id, key="disk:/", severity="warning", source="Disk /", title="old", resolved_at=now)
        s.add_all([high, acked, dl, old])
        s.commit()
        ids = {"high": high.id, "acked": acked.id, "dl": dl.id, "old": old.id, "son": son.id}

    # --- pulse_status ---
    st = call("pulse_status")["structuredContent"]
    assert st["machines_online"] == 1 and st["machines_total"] == 3
    assert st["machines_offline"] == ["servarr"], st                # gaming-pc is off on purpose
    assert st["checks_failing"] == ["AdGuard DNS"] and st["checks_passing"] == 2
    assert [p["id"] for p in st["problems"]] == [ids["high"], ids["dl"]]  # high first, acked and resolved left out
    assert st["acknowledged_hidden"] == 1
    assert "2 open problems (1 high)" in st["summary"] and "servarr" in st["summary"] and "AdGuard DNS" in st["summary"]
    assert st["problems"][1]["is_download"] and st["problems"][0]["open_for"] == "18m ago"
    st = call("pulse_status", {"include_acknowledged": True})["structuredContent"]
    assert ids["acked"] in [p["id"] for p in st["problems"]] and st["acknowledged_hidden"] == 0
    print("ok  pulse_status:", st["summary"])

    # --- devices ---
    dv = call("pulse_devices")["structuredContent"]
    assert [d["name"] for d in dv["devices"]] == ["gaming-pc", "mac-deb", "servarr"]
    m = dv["devices"][1]
    assert m["fullest_disk"] == {"mount": "/", "percent": 71.0}               # /etc/hosts isn't a drive
    assert m["updates_pending"] == 4 and m["containers_running"] == 1 and m["containers_total"] == 2
    one = call("pulse_device", {"name": "MAC-DEB"})["structuredContent"]
    assert one["name"] == "mac-deb" and one["uptime"] == "3d" and one["disks"][0]["free_gb"] == 29.0
    assert {c["name"] for c in one["containers"]} == {"galley", "mosquitto"}
    assert {c["name"]: c["status"] for c in one["checks"]} == {"Galley web": "up", "AdGuard DNS": "down"}
    assert [p["id"] for p in one["open_problems"]] == [ids["acked"]]
    assert call("pulse_device", {"name": "serv"})["structuredContent"]["name"] == "servarr"  # part of a name
    miss = call("pulse_device", {"name": "a"})                                            # matches all three
    assert miss["isError"] and "Did you mean" in miss["content"][0]["text"]
    assert call("pulse_device", {"name": "toaster"})["isError"]
    print("ok  pulse_devices, pulse_device")

    # --- checks, downloads, summary ---
    ck = call("pulse_checks")["structuredContent"]
    assert [c["name"] for c in ck["checks"]] == ["AdGuard DNS"] and ck["checks"][0]["error"] == "timeout"
    assert len(call("pulse_checks", {"status": "all"})["structuredContent"]["checks"]) == 3
    assert call("pulse_checks", {"status": "bogus"})["isError"]
    dw = call("pulse_downloads")["structuredContent"]
    assert [p["id"] for p in dw["stuck"]] == [ids["dl"]] and dw["queues"][0]["summary"] == "1 blocked in 1 title"
    assert dw["summary"] == "1 stuck download." and dw["redownloads"] == []
    ds = call("pulse_daily_summary")["structuredContent"]["summary"]
    assert "Slow Horses" in ds and "Sonarr queue" in ds, ds
    assert not (settings.data_dir() / "digest_disks.json").exists()  # tomorrow's real summary is untouched
    print("ok  pulse_checks, pulse_downloads, pulse_daily_summary")

    # --- writes ---
    seen = {}

    def fake_remove(session, keys, notify=False):
        seen.update(keys=keys, notify=notify)
        return "Sonarr queue: removed 3 downloads (Slow Horses), searching again", []

    real_remove, problems.remove_downloads = problems.remove_downloads, fake_remove
    assert call("pulse_retry_download", {"problem_id": ids["high"]})["isError"]     # not a download
    assert call("pulse_retry_download", {"problem_id": 99999})["isError"]
    r = call("pulse_retry_download", {"problem_id": ids["dl"]})["structuredContent"]
    assert seen == {"keys": [f"check:{ids['son']}:blocked:s3"], "notify": True} and "searching again" in r["summary"]
    problems.remove_downloads = real_remove

    a = call("pulse_acknowledge", {"problem_id": ids["high"], "note": "I unplugged it"})["structuredContent"]
    assert a["summary"].startswith("Acknowledged")
    with Session(engine) as s:
        p = s.get(Problem, ids["high"])
        assert p.acked_at and p.ack_note == "I unplugged it"
    assert [p["id"] for p in call("pulse_status")["structuredContent"]["problems"]] == [ids["dl"]]
    call("pulse_acknowledge", {"problem_id": ids["high"], "acknowledge": False})
    with Session(engine) as s:
        assert s.get(Problem, ids["high"]).acked_at is None
    assert call("pulse_acknowledge", {"problem_id": ids["old"]})["isError"]          # already resolved
    print("ok  pulse_retry_download, pulse_acknowledge")

    # --- a tool that blows up says so without leaking the trace ---
    real_digest, problems.digest = problems.digest, lambda *a: 1 / 0
    boom = call("pulse_daily_summary")
    assert boom["isError"] and "ZeroDivision" not in boom["content"][0]["text"]
    problems.digest = real_digest
    print("ok  failures are reported as tool errors")

print("all MCP checks passed")
