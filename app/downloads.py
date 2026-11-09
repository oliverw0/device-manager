"""Following a Remove & search again until it lands.

Each removal starts a watch: which episodes or film, the search Pulse started,
and the downloads it removed. From the Health page the progress card polls it
(/downloads/<id>.json); from the phone's ntfy button nobody is watching a
page, so the monitor sweep reads it every minute and sends one short push when
it ends: downloaded, nothing found, or the search failed. A new fake or a
download that doesn't import isn't pushed from here: it reopens the show's
problem, and that push (with its buttons) says so already.

Watches live in data/download_watches.json and are dropped a day after they
start (a phone watch says so first).
"""
import json
import logging
import secrets
import threading
import time

from . import arr, notifier
from .config import settings

logger = logging.getLogger("devicemanager.downloads")

EXPIRES = 24 * 3600
POLL_EVERY = 3      # the card: at most one read of the app per watch this often
SWEEP_EVERY = 60    # phone watches: read by the sweep this often
KEEP_FINISHED = 600  # the card's last answer stays this long, for a reload
_lock = threading.Lock()


def _path():
    return settings.data_dir() / "download_watches.json"


def _load() -> dict:
    try:
        ws = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    for w in ws.values():  # JSON keys are strings; the app's ids are ints
        w["files"] = {int(k): v for k, v in (w.get("files") or {}).items()} if w.get("files") is not None else None
    return ws


def _save(ws: dict) -> None:
    _path().write_text(json.dumps(ws), encoding="utf-8")


def label(w: dict) -> str:
    return f"{w['name']} {w['eps']}".strip()


def start(check, issue: dict, result: dict, notify: bool) -> dict:
    """A watch for one show's removal (result: arr.remove()). notify: from the
    phone, so the sweep pushes the outcome."""
    w = {"id": secrets.token_hex(6), "check_id": check.id, "check_name": check.name, "device_id": check.device_id,
         "name": issue.get("name") or issue["title"], "eps": issue.get("eps") or "", "notify": notify,
         "started": time.time(), "checked": 0, "job": None, "finished": None,
         **{k: result[k] for k in ("app", "base", "field", "ids", "command_id", "old", "files")}}
    with _lock:
        ws = _load()
        ws[w["id"]] = w
        _save(ws)
    return w


def public(w: dict) -> dict:
    """What the progress card needs: a tracker's job plus how to poll it."""
    job = w.get("job") or {"status": "running", "percent": None, "stage": "Searching indexers…"}
    return {"id": w["id"], "kind": "download", "label": label(w), "app": w["app"],
            "poll_url": f"/downloads/{w['id']}.json", **job}


def _read(w: dict, secret_of) -> dict:
    """Ask the app; on failure keep the last answer (a blip isn't the end)."""
    secret = secret_of(w["check_id"])
    if secret is None:
        return {"status": "error", "percent": None, "stage": "", "eta_seconds": None,
                "error": "The check was deleted", "done_label": None, "tone": None}
    try:
        return arr.track(w["base"].rsplit("/api/", 1)[0], secret, w)
    except Exception as exc:  # noqa: BLE001 — the card keeps polling
        logger.info("couldn't read %s for watch %s: %s", w["app"], w["id"], exc)
        return w.get("job") or {"status": "running", "percent": None, "stage": f"Waiting for {w['app']}…"}


def poll(wid: str, secret_of, now: float | None = None) -> dict | None:
    """The card's poll: the watch's job, read from the app at most every POLL_EVERY s."""
    now = now or time.time()
    with _lock:
        w = _load().get(wid)
    if w is None:
        return None
    if w.get("finished") or now - w.get("checked", 0) < POLL_EVERY:
        return public(w)
    job = _read(w, secret_of)
    with _lock:
        ws = _load()
        if wid in ws:
            w = {**ws[wid], "job": job, "checked": now, "grabbed": w.get("grabbed") or ws[wid].get("grabbed")}
            if job["status"] in ("done", "error") and not w["notify"]:
                w["finished"] = now
            ws[wid] = w
            _save(ws)
    return public(w)


def active() -> list[dict]:
    """Unfinished card watches, so the overlay re-attaches after a reload."""
    with _lock:
        return [public(w) for w in _load().values() if not w["notify"] and not w.get("finished")]


def recent() -> list[dict]:
    """Every watch still kept (they drop after a day), finished or not: for the MCP."""
    with _lock:
        return [{**public(w), "finished": bool(w.get("finished")) or (w.get("job") or {}).get("status") in ("done", "error")}
                for w in _load().values()]


def outcome_push(w: dict, job: dict) -> tuple[str, str] | None:
    """Phone-sized: (title, body), or None when the problem's own push covers it."""
    name, eps, app = w["name"], w["eps"], w["app"]
    body = lambda text: " · ".join(x for x in (eps, text) if x)
    if job["status"] == "done":
        if job.get("tone") == "neutral":
            return f"{name}: nothing found", body(f"No other release yet. {app} keeps looking.")
        if job.get("done_label") == "Downloaded":
            return f"{name} downloaded", body(f"Imported into {app}")
        return f"{name}: {job['done_label']}", body(job.get("stage") or "")
    err = job.get("error") or ""
    if err.startswith(("Fake again", "Didn't import")):
        return None  # the show's problem reopens and pushes, with its buttons
    return f"{name}: search again failed", body(err)


def sweep(secret_of, now: float | None = None) -> list[tuple[str, str]]:
    """Phone watches: read each every SWEEP_EVERY s, push how it ended, drop
    it. Every watch is dropped after EXPIRES (finished card ones after
    KEEP_FINISHED). Returns the pushes sent. Blocking."""
    now = now or time.time()
    with _lock:
        ws = _load()
    sent, drop, update = [], set(), {}
    for wid, w in ws.items():
        if w.get("finished") and now - w["finished"] > KEEP_FINISHED:
            drop.add(wid)
            continue
        if now - w["started"] > EXPIRES:
            if w["notify"]:
                sent.append((f"{w['name']}: still not downloaded", " · ".join(
                    x for x in (w["eps"], f"A day after searching again. Check {w['app']}.") if x)))
            drop.add(wid)
            continue
        if not w["notify"] or now - w.get("checked", 0) < SWEEP_EVERY:
            continue
        job = _read(w, secret_of)
        w = {**w, "job": job, "checked": now}
        if job["status"] in ("done", "error"):
            push = outcome_push(w, job)
            if push:
                sent.append(push)
            drop.add(wid)
        else:
            update[wid] = w
    for title, body in sent:
        notifier.send(body, title=title, tags="clapper", click="/health")
    with _lock:
        ws = _load()
        for wid, w in update.items():
            if wid in ws:
                ws[wid] = {**ws[wid], **{k: w[k] for k in ("job", "checked", "grabbed") if k in w}}
        for wid in drop:
            ws.pop(wid, None)
        _save(ws)
    return sent
