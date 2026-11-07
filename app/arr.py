"""Sonarr, Radarr and Lidarr: downloads stuck in their queue.

The 'arr' check reads the app's download queue with its API key (Settings >
General > API Key). Each stuck download is its own problem:

- blocked: the torrent client refused every file (qBittorrent's excluded file
  names: *.exe, *.scr, ...), so it finished, or sits stopped, at 0 B. These are
  fake releases dressed up as episodes or films. The app never marks them
  failed, so it never looks for another release on its own.
- stuck: finished but not imported (couldn't tell if it's a sample, an archive,
  a file it can't match), waiting for someone to sort it out.

remove() is the "Remove & search again" button: it takes the download out of
the queue and the client, blocklists that release, and lets the app search for
a different one.
"""
import asyncio
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx


class ArrError(Exception):
    """The app couldn't be read; the message says why."""


STOPPED_GRACE = 30 * 60  # a 0 B torrent stopped this long is blocked, not a magnet still fetching metadata
IMPORT_STATES = ("importPending", "importBlocked", "importFailed", "failedPending")
_EXT = re.compile(r"\.([A-Za-z0-9]{2,4})['\"]?\s*\]?$")
_DANGER = re.compile(r"dangerous|executable", re.I)


def normalize_url(target: str) -> str:
    """'192.168.50.39:8989' -> http://192.168.50.39:8989 (no trailing slash)."""
    url = target.strip().rstrip("/")
    return url if "://" in url else "http://" + url


def _messages(r: dict) -> list[str]:
    out = [m for s in r.get("statusMessages") or [] for m in (s.get("messages") or [])]
    return out + ([r["errorMessage"]] if r.get("errorMessage") else [])


def _ext(msgs: list[str]) -> str:
    """The file type the client refused, from Sonarr's "No files found are
    eligible for import in /downloads/Show.S01E01.exe" ('' if it can't tell)."""
    for m in msgs:
        found = _EXT.search(m.strip())
        if found:
            return "." + found.group(1).lower()
    return ""


