"""Self-check for the Sonarr / Radarr queue check. Run from host/:  python test_downloads.py

Against a fake Sonarr: a show's blocked episodes are one problem, a sweep's
finds are one short push with Remove & search again / Acknowledge / Mute 1h,
nothing is pushed twice (not even after Sonarr is unreachable for a while),
the buttons and the Health page remove and blocklist, and the daily summary
has a Downloads section.
"""
import asyncio
import json
import os
import tempfile
import time
import types
from datetime import datetime, timedelta

os.environ.update(ADMIN_USERNAME="admin", ADMIN_PASSWORD="hunter2", SESSION_HTTPS_ONLY="false",
                  DATABASE_URL=f"sqlite:///{tempfile.mkdtemp()}/t.db", NTFY_URL="http://ntfy.test/topic",
                  PUBLIC_URL="https://pulse.example")
os.chdir(tempfile.mkdtemp())
os.symlink(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"), "app")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import arr, downloads, monitor, notifier, problems  # noqa: E402
from app.database import engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Device, Problem, ServiceCheck  # noqa: E402
from app.routers import hooks  # noqa: E402


async def _idle():
    return None


monitor.run_forever = _idle

# --- a fake Sonarr ---
SONARR = {"key": "sonarr-key", "deleted": [], "commands": {}, "files": {}}


def ep(i, did, show, sid, s, e, size=0, msg=None, status="completed"):
    return {"id": i, "downloadId": did, "title": f"{show}.S{s:02d}E{e:02d}.1080p-NTb", "size": size, "status": status,
            "seriesId": sid, "series": {"title": show}, "episode": {"seasonNumber": s, "episodeNumber": e},
            "episodeId": sid * 1000 + s * 100 + e,
            "trackedDownloadStatus": "warning", "trackedDownloadState": "importPending",
            "statusMessages": [{"title": "t", "messages": [msg]}] if msg else []}


bad = lambda f: f"No files found are eligible for import in /data/downloads/qbittorrent/{f}"
QUEUE = [
    *[ep(n, f"SH{n}", "Slow Horses", 3, 6, n, msg=bad(f"Slow.Horses.S06E0{n}.exe")) for n in (1, 2, 3)],
    ep(10, "ML1", "MobLand", 4, 2, 1, msg=bad("MobLand S02E01.scr")),
    ep(20, "LK2", "Loki", 6, 1, 2, size=10 ** 9, msg="Unable to determine if file is a sample"),
    {**ep(30, "OK", "Fine Show", 7, 1, 1, size=10 ** 9, status="downloading"), "trackedDownloadStatus": "ok",
     "trackedDownloadState": "downloading", "statusMessages": []},
]


def sonarr(req: httpx.Request) -> httpx.Response:
    if req.headers.get("X-Api-Key") != SONARR["key"]:
        return httpx.Response(401)
    path = req.url.path
    if path == "/api/v3/system/status":
        return httpx.Response(200, json={"appName": "Sonarr", "version": "4.0.15"})
    if path == "/api/v3/queue" and req.method == "GET":
        assert req.url.params.get("includeSeries") == "true"
        return httpx.Response(200, json={"totalRecords": len(QUEUE), "records": QUEUE})
    if path.startswith("/api/v3/queue/") and req.method == "DELETE":
        qid = int(path.rsplit("/", 1)[1])
        # Pulse starts the search itself, so it can follow it
        assert dict(req.url.params) == {"removeFromClient": "true", "blocklist": "true", "skipRedownload": "true"}
        SONARR["deleted"].append(qid)
        QUEUE[:] = [r for r in QUEUE if r["id"] != qid]
        return httpx.Response(200)
    if path == "/api/v3/command" and req.method == "POST":
        body = json.loads(req.content)
        assert body["name"] == "EpisodeSearch" and body["episodeIds"], body
        cid = len(SONARR["commands"]) + 100
        SONARR["commands"][cid] = {"id": cid, "name": "EpisodeSearch", "status": "started", "body": body, "message": ""}
        return httpx.Response(201, json=SONARR["commands"][cid])
    if path.startswith("/api/v3/command/"):
        return httpx.Response(200, json=SONARR["commands"][int(path.rsplit("/", 1)[1])])
    if path == "/api/v3/episode":
        ids = [int(x) for x in req.url.params.get_list("episodeIds")]
        return httpx.Response(200, json=[{"id": i, "episodeFileId": SONARR["files"].get(i, 0)} for i in ids])
    return httpx.Response(404)


arr.httpx = types.SimpleNamespace(
    AsyncClient=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(sonarr), **kw),
    Client=lambda **kw: httpx.Client(transport=httpx.MockTransport(sonarr), **kw),
    HTTPError=httpx.HTTPError)