def _age(r: dict, now: float) -> float | None:
    try:
        added = datetime.fromisoformat((r.get("added") or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return now - (added if added.tzinfo else added.replace(tzinfo=timezone.utc)).timestamp()


_CODE = re.compile(r"\bS(\d{1,2})E(\d{1,3})\b", re.I)


def _who(r: dict) -> tuple[str, str, tuple | None]:
    """-> (group id, display name, (season, episode) or None) for one record:
    a show, a film or an artist, so a season's worth of blocked episodes is
    one problem. Records the app couldn't match fall back to the download."""
    ep = r.get("episode") or {}
    if r.get("seriesId"):
        name = (r.get("series") or {}).get("title")
        code = (ep.get("seasonNumber"), ep.get("episodeNumber")) if ep.get("episodeNumber") is not None else None
        if code is None:  # no episode attached: read it from the release name
            m = _CODE.search(r.get("title") or "")
            code = (int(m.group(1)), int(m.group(2))) if m else None
        return f"s{r['seriesId']}", name or r.get("title") or "?", code
    if r.get("movieId"):
        mv = r.get("movie") or {}
        name = f"{mv['title']} ({mv['year']})" if mv.get("title") and mv.get("year") else mv.get("title") or r.get("title") or "?"
        return f"m{r['movieId']}", name, None
    if r.get("artistId"):
        art, alb = (r.get("artist") or {}).get("artistName"), (r.get("album") or {}).get("title")
        return f"a{r['artistId']}", " – ".join(x for x in (art, alb) if x) or r.get("title") or "?", None
    return f"d{r.get('downloadId') or r.get('id')}", r.get("title") or "?", None


def episodes(codes) -> str:
    """{(6, 1), (6, 2), (6, 3), (6, 5), (6, 6), (7, 1)} -> "S06E01–E03, E05, E06, S07E01".
    Runs of three or more collapse to a range."""
    out = []
    for season in sorted({s for s, _ in codes}):
        eps = sorted(e for s, e in codes if s == season)
        runs, chunk = [], [eps[0]]
        for e in eps[1:]:
            if e == chunk[-1] + 1:
                chunk.append(e)
            else:
                runs.append(chunk)
                chunk = [e]
        runs.append(chunk)
        parts = []
        for run in runs:
            parts += [f"E{run[0]:02d}–E{run[-1]:02d}"] if len(run) >= 3 else [f"E{e:02d}" for e in run]
        out.append(f"S{season:02d}" + ", ".join(parts))
    return ", ".join(out)


_STUCK = [  # Sonarr/Radarr's words -> a few for a phone
    (re.compile(r"sample", re.I), "might be a sample"),
    (re.compile(r"unsupported extension: '?(\.\w+)", re.I), "unsupported file ({0})"),
    (re.compile(r"not a (?:valid )?(?:video|audio)|invalid video", re.I), "not a video file"),
    (re.compile(r"not an upgrade|existing file", re.I), "not better than the file you have"),
    (re.compile(r"(?:no|unknown) (?:series|movie|episode)|matched to .* by id|unable to parse", re.I), "couldn't match it"),
    (re.compile(r"free space|disk", re.I), "not enough disk space"),
    (re.compile(r"permission|access .*denied", re.I), "permission denied"),
]


def stuck_reason(msgs: list[str]) -> str:
    """A finished download's import warning, said in a few words."""
    for m in msgs:
        for rx, words in _STUCK:
            found = rx.search(m)
            if found:
                return words.format(*found.groups())
    return (msgs[0][:50].rstrip(". ") + ("…" if len(msgs[0]) > 50 else "")) if msgs else "needs a manual import"


def assess(records: list[dict], now: float, app: str = "Sonarr") -> tuple[str, list[dict]]:
    """Queue records -> (summary, issues). One issue per show, film or artist
    and kind (blocked / stuck), naming every episode in it; `ids` are its
    download ids (a season pack is one download with several records)."""
    groups: dict[str, dict] = {}
    placed = {}  # downloadId -> group key: a pack's records follow its first
    have = 0     # episodes / films already in the library (downloaded by hand): not news
    for r in records:
        if in_library(r):
            have += 1
            continue
        did = r.get("downloadId") or f"id{r.get('id')}"
        gid, name, code = _who(r)
        if did in placed:
            g = groups[placed[did]]
            if code:
                g["codes"].add(code)
            continue
        msgs = _messages(r)
        size = r.get("size") or 0
        status = (r.get("status") or "").lower()
        age = _age(r, now)
        no_files = any("No files found are eligible" in m for m in msgs)
        stopped = status in ("paused", "stopped") and (age is None or age >= STOPPED_GRACE)
        if size == 0 and (no_files or stopped or status == "completed") or any(_DANGER.search(m) for m in msgs):
            kind = "blocked"
        elif r.get("trackedDownloadStatus") in ("warning", "error") and (r.get("trackedDownloadState") or "") in IMPORT_STATES:
            kind = "stuck"
        else:
            continue
        key = f"{kind}:{gid}"
        g = groups.setdefault(key, {"kind": kind, "name": name, "codes": set(), "ids": [], "exts": set(),
                                    "msgs": [], "finished": False})
        placed[did] = key
        g["ids"].append(did)
        if code:
            g["codes"].add(code)
        if _ext(msgs):
            g["exts"].add(_ext(msgs))
        g["msgs"] += [m for m in msgs if m not in g["msgs"]]
        g["finished"] |= status == "completed"
    issues = []
    for key, g in groups.items():
        what = f"{g['name']} {episodes(g['codes'])}".strip() if g["codes"] else g["name"]
        many = len(g["ids"]) > 1
        if g["kind"] == "blocked":
            exts = ", ".join(sorted(g["exts"]))
            reason = f"bad file type ({exts})" if exts else "no usable files"
            title = f"{what} failed to download: {reason}"
            detail = (f"Fake release{'s' if many else ''}: the torrent client skipped the file{'s' if many else ''}. "
                      f"Remove & search again blocklists {'them' if many else 'it'} and finds another.")
        else:
            reason = stuck_reason(g["msgs"])
            title = f"{what} didn't import: {reason}"
            detail = (g["msgs"][0] if g["msgs"] else "Waiting for a manual import.") + (
                f" Import by hand in {app} (Activity > Queue), or Remove & search again.")
        issues.append({"key": key, "severity": "warning", "title": title, "detail": detail, "ids": g["ids"],
                       "n": max(len(g["codes"]), len(g["ids"])), "name": g["name"],
                       "eps": episodes(g["codes"]) if g["codes"] else "", "reason": reason})
    blocked = [g for g in groups.values() if g["kind"] == "blocked"]
    stuck = [g for g in groups.values() if g["kind"] == "stuck"]
    count = lambda gs: sum(max(len(g["codes"]), len(g["ids"])) for g in gs)
    parts = [f"{len({r.get('downloadId') or r.get('id') for r in records})} in queue"]
    if blocked:
        exts = sorted({e for g in blocked for e in g["exts"]})
        parts.append(f"{count(blocked)} blocked in {len(blocked)} title{'s' if len(blocked) != 1 else ''}"
                     + (f" ({', '.join(exts)})" if exts else ""))
    if stuck:
        parts.append(f"{count(stuck)} didn't import")
    if have:
        parts.append(f"{have} already in the library")
    return ", ".join(parts) + ("" if blocked or stuck else ", nothing stuck"), issues


def in_library(r: dict) -> bool:
    """The episode or film this queue record is for already has a file."""
    return bool(r.get("episodeHasFile") or (r.get("movie") or {}).get("hasFile"))


async def _base(c: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """-> (API base, app name). Sonarr and Radarr are /api/v3, Lidarr /api/v1."""
    for v in ("v3", "v1"):
        r = await c.get(f"{url}/api/{v}/system/status")
        if r.status_code == 401:
            raise ArrError(f"{url} refused the API key (401). Copy it from Settings > General > API Key.")
        if r.status_code < 400:
            try:
                return f"{url}/api/{v}", r.json().get("appName") or "the app"
            except ValueError:
                break
    raise ArrError(f"{url} doesn't look like Sonarr, Radarr or Lidarr (no /api/v3 or /api/v1 status).")


QUEUE_PARAMS = {"pageSize": 500, "includeSeries": "true", "includeEpisode": "true",
                "includeMovie": "true", "includeArtist": "true", "includeAlbum": "true", "includeUnknownSeriesItems": "true", "includeUnknownMovieItems": "true",
                "includeUnknownArtistItems": "true"}


async def check(url: str, api_key: str, now: float | None = None) -> tuple[bool, str, str | None, list[dict]]:
    """-> (read ok, summary, error, issues). Up whenever the queue could be
    read: what's stuck in it is the problems, not an outage. Raises ArrError."""
    now = now or time.time()
    try:
        async with httpx.AsyncClient(timeout=15, headers={"X-Api-Key": api_key}) as c:
            base, app = await _base(c, url)
            r = await c.get(f"{base}/queue", params=QUEUE_PARAMS)
            if r.status_code >= 400:
                raise ArrError(f"{app} answered {r.status_code} for its queue: {r.text[:150]}")
            records = r.json().get("records") or []
            summary, issues = assess(records, now, app)
            # Before calling something failed, make sure it isn't on the NAS already
            # (downloaded by hand, not yet scanned): rescan those shows / films, then look again.
            due = list(dict.fromkeys(g for g in (i["key"].split(":", 1)[1] for i in issues)
                                     if g[:1] in RESCANS and g[1:].isdigit()
                                     and now - _RESCANNED.get((url, g), 0) > RESCAN_EVERY))
            if due and await _rescan(c, base, url, due, now):
                r = await c.get(f"{base}/queue", params=QUEUE_PARAMS)
                if r.status_code < 400:
                    records = r.json().get("records") or []
                    summary, issues = assess(records, now, app)
    except httpx.HTTPError as exc:
        raise ArrError(f"Couldn't reach {url}: {exc or type(exc).__name__}") from exc
    return True, summary, None, issues


RESCANS = {"s": ("RescanSeries", "seriesId"), "m": ("RescanMovie", "movieId")}
RESCAN_EVERY = 6 * 3600  # per show / film
RESCAN_WAIT = 20         # seconds to wait for the scans before reading the queue again
_RESCANNED: dict[tuple[str, str], float] = {}


async def _rescan(c: httpx.AsyncClient, base: str, url: str, groups: list[str], now: float) -> bool:
    """Ask the app to rescan these shows' / films' folders, wait for it (up to
    RESCAN_WAIT s). -> whether any scan ran."""
    ids = []
    for g in groups:
        name, field = RESCANS[g[0]]
        r = await c.post(f"{base}/command", json={"name": name, field: int(g[1:])})
        _RESCANNED[(url, g)] = now
        if r.status_code < 400 and r.json().get("id"):
            ids.append(r.json()["id"])
    deadline = time.monotonic() + RESCAN_WAIT
    while ids and time.monotonic() < deadline:
        await asyncio.sleep(1)
        states = [(await c.get(f"{base}/command/{i}")).json().get("status") for i in ids]
        ids = [i for i, st in zip(ids, states) if st in ("queued", "started")]
    return bool(groups)


# what each app's queue records point at, and the search command for them
TARGETS = (("episodeId", "episodeIds", "EpisodeSearch"), ("movieId", "movieIds", "MoviesSearch"),
           ("albumId", "albumIds", "AlbumSearch"))


def _sync_base(c: httpx.Client, url: str) -> tuple[str, str]:
    r = None
    for v in ("v3", "v1"):
        r = c.get(f"{url}/api/{v}/system/status")
        if r.status_code < 400:
            return f"{url}/api/{v}", r.json().get("appName") or "the app"
    raise ArrError(f"Couldn't read {url} (HTTP {r.status_code if r is not None else '?'}).")


def _files(c: httpx.Client, base: str, field: str, ids: list[int]) -> dict[int, int] | None:
    """{episode/movie id: its file id (0 = none)}; None for apps where it isn't read (Lidarr)."""
    if field == "episodeId":
        r = c.get(f"{base}/episode", params=[("episodeIds", i) for i in ids])
        return {e["id"]: e.get("episodeFileId") or 0 for e in r.json()} if r.status_code < 400 else None
    if field == "movieId":
        out = {}
        for i in ids:
            r = c.get(f"{base}/movie/{i}")
            if r.status_code < 400:
                out[i] = (r.json().get("movieFile") or {}).get("id") or 0
        return out
    return None


def remove(url: str, api_key: str, download_ids: list[str]) -> dict:
    """Remove these downloads from the queue and the torrent client, blocklist
    the releases, and start a search for what they were meant to be. Pulse
    starts that search itself (not the app's automatic redownload) so it can
    follow it: the result is what downloads.track() needs. Blocking (called
    from the ntfy button and the Health page).
    -> {removed, app, base, field, ids, command_id, old, files}."""
    want = set(download_ids)
    with httpx.Client(timeout=20, headers={"X-Api-Key": api_key}) as c:
        base, app = _sync_base(c, url)
        records = c.get(f"{base}/queue", params=QUEUE_PARAMS).json().get("records") or []
        hit = [r for r in records if (r.get("downloadId") or f"id{r.get('id')}") in want]
        field, ids_key, command = next(((f, k, cmd) for f, k, cmd in TARGETS if any(r.get(f) for r in hit)), TARGETS[0])
        targets = sorted({r[field] for r in hit if r.get(field)})
        files = _files(c, base, field, targets) if targets else None
        done, removed = set(), 0
        for rec in hit:
            did = rec.get("downloadId") or f"id{rec.get('id')}"
            if did in done:
                continue
            d = c.delete(f"{base}/queue/{rec['id']}",
                         params={"removeFromClient": "true", "blocklist": "true", "skipRedownload": "true"})
            if d.status_code < 400 or d.status_code == 404:  # 404: already gone
                done.add(did)
                removed += d.status_code < 400
        command_id = None
        if targets:
            r = c.post(f"{base}/command", json={"name": command, ids_key: targets})
            command_id = r.json().get("id") if r.status_code < 400 else None
    return {"removed": removed, "app": app, "base": base, "field": field, "ids": targets,
            "command_id": command_id, "old": sorted(done), "files": files}


_REPORTS = re.compile(r"(\d+) reports? downloaded", re.I)


def _seconds(timeleft: str | None) -> int | None:
    """Sonarr's timeleft "1.02:03:04" / "02:03:04" -> seconds."""
    m = re.fullmatch(r"(?:(\d+)\.)?(\d+):(\d+):(\d+)(?:\.\d+)?", timeleft or "")
    if not m:
        return None
    d, h, mi, sec = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + sec


def progress(w: dict, command: dict | None, records: list[dict], files: dict | None, now: float) -> dict:
    """Where a re-search stands, as a progress card's job:
    {status: running | done | error, percent, stage, eta_seconds, error, done_label, tone}.
    w: the watch (from remove(), plus 'grabbed' once a new download was seen;
    this sets it). command: the search command; records: the queue, every
    record; files: {id: file id} now. Pure, for testing."""
    app = w.get("app") or "Sonarr"
    mine = [r for r in records if r.get(w["field"]) in w["ids"]
            and (r.get("downloadId") or f"id{r.get('id')}") not in w["old"]]
    job = {"status": "running", "percent": None, "stage": "", "eta_seconds": None, "error": None,
           "done_label": None, "tone": None}
    total = len(w["ids"]) or 1
    before = w.get("files") or {}
    got = sum(1 for i in w["ids"] if files and files.get(i) and files.get(i) != before.get(i)) if files is not None else None
    if mine:
        w["grabbed"] = True
        _, issues = assess(mine, now, app)
        bad = [i for i in issues if i["key"].startswith("blocked:")]
        stuck = [i for i in issues if i["key"].startswith("stuck:")]
        if bad:
            return {**job, "status": "error", "error": f"Fake again: {bad[0]['reason']}"}
        if stuck:
            return {**job, "status": "error", "error": f"Didn't import: {stuck[0]['reason']}"}
        size = sum(r.get("size") or 0 for r in mine)
        left = sum(r.get("sizeleft") or 0 for r in mine)
        etas = [x for x in (_seconds(r.get("timeleft")) for r in mine) if x is not None]
        if size and left == 0:
            return {**job, "percent": 100, "stage": f"Importing into {app}…"}
        paused = all((r.get("status") or "").lower() in ("queued", "paused", "delay") for r in mine)
        stage = "Waiting in the download client" if paused else "Downloading"
        if total > 1 and got:
            stage += f" · {got} of {total} done"
        return {**job, "percent": round((size - left) / size * 100) if size else None,
                "stage": stage, "eta_seconds": max(etas) if etas else None}
    if got is not None and got >= total:
        return {**job, "status": "done", "done_label": "Downloaded", "tone": "ok", "stage": f"Imported into {app}"}
    if got is None and w.get("grabbed"):  # Lidarr: no file ids read; left the queue after a grab
        return {**job, "status": "done", "done_label": "Downloaded", "tone": "ok", "stage": f"Left {app}'s queue"}
    status = (command or {}).get("status")
    if command is None or status in ("queued", "started"):
        return {**job, "stage": "Searching indexers…"}
    if status in ("failed", "aborted", "cancelled", "orphaned"):
        return {**job, "status": "error", "error": f"The search {status}: {(command.get('message') or '')[:80]}".rstrip(": ")}
    found = _REPORTS.search(command.get("message") or "")
    n = int(found.group(1)) if found else None
    if got:
        return {**job, "status": "done", "done_label": f"{got} of {total} downloaded", "tone": "ok",
                "stage": "No release found for the rest"}
    if w.get("grabbed"):
        return {**job, "status": "error", "error": f"Left {app}'s queue without importing"}
    if n == 0:
        return {**job, "status": "done", "done_label": "Nothing found", "tone": "neutral",
                "stage": f"No other release yet. {app} keeps looking."}
    ended = (command or {}).get("ended")
    try:
        waited = now - datetime.fromisoformat(ended.replace("Z", "+00:00")).timestamp() if ended else 0
    except ValueError:
        waited = 0
    if waited > 600:
        return {**job, "status": "error", "error": "Grabbed, but it never reached the download client"}
    return {**job, "stage": "Sending to the download client…"}


def track(url: str, api_key: str, w: dict, now: float | None = None) -> dict:
    """Read the app for a watch and say where it stands (progress()). Blocking."""
    now = now or time.time()
    with httpx.Client(timeout=15, headers={"X-Api-Key": api_key}) as c:
        command = None
        if w.get("command_id"):
            r = c.get(f"{w['base']}/command/{w['command_id']}")
            command = r.json() if r.status_code < 400 else {"status": "completed", "message": ""}
        else:
            command = {"status": "completed", "message": "0 reports downloaded"}
        records = c.get(f"{w['base']}/queue", params=QUEUE_PARAMS).json().get("records") or []
        files = _files(c, w["base"], w["field"], w["ids"]) if w["ids"] else None
    return progress(w, command, records, files, now)


def split_key(key: str) -> tuple[int, str] | None:
    """A problem key "check:7:blocked:s12" -> (7, "blocked:s12"), the check and
    its issue key; None for anything that isn't a download problem."""
    parts = key.split(":", 2)
    if len(parts) == 3 and parts[0] == "check" and parts[1].isdigit() and parts[2].split(":")[0] in ("blocked", "stuck"):
        return int(parts[1]), parts[2]
    return None


if __name__ == "__main__":  # ponytail: self-check
    assert normalize_url("192.168.50.39:8989/") == "http://192.168.50.39:8989"
    assert episodes({(6, 1), (6, 2), (6, 3), (6, 5), (6, 6), (7, 1)}) == "S06E01–E03, E05, E06, S07E01"
    assert episodes({(2, 8)}) == "S02E08"
    now = 1_800_000_000
    iso = lambda ago: datetime.fromtimestamp(now - ago, timezone.utc).isoformat().replace("+00:00", "Z")

    def rec(i, did, show, sid, se, size=0, status="completed", msg=None, ok=False, **kw):
        r = {"id": i, "downloadId": did, "title": f"{show} S{se[0]:02d}E{se[1]:02d} 1080p-GRP", "size": size, "status": status,
             "seriesId": sid, "series": {"title": show}, "episode": {"seasonNumber": se[0], "episodeNumber": se[1]},
             "trackedDownloadStatus": "ok" if ok else "warning",
             "trackedDownloadState": "downloading" if ok else "importPending",
             "statusMessages": [{"title": "x", "messages": [msg]}] if msg else []}
        return {**r, **kw}

    eligible = lambda f: f"No files found are eligible for import in /dl/{f}"
    recs = [
        *[rec(i, f"SH{e}", "Slow Horses", 3, (6, e), msg=eligible(f"Slow.Horses.S06E0{e}.exe")) for i, e in enumerate((1, 2, 3, 5, 6))],
        rec(10, "ML8", "MobLand", 4, (2, 8), msg=eligible("MobLand.S02E08.scr")),
        rec(11, "ML1", "MobLand", 4, (2, 1), msg=eligible("MobLand S02E01.exe")),
        rec(12, "ML9", "MobLand", 4, (2, 9), status="paused", ok=True, added=iso(3 * 3600)),  # stopped at 0 B: blocked
        rec(13, "NEW", "Fresh Show", 5, (1, 1), status="paused", ok=True, added=iso(60)),   # magnet still fetching
        rec(14, "LK", "Loki", 6, (1, 2), size=1_000_000, msg="Unable to determine if file is a sample"),
        rec(15, "LK", "Loki", 6, (1, 3), size=1_000_000, msg="Unable to determine if file is a sample"),  # season pack
        rec(16, "REAL", "Real Show", 7, (1, 1), size=900, status="downloading", ok=True),
        {"id": 20, "downloadId": "MV", "title": "Dune.Part.Two.2024.1080p", "size": 0, "status": "completed", "movieId": 9,
         "movie": {"title": "Dune: Part Two", "year": 2024}, "trackedDownloadStatus": "warning", "trackedDownloadState": "importBlocked",
         "statusMessages": [{"messages": ["Caution: Found potentially dangerous file with extension: .lnk"]}]},
    ]
    summary, issues = assess(recs, now)
    keys = {i["key"]: i for i in issues}
    assert set(keys) == {"blocked:s3", "blocked:s4", "stuck:s6", "blocked:m9"}, keys
    sh = keys["blocked:s3"]
    assert sh["title"] == "Slow Horses S06E01–E03, E05, E06 failed to download: bad file type (.exe)", sh["title"]
    assert (sh["name"], sh["eps"], sh["reason"]) == ("Slow Horses", "S06E01–E03, E05, E06", "bad file type (.exe)")
    assert sh["ids"] == ["SH1", "SH2", "SH3", "SH5", "SH6"] and sh["n"] == 5
    assert sh["detail"].startswith("Fake releases: the torrent client skipped the files."), sh["detail"]
    assert keys["blocked:s4"]["title"] == "MobLand S02E01, E08, E09 failed to download: bad file type (.exe, .scr)"
    assert keys["stuck:s6"]["title"] == "Loki S01E02, E03 didn't import: might be a sample" and keys["stuck:s6"]["ids"] == ["LK"]
    assert keys["stuck:s6"]["detail"].startswith("Unable to determine if file is a sample")
    assert keys["blocked:m9"]["title"] == "Dune: Part Two (2024) failed to download: bad file type (.lnk)", keys["blocked:m9"]["title"]
    assert keys["blocked:m9"]["eps"] == ""
    assert summary == "12 in queue, 9 blocked in 3 titles (.exe, .lnk, .scr), 2 didn't import", summary
    assert stuck_reason(["Invalid video file, unsupported extension: '.7z'"]) == "unsupported file (.7z)"
    assert stuck_reason([]) == "needs a manual import"
    assert assess([recs[-2]], now) == ("1 in queue, nothing stuck", [])
    manual = [{**recs[0], "episodeHasFile": True}, recs[1]]
    summary, issues = assess(manual, now)
    assert [i["ids"] for i in issues] == [["SH2"]] and summary == "2 in queue, 1 blocked in 1 title (.exe), 1 already in the library", summary
    assert assess([{**recs[-1], "movie": {"title": "Dune", "hasFile": True}}], now) == ("1 in queue, 1 already in the library, nothing stuck", [])
    assert assess([], now) == ("0 in queue, nothing stuck", [])
    assert split_key("check:7:blocked:s12") == (7, "blocked:s12")
    assert split_key("check:7:late:x") is None and split_key("offline") is None and split_key("check:7") is None

    # --- following a re-search ---
    assert _seconds("1.02:03:04") == 93784 and _seconds("00:12:30") == 750 and _seconds("") is None
    w = {"app": "Sonarr", "field": "episodeId", "ids": [11, 12], "old": ["OLD1", "OLD2"], "files": {11: 0, 12: 0}}
    searching = {"status": "started", "message": "Searching indexers"}
    done = lambda n: {"status": "completed", "message": f"Episode search completed. {n} reports downloaded.",
                      "ended": iso(30)}
    q = lambda did, epid, size, left, **kw: {"id": epid * 10, "downloadId": did, "episodeId": epid, "seriesId": 3,
                                            "series": {"title": "Slow Horses"}, "episode": {"seasonNumber": 6, "episodeNumber": epid - 10},
                                            "title": "x", "size": size, "sizeleft": left, "status": kw.pop("status", "downloading"),
                                            "trackedDownloadStatus": "ok", "trackedDownloadState": "downloading", "timeleft": kw.pop("tl", None), **kw}
    old_still_listed = q("OLD1", 11, 0, 0, status="completed")
    assert progress(dict(w), searching, [old_still_listed], {11: 0, 12: 0}, now)["stage"] == "Searching indexers…"
    assert progress(dict(w), done(2), [], {11: 0, 12: 0}, now)["stage"] == "Sending to the download client…"
    ww = dict(w)
    j = progress(ww, done(2), [q("N1", 11, 1000, 750, tl="00:10:00"), q("N2", 12, 1000, 250, tl="00:02:00")], {11: 0, 12: 0}, now)
    assert (j["status"], j["percent"], j["stage"], j["eta_seconds"]) == ("running", 50, "Downloading", 600), j
    assert ww["grabbed"]
    j = progress(ww, done(2), [q("N2", 12, 1000, 0, status="completed", trackedDownloadState="importPending")], {11: 77, 12: 0}, now)
    assert (j["percent"], j["stage"]) == (100, "Importing into Sonarr…"), j
    j = progress(ww, done(2), [], {11: 77, 12: 78}, now)
    assert (j["status"], j["done_label"], j["tone"]) == ("done", "Downloaded", "ok"), j
    j = progress(dict(w), done(0), [], {11: 0, 12: 0}, now)
    assert (j["status"], j["done_label"], j["tone"]) == ("done", "Nothing found", "neutral"), j
    fake = q("N3", 11, 0, 0, status="completed", trackedDownloadStatus="warning", trackedDownloadState="importPending",
             statusMessages=[{"messages": ["No files found are eligible for import in /dl/Slow.Horses.S06E01.exe"]}])
    j = progress(dict(w), done(1), [fake], {11: 0, 12: 0}, now)
    assert (j["status"], j["error"]) == ("error", "Fake again: bad file type (.exe)"), j
    j = progress(dict(w, grabbed=True), done(1), [], {11: 0, 12: 0}, now)
    assert j["status"] == "error" and "without importing" in j["error"], j
    j = progress(dict(w), done(1), [], {11: 5, 12: 0}, now)
    assert j["done_label"] == "1 of 2 downloaded", j
    upgrade = dict(w, files={11: 40, 12: 41})  # had files already: only new ones count
    assert progress(upgrade, searching, [], {11: 40, 12: 41}, now)["status"] == "running"
    print("ok")