# --- ntfy: record what would be sent, headers and all ---
PUSHES = []


def fake_post(url, content=b"", headers=None, timeout=None):
    PUSHES.append({"body": content.decode(), **headers})
    return httpx.Response(200)


notifier.httpx = types.SimpleNamespace(post=fake_post, HTTPError=httpx.HTTPError)


def sweep():
    with Session(engine) as s:  # every check due now
        for c in s.exec(select(ServiceCheck)).all():
            c.last_checked_at = None
            s.add(c)
        s.commit()
    asyncio.run(monitor.check_services())
    with Session(engine) as s:
        problems.sync(s)


def open_problems():
    with Session(engine) as s:
        return {p.key: p for p in s.exec(select(Problem).where(Problem.resolved_at == None)).all()}  # noqa: E711


with TestClient(app) as c:
    c.post("/login", data={"username": "admin", "password": "hunter2", "next": "/"})
    with Session(engine) as s:
        d = Device(name="servarr", api_key="x", is_online=True, last_seen_at=datetime.utcnow())
        s.add(d)
        s.commit()
        dev = d.id
    r = c.post(f"/devices/{dev}/checks", data={"name": "Sonarr", "kind": "arr", "target": "sonarr.test:8989",
                                               "secret": SONARR["key"]}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        chk = s.exec(select(ServiceCheck)).one()
        assert (chk.kind, chk.target, chk.interval_seconds) == ("arr", "http://sonarr.test:8989", 300)
        cid = chk.id

    # --- first sweep: one problem per show, one short push for all of it ---
    sweep()
    open_ = open_problems()
    k = lambda key: f"check:{cid}:{key}"
    assert set(open_) == {k("blocked:s3"), k("blocked:s4"), k("stuck:s6")}, set(open_)
    assert open_[k("blocked:s3")].title == "Slow Horses S06E01–E03 failed to download: bad file type (.exe)"
    assert open_[k("blocked:s3")].source == "Downloads" and open_[k("blocked:s3")].severity == "warning"
    assert len(PUSHES) == 1, PUSHES
    push = PUSHES[0]
    assert push["Title"] == "3 downloads need a look", push["Title"]
    assert push["body"] == ("Slow Horses S06E01–E03 · failed to download: bad file type (.exe)\n"
                            "MobLand S02E01 · failed to download: bad file type (.scr)\n"
                            "Loki S01E02 · didn't import: might be a sample"), push["body"]
    labels = [a.split(",")[1].strip() for a in push["Actions"].split("; ")]
    assert labels == ["Remove & search again", "Acknowledge", "Mute 1h"], labels
    assert push["Click"] == "https://pulse.example/health"
    with Session(engine) as s:
        assert "6 in queue, 4 blocked in 2 titles (.exe, .scr), 1 didn't import" == s.get(ServiceCheck, cid).summary

    # --- nothing new: nothing pushed; one more episode of a show already pushed: a short push of its own ---
    sweep()
    assert len(PUSHES) == 1
    QUEUE.append(ep(5, "SH5", "Slow Horses", 3, 6, 5, msg=bad("Slow.Horses.S06E05.exe")))
    sweep()
    assert len(PUSHES) == 2 and PUSHES[1]["Title"] == "Slow Horses failed to download", PUSHES[1]
    assert PUSHES[1]["body"] == "S06E01–E03, E05 · Bad file type (.exe)", PUSHES[1]["body"]

    # --- Sonarr unreachable: problems stay, one "can't read" push; back again: no repeat pushes ---
    SONARR["key"] = "rotated"
    sweep()
    open_ = open_problems()
    assert k("blocked:s3") in open_ and f"check:{cid}" in open_, set(open_)
    assert PUSHES[-1]["body"].startswith("servarr: Sonarr: can't read its queue") and len(PUSHES) == 3, PUSHES[-1]
    SONARR["key"] = "sonarr-key"
    sweep()
    assert len(PUSHES) == 3 and f"check:{cid}" not in open_problems()

    # --- Remove & search again from the phone: blocklists every Slow Horses download ---
    token = hooks.action_url("remove", dev, [k("blocked:s3")]).rsplit("/", 1)[1]
    r = c.post(f"/hooks/{token}")
    assert r.status_code == 200 and r.text.startswith("Sonarr: removed 4 downloads (Slow Horses), searching again"), r.text
    assert "You'll get a push when it's done" in r.text
    assert sorted(SONARR["deleted"]) == [1, 2, 3, 5], SONARR["deleted"]
    (cmd,) = SONARR["commands"].values()
    assert cmd["body"]["episodeIds"] == [3601, 3602, 3603, 3605], cmd
    sweep()
    assert k("blocked:s3") not in open_problems()

    # ... the search finds one: downloading (no push yet), then imported: one short push
    watches = downloads._load()
    (w,) = watches.values()
    assert w["notify"] and w["name"] == "Slow Horses" and w["eps"] == "S06E01–E03, E05"
    cmd.update(status="completed", message="Episode search completed. 1 reports downloaded.")
    QUEUE.append({**ep(40, "PACK", "Slow Horses", 3, 6, 1, size=4000), "sizeleft": 1000, "status": "downloading",
                  "trackedDownloadStatus": "ok", "trackedDownloadState": "downloading", "timeleft": "00:05:00"})
    n = len(PUSHES)
    downloads.sweep({cid: SONARR["key"]}.get, now=time.time() + 61)
    assert len(PUSHES) == n and downloads._load()[w["id"]]["job"]["percent"] == 75
    QUEUE[:] = [r for r in QUEUE if r["id"] != 40]
    SONARR["files"].update({3601: 9, 3602: 9, 3603: 9, 3605: 9})
    downloads.sweep({cid: SONARR["key"]}.get, now=time.time() + 122)
    assert (PUSHES[-1]["Title"], PUSHES[-1]["body"]) == ("Slow Horses downloaded", "S06E01–E03, E05 · Imported into Sonarr"), PUSHES[-1]
    assert downloads._load() == {}

    # --- Acknowledge from the phone takes a list of keys ---
    token = hooks.action_url("ack", dev, [k("stuck:s6")]).rsplit("/", 1)[1]
    assert c.post(f"/hooks/{token}").text.strip() == "servarr: acknowledged"
    assert open_problems()[k("stuck:s6")].acked_at is not None
    assert open_problems()[k("blocked:s4")].acked_at is None

    # --- the Health page: a Remove & search again button on download problems only ---
    health = c.get("/health").text
    assert "MobLand S02E01 failed to download: bad file type (.scr)" in health
    assert health.count("removeDownload(") == 2, health.count("removeDownload(")  # MobLand, and Loki (acknowledged)
    pid = open_problems()[k("blocked:s4")].id
    r = c.post(f"/problems/{pid}/remove")
    assert r.json()["message"].startswith("Sonarr: removed 1 download (MobLand)"), r.json()

    # ... and the page's progress card follows it: searching, then nothing found
    (card,) = r.json()["watches"]
    assert card["label"] == "MobLand S02E01" and card["poll_url"] == f"/downloads/{card['id']}.json" and card["app"] == "Sonarr"
    assert any(j.get("id") == card["id"] for j in c.get("/updates.json").json()["jobs"])  # re-attaches after a reload
    job = c.get(card["poll_url"]).json()
    assert (job["status"], job["stage"]) == ("running", "Searching indexers…"), job
    SONARR["commands"][max(SONARR["commands"])].update(status="completed", message="Episode search completed. 0 reports downloaded.")
    downloads._save({i: {**w, "checked": 0} for i, w in downloads._load().items()})  # past the 3 s cache
    job = c.get(card["poll_url"]).json()
    assert (job["status"], job["done_label"], job["tone"]) == ("done", "Nothing found", "neutral"), job
    assert not any(j.get("id") == card["id"] for j in c.get("/updates.json").json()["jobs"])
    n = len(PUSHES)
    downloads.sweep({cid: SONARR["key"]}.get, now=time.time() + 61)
    assert len(PUSHES) == n  # the card said it; no push for a page removal
    assert c.post("/problems/99999/remove").status_code == 404
    assert c.get(f"/devices/{dev}").status_code == 200

    # --- the daily summary ---
    with Session(engine) as s:
        text, _ = problems.digest(s, datetime.utcnow() + timedelta(minutes=1), {})
    assert "Downloads\n· Sonarr: " in text, text
    assert "· New: Slow Horses S06E01–E03, E05 failed to download: bad file type (.exe) (cleared" in text, text
    assert "Errors" not in text or "Slow Horses" not in text.split("Downloads")[0], text

print("ok")
