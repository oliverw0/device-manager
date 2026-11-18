#!/usr/bin/env python3
"""NEXUS: the lab's action board. The network map (what's down, what has problems) and Willow to ask about the details,
with what needs Oliver beside them; services and media are on a second screen (#apps).

Runs on LXC 106 next to Willow, because Willow's API only answers localhost and NPM. Standard library only.

  python3 nexus.py           serve on :8810
  python3 nexus.py --test    self-check of build(), the access rules and the input checks
  python3 nexus.py --demo    serve the page on 127.0.0.1 with made-up data (for working on the look)

  GET  /api/overview            everything the page shows (every 30 s)
  GET  /api/net                 traffic per guest from Glances on pve (every 5 s)
  GET  /api/img/plex?thumb=...  a poster from Plex, through Tautulli
  GET  /api/img/jf/<item id>    a poster from Jellyfin
  POST /api/act                 {"do", "id"}: approve/decline (Willow), done (list), retry/ack (Pulse),
                                seerr-approve/seerr-decline (Seerr)
  POST /api/chat                {"text"}: ask Willow; a slow answer comes back as {"pending": id}
  GET  /api/pending/<id>?after=N   follow a slow answer

Who may use it: localhost, and NPM (nexus.wiles.au, behind Authentik, so it carries X-authentik-username).
POSTs also need the header X-Nexus: 1, which a page on another site can't send without a CORS preflight.
Secrets in ./secrets (700): pulse-mcp-token, runner-token, tautulli-key, seerr-key, jellyfin-key (optional).
"""
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
SECRETS = HERE / "secrets"
PORT = int(os.environ.get("NEXUS_PORT", "8810"))
NPM_IP = os.environ.get("NEXUS_NPM_IP", "192.168.50.172")  # LXC 104, Nginx Proxy Manager
PULSE = os.environ.get("NEXUS_PULSE", "http://192.168.50.225:8000/mcp")
WILLOW = "http://127.0.0.1:8800"
RUNNER = "http://127.0.0.1:8790"
GLANCES = "http://192.168.50.30:61208/api/4/network"
TAUTULLI = "http://192.168.50.146:8181/api/v2"
SEERR = "http://192.168.50.226:5055/api/v1"
JELLYFIN = "http://192.168.50.50:8096"
TMDB_IMG = "https://image.tmdb.org/t/p/w185"
HA = "http://192.168.50.225:8123"
HA_RATES = {"nas": ("sensor.wiles_nas_download_throughput", "sensor.wiles_nas_upload_throughput")}  # Synology integration
RATE_UNITS = {"B/s": 1, "kB/s": 1e3, "KB/s": 1e3, "KiB/s": 1024, "MB/s": 1e6, "MiB/s": 1048576, "GB/s": 1e9,
              "bit/s": 0.125, "kbit/s": 125, "Mbit/s": 125e3, "Gbit/s": 125e6}
UPLINK = "enp3s0"  # pve's port to the router: all of pve's traffic
WILLOW_HEADERS = {"X-Willow-Session": "nexus-dashboard", "X-Willow-Device": "nexus"}
DAY = 86400

# The lab, for the map and the service list. "device" is the machine's name in Pulse (lights its LED); a guest's "id"
# finds its veth/tap interface on pve (traffic, and whether it's running). Icons: a name from the dashboard-icons
# set (loaded from jsDelivr by the browser), or a path to one of ours in static/icons.
LAB = {
    "router": {"name": "Router", "sub": "192.168.50.1 · ASUS RT-AX82U", "url": "https://rout.wiles.au", "icon": "asus-router"},
    "hosts": [
        {"id": "pve", "name": "pve", "sub": "192.168.50.30 · Proxmox VE 9.2", "model": "Intel i5-12600K · 32 GB · 256 GB NVMe + 2 TB HDD",
         "device": "proxmox-host",
         "url": "https://proxmox.wiles.au", "icon": "proxmox", "services": [["Proxmox", "https://proxmox.wiles.au", "proxmox"]],
         "guests": [
             {"id": 101, "name": "authentik", "sub": ".171", "device": "Authentik",
              "services": [["Authentik", "https://auth.wiles.au", "authentik"]]},
             {"id": 102, "name": "pbs", "kind": "VM", "sub": ".81", "device": "PBS (VM)",
              "services": [["Backup Server", "https://pbs.wiles.au", "proxmox"]]},
             {"id": 103, "name": "services", "sub": ".146", "device": "services",
              "services": [["AdGuard Home", "http://192.168.50.146", "adguard-home"], ["Tautulli", "http://192.168.50.146:8181", "tautulli"]]},
             {"id": 104, "name": "proxy", "sub": ".172", "device": "proxy", "tag": "DOOR",
              "services": [["Proxy Manager", "http://192.168.50.172:81", "nginx-proxy-manager"]]},
             {"id": 106, "name": "willow", "sub": ".194",
              "services": [["Willow", "https://willow.wiles.au", "icons/willow.png"]]},
             {"id": 108, "name": "games", "sub": ".83",
              "services": [["Lobby", "https://lobby.wiles.au", "icons/lobby.svg"]]},
             {"id": 109, "name": "plex", "sub": ".50", "device": "plex-server", "tag": "PUBLIC",
              "services": [["Plex", "https://plex.wiles.au/web", "plex"], ["Jellyfin", "https://jellyfin.wiles.au", "jellyfin"]]},
             {"id": 110, "name": "frigate", "sub": ".22", "device": "frigate",
              "services": [["Frigate", "https://frigate.wiles.au", "frigate"]]},
             {"id": 114, "name": "servarr", "sub": ".226", "device": "servarr",
              "services": [["Seerr", "https://seerr.wiles.au", "overseerr"], ["Sonarr", "http://100.100.229.115:8989", "sonarr"],
                           ["Radarr", "http://100.100.229.115:7878", "radarr"], ["Lidarr", "http://100.100.229.115:8686", "lidarr"],
                           ["Prowlarr", "http://100.100.229.115:9696", "prowlarr"], ["qBittorrent", "http://100.100.229.115:8080", "qbittorrent"],
                           ["Portainer", "https://100.100.229.115:9443", "portainer"]]},
             {"id": 100, "name": "pufferpanel", "sub": "retired", "off": True, "services": []},
             {"id": 105, "name": "adguard", "sub": "retired", "off": True, "services": []},
             {"id": 115, "name": "crafty", "sub": "retired", "off": True, "services": []},
         ]},
        {"id": "mac-deb", "name": "mac-deb", "sub": "192.168.50.225 · Debian 13 (Asahi)", "model": "MacBook Air M1 · 8 GB",
         "device": "mac-deb", "icon": "debian-linux",
         "services": [["Home Assistant", "http://192.168.50.225:8123", "home-assistant"],
                      ["Galley", "https://galley.wiles.au", "icons/galley.svg"]]},
        {"id": "nas", "name": "nas", "sub": "192.168.50.4 · Synology DSM", "model": "Synology DS916+ · 16 TB", "device": "Wiles-NAS", "url": "https://nas.wiles.au",
         "icon": "synology-dsm", "services": [["DSM", "https://nas.wiles.au", "synology-dsm"]]},
        # the gaming PC: switched on and off often ("Often switched off" in Pulse), so off is grey, not red
        {"id": "bazzite", "name": "bazzite", "sub": "192.168.50.85 · Bazzite 45 (Wi-Fi)", "model": "Acer Veriton N4680GT · i5-11400 · 16 GB · 256 GB NVMe",
         "device": "bazzite", "icon": "steam", "services": []},
    ],
}

# What the internet can reach: the router forwards only WAN 443, to the edge nginx on LXC 104 (:8443), which serves these
# names and refuses every other one in the TLS handshake. NPM (:443) is for the LAN and Tailscale only.
DOOR = {"port": 443, "to": "LXC 104 edge :8443", "addr": "192.168.50.172:8443", "names": ["jellyfin.wiles.au", "plex.wiles.au"], "probe": "pbs.wiles.au"}


# ---- sources
def secret(name):
    p = SECRETS / name
    return p.read_text().strip() if p.exists() else None


def call(url, body=None, headers=None, timeout=10, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return (r.read(), r.headers.get("Content-Type", "")) if raw else json.load(r)


def pulse(tool, **args):
    r = call(PULSE, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}},
             {"Authorization": f"Bearer {secret('pulse-mcp-token')}"}, timeout=30 if tool == "pulse_retry_download" else 10)
    if "error" in r or r["result"].get("isError"):
        raise RuntimeError(str(r.get("error") or r["result"].get("content"))[:200])
    return r["result"]["structuredContent"]


_usage = {"at": 0, "v": None}


def usage():
    """Plan usage, kept 10 minutes: the runner allows this client 10 requests an hour, the page asks every 30 s."""
    tok = secret("runner-token")
    if not tok:
        return None
    if time.time() - _usage["at"] > 600:
        _usage["at"] = time.time()  # a failure is kept 10 minutes too: retrying every 30 s would use up the hourly limit
        try:
            _usage["v"], _usage["err"] = call(RUNNER + "/v1/usage?max_age=3600", headers={"Authorization": f"Bearer {tok}"}, timeout=30), None
        except Exception as e:  # noqa: BLE001
            _usage["err"] = e
    if _usage.get("err"):
        raise _usage["err"]
    return _usage["v"]


_wan = {"at": 0, "v": None}


def wan():
    """The lab's public IP and ISP as the internet sees it, kept 30 minutes. Cloudflare's trace page is reached by IP,
    so AdGuard's blocklists (which block ipinfo.io and ipify) can't stop it; the ISP's name comes from APNIC's registry."""
    if time.time() - _wan["at"] > 1800:
        req = urllib.request.Request("https://1.1.1.1/cdn-cgi/trace", headers={"User-Agent": "nexus"})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = dict(line.split("=", 1) for line in r.read().decode().splitlines() if "=" in line)
        isp = (_wan["v"] or {}).get("isp") if (_wan["v"] or {}).get("ip") == d.get("ip") else None
        if not isp:
            try:
                rd = call(f"https://rdap.apnic.net/ip/{d['ip']}", timeout=10)
                isp = next((x["description"][0] for x in rd.get("remarks", []) if x.get("description")), None) or rd.get("name")
            except Exception:  # noqa: BLE001  the IP still shows without its ISP
                isp = None
        _wan["v"] = {"ip": d.get("ip"), "isp": isp, "city": d.get("loc")}
        _wan["at"] = time.time()
    return _wan["v"]


_door = {"at": 0, "v": None}


def door_probe(ip, name):
    """One HTTPS request to the public IP with this name: ["open", status], ["refused", None] (no TLS for that name, as
    the edge does for anything not public) or ["silent", None] (no answer at all)."""
    try:
        with socket.create_connection((ip, DOOR["port"]), timeout=6) as raw, \
                ssl.create_default_context().wrap_socket(raw, server_hostname=name) as s:
            s.sendall(f"HEAD / HTTP/1.1\r\nHost: {name}\r\nUser-Agent: nexus-door\r\nConnection: close\r\n\r\n".encode())
            line = s.recv(64).decode("latin-1").split("\r\n")[0].split()
            return ["open", int(line[1]) if len(line) > 1 and line[1].isdigit() else 0]
    except (ssl.SSLError, ConnectionResetError):
        return ["refused", None]
    except OSError:
        return ["silent", None]


def door():
    """Checks the internet door from inside (the router loops the public IP back): the public names must answer, and an
    internal one must be refused. If the internal one answers, the router is forwarding to NPM again and every host is
    on the internet. Kept 10 minutes."""
    if time.time() - _door["at"] > 600:
        ip = (wan() or {}).get("ip")
        if not ip:
            raise OSError("public IP unknown")
        names = [*DOOR["names"], DOOR["probe"]]
        with ThreadPoolExecutor(len(names)) as ex:
            res = dict(zip(names, ex.map(lambda n: door_probe(ip, n), names)))
        _door["v"] = {"ip": ip, "names": {n: res[n] for n in DOOR["names"]}, "probe": res[DOOR["probe"]], "at": time.time()}
        _door["at"] = time.time()
    return _door["v"]


def net():
    """Bytes per second per guest, from pve's side of each guest's veth/tap: sent = the guest's download."""
    out = {"uplink": None, "guests": {}}
    for i in call(GLANCES, timeout=5):
        name, down, up = i["interface_name"], i.get("bytes_sent_rate_per_sec") or 0, i.get("bytes_recv_rate_per_sec") or 0
        m = re.fullmatch(r"(?:veth|tap)(\d+)i0", name)
        if m:
            out["guests"][m.group(1)] = {"down": round(down), "up": round(up)}
        elif name == UPLINK:  # the host's own port: recv is what comes in from the router
            out["uplink"] = {"down": round(i.get("bytes_recv_rate_per_sec") or 0), "up": round(i.get("bytes_sent_rate_per_sec") or 0)}
    out["hosts"] = ha_rates()
    return out


_ha = {"at": 0, "v": {}}


def ha_rates():
    """Traffic for hosts Glances can't see, from Home Assistant sensors (the NAS through the Synology integration).
    Kept 15 s: HA only refreshes them about once a minute. Inert until secrets/ha-token exists."""
    tok = secret("ha-token")
    if not tok or time.time() - _ha["at"] < 15:
        return _ha["v"] if tok else {}
    out = {}
    for host, ids in HA_RATES.items():
        try:
            vals = []
            for e in ids:
                st = call(f"{HA}/api/states/{e}", headers={"Authorization": f"Bearer {tok}"}, timeout=5)
                vals.append(float(st["state"]) * RATE_UNITS.get(st["attributes"].get("unit_of_measurement"), 1))
            out[host] = {"down": round(vals[0]), "up": round(vals[1])}
        except Exception:  # noqa: BLE001  unknown/unavailable sensor: the map shows the link as unmeasured
            pass
    _ha.update(at=time.time(), v=out)
    return out


def tautulli():
    key = secret("tautulli-key")
    if not key:
        return None
    q = lambda cmd, **kw: call(f"{TAUTULLI}?" + urllib.parse.urlencode({"apikey": key, "cmd": cmd, **kw}))["response"]["data"]
    with ThreadPoolExecutor(2) as ex:
        act, hist = ex.map(lambda a: q(*a[:1], **a[1]), [("get_activity", {}), ("get_history", {"length": 14})])
    now = [{"src": "plex", "title": s.get("grandparent_title") or s.get("title"), "sub": plex_sub(s), "user": s.get("friendly_name"),
            "player": s.get("player") or s.get("product"), "state": s.get("state"), "pct": int(s.get("progress_percent") or 0),
            "transcode": s.get("transcode_decision"), "img": plex_img(s.get("grandparent_thumb") or s.get("thumb"))}
           for s in act.get("sessions", [])]
    recent = [{"src": "plex", "title": h.get("grandparent_title") or h.get("title"), "sub": plex_sub(h), "user": h.get("friendly_name"),
               "player": h.get("player"), "at": h.get("stopped") or h.get("date"), "pct": h.get("percent_complete"),
               "img": plex_img(h.get("thumb"))} for h in hist.get("data", [])]
    return {"now": now, "recent": recent}


def plex_sub(x):
    if x.get("media_type") == "episode":
        return f"S{int(x.get('parent_media_index') or 0):02d} · E{int(x.get('media_index') or 0):02d} · {x.get('title', '')}"
    return str(x.get("year") or "")


def plex_img(thumb):
    return f"api/img/plex?thumb={urllib.parse.quote(thumb)}" if thumb and PLEX_THUMB.fullmatch(thumb) else None


PLEX_THUMB = re.compile(r"/library/metadata/\d+/(?:thumb|art)(?:/\d+)?")
JF_ID = re.compile(r"[0-9a-f]{32}")


def jf_auth(key):
    """Jellyfin 10.11 turned off the old X-Emby-Token header by default; this is the one it takes now."""
    return {"Authorization": f'MediaBrowser Token="{key or ""}"'}


def jellyfin():
    """Now playing and recently played, every user. Inert until secrets/jellyfin-key exists (Jellyfin > Dashboard > API Keys)."""
    key = secret("jellyfin-key")
    if not key:
        return None
    h = jf_auth(key)

    def sub(it):
        if it.get("Type") == "Episode":
            return f"S{it.get('ParentIndexNumber') or 0:02d} · E{it.get('IndexNumber') or 0:02d} · {it.get('Name', '')}"
        return str(it.get("ProductionYear") or "")

    def img(it):
        i = it.get("SeriesId") if it.get("Type") == "Episode" else it.get("Id")
        return f"api/img/jf/{i}" if i and JF_ID.fullmatch(i) else None

    now = []
    for s in call(f"{JELLYFIN}/Sessions?activeWithinSeconds=120", headers=h):
        it = s.get("NowPlayingItem")
        if not it:
            continue
        pos, run = (s.get("PlayState") or {}).get("PositionTicks") or 0, it.get("RunTimeTicks") or 0
        now.append({"src": "jellyfin", "title": it.get("SeriesName") or it.get("Name"), "sub": sub(it), "user": s.get("UserName"),
                    "player": s.get("DeviceName") or s.get("Client"), "state": "paused" if (s.get("PlayState") or {}).get("IsPaused") else "playing",
                    "pct": int(pos * 100 / run) if run else 0, "img": img(it)})
    recent = []
    for u in call(f"{JELLYFIN}/Users", headers=h)[:8]:
        q = urllib.parse.urlencode({"SortBy": "DatePlayed", "SortOrder": "Descending", "Filters": "IsPlayed", "Recursive": "true",
                                    "Limit": 8, "IncludeItemTypes": "Movie,Episode", "Fields": "UserData"})
        for it in call(f"{JELLYFIN}/Users/{u['Id']}/Items?{q}", headers=h).get("Items", []):
            played = (it.get("UserData") or {}).get("LastPlayedDate")
            at = datetime.fromisoformat(played[:19] + "+00:00").timestamp() if played else None
            recent.append({"src": "jellyfin", "title": it.get("SeriesName") or it.get("Name"), "sub": sub(it), "user": u.get("Name"),
                           "at": at, "pct": 100, "img": img(it)})
    return {"now": now, "recent": sorted(recent, key=lambda r: -(r["at"] or 0))[:14]}


_titles = {}  # (type, tmdbId) -> {"title", "year", "poster"}: Seerr's request list has no titles


def seerr():
    key = secret("seerr-key")
    if not key:
        return None
    h = {"X-Api-Key": key}
    reqs = call(f"{SEERR}/request?take=20&skip=0&sort=added&filter=all", headers=h)["results"]

    def title(r):
        k = (r["type"], r["media"]["tmdbId"])
        if k not in _titles:
            d = call(f"{SEERR}/{'movie' if r['type'] == 'movie' else 'tv'}/{k[1]}", headers=h)
            date = d.get("releaseDate") or d.get("firstAirDate") or ""
            _titles[k] = {"title": d.get("title") or d.get("name") or "?", "year": date[:4],
                          "poster": TMDB_IMG + d["posterPath"] if d.get("posterPath") else None}
        return _titles[k]

    with ThreadPoolExecutor(6) as ex:
        names = list(ex.map(title, reqs))
    out = []
    for r, t in zip(reqs, names):
        m, dl = r.get("media") or {}, (r.get("media") or {}).get("downloadStatus") or []
        state = {1: "pending", 3: "declined", 4: "failed"}.get(r["status"]) or \
            ("available" if m.get("status") == 5 else "partial" if m.get("status") == 4 else "downloading" if dl else
             "processing" if m.get("status") == 3 else "approved")
        size = sum(d.get("size") or 0 for d in dl)
        left = sum(d.get("sizeLeft") or 0 for d in dl)
        eta = max((d.get("estimatedCompletionTime") or "" for d in dl), default="") or None
        out.append({"id": r["id"], "type": r["type"], **t, "state": state, "by": (r.get("requestedBy") or {}).get("displayName"),
                    "at": iso(r.get("createdAt")), "seasons": [s["seasonNumber"] for s in r.get("seasons") or []],
                    "pct": round((size - left) * 100 / size) if size else None, "eta": iso(eta)})
    return out


def containers():
    """{device name: [{"name", "state" (ok/warn/bad), "status", "health"}]}: every Docker container each machine's
    client reports, for the squares under the map's tiles. Inside Pulse this reads its own database (setup() patches
    it in); standalone there's nothing to read, so the map has no squares."""
    return {}


def device_ids():
    """{device name: Pulse device id}, for the overlay's "Open in Devices" button. Patched in by setup() inside Pulse."""
    return {}


def service_of(problem):
    """Which app on its machine a Pulse problem belongs to, so only that app's light changes: a stuck episode is
    Sonarr's, a stuck film Radarr's. Anything else belongs to the machine as a whole (None)."""
    if not problem.get("is_download"):
        return None
    return "Sonarr" if re.search(r"\bS\d{2}E\d{2}", problem.get("title", "")) else "Radarr"


def plural(n, w):
    return f"{n} {w}{'' if n == 1 else 's'}"


def iso(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None
    except ValueError:
        return None


SOURCES = {
    "status": lambda: pulse("pulse_status"),
    "devices": lambda: pulse("pulse_devices"),
    "willow": lambda: call(WILLOW + "/api/tasks", headers=WILLOW_HEADERS),
    "log": lambda: call(WILLOW + "/api/log?days=3", headers=WILLOW_HEADERS),
    "usage": usage,
    "net": net,
    "plex": tautulli,
    "jellyfin": jellyfin,
    "seerr": seerr,
    "wan": wan,
    "door": door,
    "ctrs": lambda: containers(),
}
LABELS = {"ctrs": "the container list", "wan": "the public IP lookup", "door": "the internet door check","status": "the health checks", "devices": "the device list", "willow": "Willow", "log": "Willow's log", "usage": "the Claude runner",
          "net": "Glances on pve", "plex": "Tautulli", "jellyfin": "Jellyfin", "seerr": "Seerr"}


def fetch_all():
    def one(fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001  a dead source becomes a warning, never a broken page
            return e
    with ThreadPoolExecutor(len(SOURCES)) as ex:
        return dict(zip(SOURCES, ex.map(one, SOURCES.values())))


# ---- what the page shows
def willow_brief(x):
    """What "Pass to Willow" sends: the item, with its Pulse problem id when it has one. The first line is the title,
    which is how the page knows it's already with Willow."""
    pid = next((a[1] for a in x.get("actions", []) if a[0] in ("retry", "ack")), None)
    return "\n".join(s for s in (
        x["title"],
        f"Passed to you from NEXUS ({x.get('src') or 'a warning'}{', open ' + x['age'] if x.get('age') else ''}).",
        x.get("detail") or "",
        f"Pulse problem id {pid}." if pid is not None else "",
        "Find out what's wrong. If one of your fixes would sort it, propose it for my OK; "
        "otherwise tell me what you found and put the fix on my list.",
    ) if s)[:2000]


def build(src, now=None):
    now = now or time.time()
    ok = {k: v is not None and not isinstance(v, Exception) for k, v in src.items()}
    st, dev, wil, use, nw = (src.get(k) for k in ("status", "devices", "willow", "usage", "net"))
    lamps, needs, warns, todo = [], [], [], []

    def lamp(name, level, text, href="#warnings", items=()):
        lamps.append({"name": name, "level": level, "text": text, "href": href, "items": [str(i)[:90] for i in items][:6]})

    for k, label in LABELS.items():
        if isinstance(src.get(k), Exception):
            warns.append({"level": "warn", "title": f"Can't reach {label}", "detail": str(src[k])[:200], "src": "NEXUS"})

    # Pulse: machines, checks, problems
    problems = [p for p in (st or {}).get("problems", []) if not p.get("acknowledged")] if ok.get("status") else []
    checks = [c for c in (st or {}).get("checks_failing", []) if isinstance(c, dict)] if ok.get("status") else []
    if ok.get("status"):
        off = [m if isinstance(m, str) else m.get("name", "?") for m in st.get("machines_offline", [])]
        lamp("Machines", "bad" if off else "ok", f"{len(off)} OFFLINE" if off else f"{st['machines_online']}/{st['machines_total']} UP", "#map", off)
        for m in off:
            warns.append({"level": "bad", "title": f"{m} is offline", "detail": "NEXUS hasn't heard from it.", "src": "PULSE"})
        failing = st.get("checks_failing", [])
        lamp("Checks", "bad" if failing else "ok", f"{len(failing)} FAILING" if failing else f"{st['checks_passing']}/{st['checks_total']} PASS",
             items=[(c.get("name") or c.get("title", "?")) if isinstance(c, dict) else c for c in failing])
        for c in failing:
            c = c if isinstance(c, dict) else {"name": str(c)}
            warns.append({"level": "bad", "title": f"Check failing: {c.get('name') or c.get('title', '?')}",
                          "detail": c.get("detail") or c.get("summary") or "", "src": "PULSE"})
    else:
        lamp("Machines", "bad", "NO ANSWER", "#map")
        lamp("Checks", "bad", "NO ANSWER")
    downloads = [p for p in problems if p.get("is_download")]
    others = [p for p in problems if not p.get("is_download")]
    lamp("Downloads", "warn" if downloads else "ok", f"{len(downloads)} STUCK" if downloads else "CLEAR", "#needs",
         [p["title"] for p in downloads])
    lamp("Problems", ("bad" if any(p.get("severity") == "critical" for p in others) else "warn") if others else "ok",
         f"{len(others)} OPEN" if others else "NONE", items=[f"{p.get('device')}: {p['title']}" for p in others])
    for p in others:
        warns.append({"level": "bad" if p.get("severity") == "critical" else "warn", "title": p["title"], "detail": p.get("detail", ""),
                      "src": f"PULSE · {p.get('device', '')}", "age": p.get("open_for"), "actions": [["ack", p["id"], "Acknowledge"]]})

    # Disks (Pulse devices)
    devices = {d["name"]: d for d in (dev or {}).get("devices", [])} if ok.get("devices") else {}
    try:
        dev_ids = device_ids()
    except Exception:  # noqa: BLE001  the button falls back to the device list
        dev_ids = {}
    worst, full = 0, []
    for d in devices.values():
        pct = (d.get("fullest_disk") or {}).get("percent") or 0
        if d.get("expected_off"):
            continue
        worst = max(worst, pct)
        if pct >= 70:
            full.append(f"{d['name']} {d['fullest_disk']['mount']} {pct:.0f}%")
        if pct >= 80:  # ponytail: one threshold for every disk; per-disk limits if a big media disk lives near full on purpose
            warns.append({"level": "bad" if pct >= 90 else "warn", "title": f"{d['name']}: {d['fullest_disk']['mount']} is {pct:.0f}% full",
                          "detail": "Free some space or grow the disk before it fills.", "src": "PULSE"})
    lamp("Disks", "bad" if worst >= 90 else "warn" if worst >= 80 else "ok", f"FULLEST {worst:.0f}%" if devices else "NO ANSWER", items=full)

    # Willow: approvals, jobs, the list
    tasks = (wil or {}).get("tasks", []) if ok.get("willow") else []
    waiting = [t for t in tasks if t.get("state") == "waiting"]
    running = [t for t in tasks if t.get("state") in ("running", "building")]
    failed = [t for t in tasks if t.get("state") == "failed" and now - (t.get("updated") or 0) < DAY]
    for t in waiting:
        fix = [a.get("text", "?") for a in (t.get("proposal") or {}).get("actions", [])] if (t.get("proposal") or {}).get("kind") == "fix" else []
        needs.append({"src": "WILLOW", "label": "Fix · approval" if fix else "Approval", "at": t.get("created"), "title": t.get("title", "?"),
                      "detail": (t.get("say") or t.get("request") or "")[:300] + (f" The fix: {'; '.join(fix)}." if fix else ""),
                      "actions": [["approve", t["id"], "Approve"], ["decline", t["id"], "Decline"]]})
    for t in failed:
        warns.append({"level": "warn", "title": f"Willow job failed: {t.get('title', '?')}", "detail": (t.get("say") or "")[:300], "src": "WILLOW"})
    rank = {"decision": 0, "claude": 1}
    for i in sorted((wil or {}).get("list", []) if ok.get("willow") else [], key=lambda i: (i.get("due") or "9999", rank.get(i.get("kind"), 2))):
        acts = [["copy", i["detail"], "Copy brief"]] if i.get("kind") == "claude" and i.get("detail") else []
        todo.append({"kind": i.get("kind") or "todo", "label": {"claude": "Claude brief", "decision": "Decision"}.get(i.get("kind"), "To do"),
                     "due": i.get("due"), "at": i.get("created"), "title": i.get("text", "?"),
                     "detail": "" if acts else (i.get("detail") or "")[:300], "actions": acts + [["done", i["id"], "Done"]]})

    # Seerr: requests waiting for approval go to "needs you"
    reqs = src["seerr"] if ok.get("seerr") else []
    pending = [r for r in reqs if r["state"] == "pending"]
    for r in pending:
        needs.append({"src": "SEERR", "label": f"Request · {'TV' if r['type'] == 'tv' else 'Movie'}", "at": r["at"],
                      "title": f"{r['title']} ({r['year']})" if r.get("year") else r["title"], "detail": f"Requested by {r.get('by') or '?'}.",
                      "img": r.get("poster"), "actions": [["seerr-approve", r["id"], "Approve"], ["seerr-decline", r["id"], "Decline"]]})
    for p in downloads:
        needs.append({"src": "PULSE", "label": "Download", "age": p.get("open_for"), "title": p["title"], "detail": p.get("detail", ""),
                      "actions": [["retry", p["id"], "Remove & search again"], ["ack", p["id"], "Acknowledge"]]})
    lamp("Approvals", "warn" if waiting or pending else "ok", f"{len(waiting) + len(pending)} WAITING" if waiting or pending else "NONE", "#needs",
         [t.get("title", "?") for t in waiting] + [r["title"] for r in pending])
    lamp("Willow", "bad" if not ok.get("willow") else "warn" if failed else "ok",
         "NO ANSWER" if not ok.get("willow") else f"{len(failed)} FAILED" if failed else f"{len(running)} RUNNING" if running else "IDLE", "#willow",
         [f"Failed: {t.get('title')}" for t in failed] + [f"Running: {t.get('title')}" for t in running])

    # Claude plan usage
    week = (use if ok.get("usage") else {}).get("seven_day") or {}  # an HTTPError is truthy and fakes attributes
    if isinstance(use, Exception):
        lamp("Claude", "warn", "NO ANSWER", "#willow")
    elif "used_percent" not in week:
        lamp("Claude", "neutral", "NOT SET UP", "#willow")
    else:
        p = week["used_percent"]
        lamp("Claude", "bad" if p >= 90 else "warn" if p >= 70 else "ok", f"{p:.0f}% OF WEEK", "#willow")

    # The map: every machine's light, the reasons behind it, Pulse's readings, running state and traffic
    traffic = (nw or {}).get("guests", {}) if ok.get("net") else {}
    host_rates = (nw or {}).get("hosts", {}) if ok.get("net") else {}

    def issues_for(name):
        out = []
        d = devices.get(name)
        if d and not d.get("online") and not d.get("expected_off"):
            out.append({"level": "bad", "title": "Offline", "down": True, "detail": f"Last heard from it {d.get('last_seen') or 'a while ago'}."})
        for p in problems:
            if p.get("device") == name:
                acts = [["retry", p["id"], "Remove & search again"], ["ack", p["id"], "Acknowledge"]] if p.get("is_download") else [["ack", p["id"], "Acknowledge"]]
                out.append({"level": "bad" if p.get("severity") == "critical" else "warn", "title": p["title"], "detail": p.get("detail", ""),
                            "age": p.get("open_for"), "src": p.get("source"), "actions": acts, "service": service_of(p)})
        for c in checks:
            if c.get("device") == name:
                out.append({"level": "warn", "title": f"Check failing: {c.get('name') or c.get('title', '?')}",
                            "detail": c.get("detail") or c.get("summary") or "", "src": "PULSE"})
        pct = ((d or {}).get("fullest_disk") or {}).get("percent") or 0
        if pct >= 80 and not (d or {}).get("expected_off"):
            out.append({"level": "bad" if pct >= 90 else "warn", "title": f"{d['fullest_disk']['mount']} is {pct:.0f}% full",
                        "detail": "Free some space or grow the disk before it fills."})
        return out

    def readings(name):
        d = devices.get(name)
        if not d:
            return None
        run, tot = d.get("containers_running"), d.get("containers_total")
        return {"online": d.get("online"), "last_seen": d.get("last_seen"), "cpu": d.get("cpu_percent"), "mem": d.get("memory_percent"),
                "disk": d.get("fullest_disk"), "updates": d.get("updates_pending"), "containers": [run, tot] if tot else None,
                "muted": d.get("alerts_muted"), "expected_off": d.get("expected_off")}

    def status(node, issues, running=None):
        """The light and a one-line reason for it: what the hover card and the dialog lead with. Red only when the machine
        is down (offline, not running); any open problem on it (unacknowledged Pulse problems, a failing check, a full
        disk) makes it amber, however serious: the map shows where to look, the problem list says how bad."""
        dev_name = node.get("device")
        d = devices.get(dev_name) if dev_name else None
        if node.get("off"):
            return None, "Retired: stopped, waiting to be deleted"
        if running is False:
            return "off", "Not running: no network interface on pve"
        down = [i for i in issues if i.get("down")]
        if down:
            return "off", down[0]["title"] + (f", and {plural(len(issues) - 1, 'other problem')}" if len(issues) > 1 else "")
        if issues:
            return "warn", f"{plural(len(issues), 'open problem')}"
        if d:
            return ("on", "Up, nothing wrong") if d.get("online") else (None, "Off, as expected")
        if running:
            return "on", "Running (no monitoring client, so no readings)"
        return None, "No monitor: nothing reports on it"

    def node(n, rates=None, running=None, extra=()):
        iss = issues_for(n.get("device")) + list(extra)
        light, why = status(n, iss, running)
        ctrs = (src.get("ctrs") or {}).get(n.get("device")) if ok.get("ctrs") and n.get("device") else None
        return {**n, "led": light, "why": why, "issues": iss, "readings": readings(n.get("device")), "net": rates, "ctrs": ctrs,
                "device_id": dev_ids.get(n.get("device")) if n.get("device") else None}

    def guest(g):
        run = str(g["id"]) in traffic if ok.get("net") else None
        extra = [{"level": "bad", "title": "Not running", "down": True, "detail": f"pve has no network interface for {g.get('kind', 'LXC')} {g['id']}."}]             if run is False and not g.get("off") else []
        n = node(g, traffic.get(str(g["id"])), run, extra)
        n["state"] = "unknown" if run is None else "up" if run else "down"
        if n["state"] == "down" and not g.get("off"):
            warns.append({"level": "bad", "title": f"{g['id']} {g['name']} isn't running", "detail": "No network interface for it on pve.", "src": "PROXMOX"})
        return n

    hosts = []
    for h in LAB["hosts"]:
        rates = (nw or {}).get("uplink") if h["id"] == "pve" and ok.get("net") else host_rates.get(h["id"])
        n = node(h, rates, True if h["id"] == "pve" and ok.get("net") else None)
        n["measured"] = rates is not None
        n["guests"] = [guest(g) for g in h.get("guests", [])]
        hosts.append(n)

    # Media
    media = {"now": [], "recent": [], "sources": {}}
    for k in ("plex", "jellyfin"):
        media["sources"][k] = "ok" if ok.get(k) else "error" if isinstance(src.get(k), Exception) else "off"
        if ok.get(k):
            media["now"] += src[k]["now"]
            media["recent"] += src[k]["recent"]
    media["recent"].sort(key=lambda r: -(r.get("at") or 0))
    media["recent"] = media["recent"][:14]

    # Willow's conversation log, newest last
    turns = [{k: t.get(k) for k in ("at", "device", "heard", "say", "route", "ms")} for t in (src["log"].get("turns", []) if ok.get("log") else [])
             if t.get("heard") or t.get("say")][-40:]

    # "Pass to Willow": anything wrong (not Willow's own jobs or Seerr requests, which have their own buttons) can go
    # to Willow as a background job; it looks into it and proposes a fix for Oliver's OK. Once passed, it says so.
    if ok.get("willow"):
        busy = {(t.get("request") or "").split("\n")[0] for t in tasks if t.get("state") in ("running", "waiting", "building")}
        for x in needs + warns:
            if x.get("src") in ("WILLOW", "SEERR"):
                continue
            if x["title"] in busy:
                x["with_willow"] = True
            else:
                x["actions"] = x.get("actions", []) + [["pass", willow_brief(x), "Pass to Willow"]]

    # The internet door: only the public names may answer on the public IP
    dr = src.get("door") if ok.get("door") else None
    if dr:
        if dr["probe"][0] == "open":
            warns.append({"level": "bad", "title": f"The internet can reach {DOOR['probe']}",
                          "detail": f"An internal name answered on the public IP ({dr['ip']}:443), so every NPM host probably can. "
                                    f"Point the router's 443 forward back at the edge, {DOOR['addr']}.",
                          "src": "EDGE"})
        for n, (state, _code) in dr["names"].items():
            if state != "open":
                warns.append({"level": "warn", "title": f"{n} isn't answering from the internet",
                              "detail": "Nothing came back on the public IP for it. Check the edge container on LXC 104 and the router's 443 forward.",
                              "src": "EDGE"})

    order = {"bad": 0, "warn": 1}
    warns.sort(key=lambda w: order.get(w["level"], 2))
    return {"at": now, "lamps": lamps, "needs": needs, "warnings": warns, "todo": todo,
            "map": {"router": LAB["router"], "wan": src.get("wan") if ok.get("wan") else None, "door": {**DOOR, "check": dr}, "uplink": (nw or {}).get("uplink") if ok.get("net") else None, "hosts": hosts},
            "media": media, "requests": reqs,
            "willow": {"running": [{"title": t.get("title"), "steps": len(t["steps"]) if isinstance(t.get("steps"), list) else None,
                                    "at": t.get("created")} for t in running],
                       "recent": [{"title": t.get("title"), "state": t.get("state"), "at": t.get("updated") or t.get("created")}
                                  for t in tasks if t.get("state") in ("done", "failed")][:5],
                       "turns": turns},
            "usage": use if ok.get("usage") else None,
            "sources": {k: ok.get(k, False) for k in SOURCES if not (k == "jellyfin" and src.get(k) is None)}}


def act(do, ident):
    """One button on the page. Ids are checked here, before anything is sent anywhere."""
    if do in ("approve", "decline") and re.fullmatch(r"[A-Za-z0-9_-]{12}", str(ident)):
        r = call(f"{WILLOW}/api/tasks/{ident}/{do}", {}, WILLOW_HEADERS, timeout=30)
        return {"ok": True, "say": r.get("say", "")}
    if do == "add" and isinstance(ident, str) and ident.strip() and len(ident) <= 200:   # a new item on Willow's list
        r = call(f"{WILLOW}/api/list", {"text": ident.strip(), "kind": "todo"}, WILLOW_HEADERS)
        return {"ok": bool(r.get("item")), "say": "Added to your list." if r.get("item") else "Willow didn't take that one."}
    if do == "done" and re.fullmatch(r"[A-Za-z0-9_-]{8}", str(ident)):
        r = call(f"{WILLOW}/api/list/{ident}/done", {}, WILLOW_HEADERS)
        return {"ok": bool(r.get("ok")), "say": ""}
    if do in ("retry", "ack") and isinstance(ident, int) and not isinstance(ident, bool):
        r = pulse("pulse_retry_download", problem_id=ident) if do == "retry" else \
            pulse("pulse_acknowledge", problem_id=ident, acknowledge=True, note="Acknowledged in NEXUS")
        return {"ok": True, "say": r.get("summary", "")}
    if do == "pass" and isinstance(ident, str) and ident.strip() and len(ident) <= 2000:
        call(f"{WILLOW}/api/tasks", {"title": ident.split("\n")[0][:60], "request": ident}, WILLOW_HEADERS, timeout=30)
        return {"ok": True, "say": "Passed to Willow. It'll look into it and ask before changing anything."}
    if do in ("seerr-approve", "seerr-decline") and isinstance(ident, int) and not isinstance(ident, bool):
        call(f"{SEERR}/request/{ident}/{do.split('-')[1]}", {}, {"X-Api-Key": secret("seerr-key")}, timeout=20)
        return {"ok": True, "say": "Approved in Seerr." if do == "seerr-approve" else "Declined in Seerr."}
    raise ValueError("unknown action or bad id")


def chat(text):
    text = str(text or "").strip()[:2000]
    if not text:
        raise ValueError("say something")
    r = call(WILLOW + "/api/chat", {"text": text}, WILLOW_HEADERS, timeout=60)
    return {k: r.get(k) for k in ("heard", "say", "show", "route", "pending")}


def pending(pid, after):
    r = call(f"{WILLOW}/api/pending/{pid}?after={after}", headers=WILLOW_HEADERS, timeout=40)
    return {"done": r.get("done"), "next": r.get("next"), "steps": [s.get("say") for s in r.get("steps", [])],
            **{k: r.get(k) for k in ("say", "show", "route", "superseded")}}


@lru_cache(maxsize=400)
def image(kind, ident):
    """A poster, fetched once and kept in memory (400 posters is a few MB)."""
    if kind == "plex":
        return call(f"{TAUTULLI}?" + urllib.parse.urlencode({"apikey": secret("tautulli-key"), "cmd": "pms_image_proxy", "img": ident,
                                                              "width": 200, "height": 300, "fallback": "poster"}), raw=True, timeout=15)
    return call(f"{JELLYFIN}/Items/{ident}/Images/Primary?fillHeight=300&quality=85", headers=jf_auth(secret("jellyfin-key")),
                raw=True, timeout=15)


# ---- HTTP
def allowed(ip, headers, demo=False, path="/"):
    if ip == "127.0.0.1":
        return True
    if demo or ip != NPM_IP:
        return False
    # NPM's "Cache Assets" serves .css/.js/images from its own location block, which skips Authentik, so those
    # arrive without the header. They're the page's own stylesheets, scripts and icons: nothing private.
    return bool(headers.get("X-authentik-username")) or (not path.startswith("/api/") and Path(path).suffix in (".css", ".js", ".svg", ".png"))


TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml", ".png": "image/png"}


class Handler(BaseHTTPRequestHandler):
    server_version = "nexus/2"
    demo = False

    def log_message(self, fmt, *args):
        if "/api/net" not in self.path:  # every 5 s per open page: not worth a log line
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def send(self, body, ctype="application/json", status=200, cache="no-store"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if not allowed(self.client_address[0], self.headers, self.demo, path):
            return self.send({"error": "not allowed"}, status=403)
        try:
            if path == "/api/overview":
                return self.send(build(demo_sources() if self.demo else fetch_all()))
            if path == "/api/net":
                return self.send(demo_sources()["net"] if self.demo else net())
            if path == "/api/img/plex" and PLEX_THUMB.fullmatch(q.get("thumb", [""])[0]) and not self.demo:
                body, ctype = image("plex", q["thumb"][0])
                return self.send(body, ctype if ctype.startswith("image/") else "image/jpeg", cache="private, max-age=86400")
            m = re.fullmatch(r"/api/img/jf/([0-9a-f]{32})", path)
            if m and not self.demo:
                body, ctype = image("jf", m.group(1))
                return self.send(body, ctype if ctype.startswith("image/") else "image/jpeg", cache="private, max-age=86400")
            m = re.fullmatch(r"/api/pending/([A-Za-z0-9_-]{16})", path)
            if m:
                after = int(q.get("after", ["0"])[0]) if q.get("after", ["0"])[0].isdigit() else 0
                return self.send(pending(m.group(1), after))
        except Exception as e:  # noqa: BLE001
            return self.send({"error": str(e)[:200]}, status=502)
        if path.startswith("/api/"):
            return self.send({"error": "not found"}, status=404)
        f = (STATIC / (path.lstrip("/") or "index.html")).resolve()
        if STATIC.resolve() not in f.parents or not f.is_file():
            return self.send({"error": "not found"}, status=404)
        return self.send(f.read_bytes(), TYPES.get(f.suffix, "application/octet-stream"),
                         cache="no-cache" if f.suffix == ".html" else "public, max-age=3600")

    def do_POST(self):
        path = urlparse(self.path).path
        if not allowed(self.client_address[0], self.headers, self.demo, path) or self.headers.get("X-Nexus") != "1":
            return self.send({"error": "not allowed"}, status=403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(min(n, 8192)) or b"{}")
            if path == "/api/act":
                return self.send({"ok": True, "say": f"(demo) {d.get('do')} {d.get('id')}"} if self.demo else act(d.get("do"), d.get("id")))
            if path == "/api/chat":
                return self.send(demo_chat(d.get("text")) if self.demo else chat(d.get("text")))
            return self.send({"error": "not found"}, status=404)
        except ValueError as e:
            return self.send({"ok": False, "error": str(e)}, status=400)
        except Exception as e:  # noqa: BLE001
            return self.send({"ok": False, "error": str(e)[:200]}, status=502)


# ---- demo data and self-check
def demo_chat(text):
    return {"heard": text, "say": f"(demo) You said: {text}. Everything's answering and nothing needs you right now.", "route": "demo"}


def demo_sources():
    now = time.time()
    rnd = lambda a, b: round(a + (b - a) * ((now * 7.3) % 1))
    return {
        "status": {"machines_online": 10, "machines_total": 11, "machines_offline": ["Wiles-NAS"], "checks_passing": 3, "checks_total": 3,
                   "checks_failing": [], "problems": [
                       {"id": 76, "severity": "warning", "device": "servarr", "source": "Downloads", "is_download": True, "acknowledged": False,
                        "title": "South Park S29E05 failed to download: bad file type (.exe)", "open_for": "1d 9h ago",
                        "detail": "Fake release: the torrent client skipped the file. Remove & search again blocklists it and finds another."},
                       {"id": 80, "severity": "warning", "device": "proxmox-host", "source": "Updates", "is_download": False, "acknowledged": False,
                        "title": "31 updates waiting on proxmox-host", "open_for": "3d ago", "detail": "Includes a kernel; needs a reboot."}]},
        "devices": {"devices": [
            {"name": "proxmox-host", "online": True, "fullest_disk": {"mount": "/mnt/hdd2tb", "percent": 74.9}},
            {"name": "services", "online": True, "fullest_disk": {"mount": "/", "percent": 84.0}},
            {"name": "servarr", "online": True, "last_seen": "18s ago", "cpu_percent": 4.3, "memory_percent": 47.3, "containers_running": 15,
             "containers_total": 15, "fullest_disk": {"mount": "/", "percent": 61.0}},
            {"name": "Wiles-NAS", "online": False, "last_seen": "2h ago"}, {"name": "mac-deb", "online": True, "cpu_percent": 1.5,
             "memory_percent": 59.3, "containers_running": 8, "containers_total": 12, "last_seen": "1m ago"},
            {"name": "Authentik", "online": True}, {"name": "proxy", "online": True}, {"name": "plex-server", "online": True},
            {"name": "frigate", "online": True}, {"name": "PBS (VM)", "online": True},
            {"name": "Oliver PC", "online": False, "expected_off": True}]},
        "willow": {"tasks": [
            {"id": "abcdefghijkl", "state": "waiting", "title": "Set up a Valheim server on Lobby", "created": now - 3000,
             "say": "Valheim from the template, 4 GB of memory, port 2456. Say yes and I'll start it."},
            {"id": "defghijklmno", "state": "waiting", "title": "Sonarr isn't importing", "created": now - 600,
             "request": "Sonarr isn't importing\nPassed to you from NEXUS.",
             "say": "Sonarr lost its connection to qBittorrent after the VPN reconnected, so finished episodes sit there. A restart sorts it. It's ready for your OK.",
             "proposal": {"kind": "fix", "actions": [{"do": "restart", "target": "sonarr", "text": "Restart sonarr (on servarr)"}]}},
            {"id": "bcdefghijklm", "state": "running", "title": "Compare 2.5 GbE switches under $200", "created": now - 240, "steps": [1, 2, 3]},
            {"id": "cdefghijklmn", "state": "done", "title": "Dessert places near Ormond", "created": now - 9000, "updated": now - 8900}],
            "list": [{"id": "7m4g3idy", "kind": "claude", "text": "Claude: vary the phone-sent confirmation", "detail": "Change Willow so...", "created": now - 9000},
                     {"id": "mxrSF2lk", "kind": "decision", "text": "Gift for Sabrina", "due": "2026-11-16", "detail": "Birthday Thursday 19 November.", "created": now - 90000},
                     {"id": "-EwkmD4P", "kind": "todo", "text": "Will to come round and test the guitars", "detail": "Any weekday from Monday 16 November.", "created": now - 70000}]},
        "log": {"turns": [
            {"at": now - 7200, "device": "pixel", "heard": "What's on tomorrow?", "say": "Tomorrow's clear until 6, then dinner at Mum's.", "route": "calendar", "ms": 640},
            {"at": now - 3100, "device": "pc", "heard": "Can you set up a Valheim server?", "say": "I've planned it. It needs your OK before I start it.", "route": "task", "ms": 5200},
            {"at": now - 300, "device": "talk page", "heard": "Is anything broken?", "say": "One download's stuck: South Park S29E05 was a fake release. Everything else is fine.", "route": "lab", "ms": 4100}]},
        "usage": {"seven_day": {"used_percent": 23, "resets_at": "2026-11-14T09:00:00+11:00"}, "five_hour": {"used_percent": 4}},
        "wan": {"ip": "203.0.113.24", "isp": "Example ISP", "city": "Melbourne"},
        "door": {"ip": "203.0.113.24", "names": {"jellyfin.wiles.au": ["open", 302], "plex.wiles.au": ["open", 401]}, "probe": ["refused", None], "at": now},
        "ctrs": {k: [{"name": n, "state": st, "status": "running" if st == "ok" else "exited", "health": None} for n, st in v] for k, v in {
            "servarr": [("gluetun", "ok"), ("sonarr", "ok"), ("radarr", "ok"), ("lidarr", "ok"), ("prowlarr", "ok"), ("qbittorrent", "ok"),
                        ("nzbget", "ok"), ("seerr", "ok"), ("slskd", "ok"), ("soularr", "warn"), ("byparr", "ok"), ("portainer", "ok")],
            "services": [("adguard-adguardhome-1", "ok"), ("homepage", "ok"), ("tautulli", "ok"), ("tautulli-proxy", "ok")],
            "frigate": [("frigate", "ok")], "proxy": [("nginx-proxy-manager-app-1", "ok"), ("devicemanager-client", "ok")],
            "mac-deb": [("homeassistant", "ok"), ("mosquitto", "ok"), ("devicemanager-host", "ok"), ("galley", "ok"), ("adguard", "bad")],
        }.items()},
        "net": {"uplink": {"down": rnd(400_000, 4_000_000), "up": rnd(50_000, 2_500_000)},
                "guests": {"101": {"down": 230, "up": 550}, "102": {"down": 1600, "up": 1400}, "103": {"down": rnd(3000, 9000), "up": 3100},
                           "104": {"down": 4500, "up": 4800}, "106": {"down": 2800, "up": 2000}, "108": {"down": 140, "up": 570},
                           "109": {"down": rnd(20_000, 90_000), "up": rnd(900_000, 3_000_000)}, "110": {"down": rnd(800_000, 1_400_000), "up": 30_000},
                           "114": {"down": rnd(200_000, 6_000_000), "up": 70_000}},
                "hosts": {"nas": {"down": rnd(20_000, 400_000), "up": 3000}}},
        "plex": {"now": [{"src": "plex", "title": "Slow Horses", "sub": "S06 · E03 · Footprints", "user": "James", "player": "TV 2021",
                          "state": "playing", "pct": 38, "transcode": "direct play", "img": None}],
                 "recent": [{"src": "plex", "title": "Slow Horses", "sub": "S06 · E02 · Daddy Issues", "user": "James", "player": "TV 2021", "at": now - 80000, "pct": 100, "img": None},
                            {"src": "plex", "title": "The Running Man", "sub": "2025", "user": "oliverwiles", "player": "Chrome", "at": now - 120000, "pct": 100, "img": None}]},
        "jellyfin": {"now": [], "recent": [{"src": "jellyfin", "title": "Severance", "sub": "S02 · E10 · Cold Harbor", "user": "Oliver", "at": now - 30000, "pct": 100, "img": None}]},
        "seerr": [{"id": 73, "type": "tv", "title": "Pluribus", "year": "2025", "poster": None, "state": "pending", "by": "James", "at": now - 1800, "seasons": [1], "pct": None, "eta": None},
                  {"id": 72, "type": "movie", "title": "Weapons", "year": "2025", "poster": None, "state": "downloading", "by": "oliverwiles", "at": now - 5000, "seasons": [], "pct": 64, "eta": now + 1300},
                  {"id": 71, "type": "movie", "title": "The Long Walk", "year": "2025", "poster": None, "state": "available", "by": "oliverwiles", "at": now - 90000, "seasons": [], "pct": None, "eta": None}],
    }


def test():
    v = build(demo_sources())
    lamps = {l["name"]: l for l in v["lamps"]}
    assert lamps["Machines"]["level"] == "bad" and lamps["Machines"]["text"] == "1 OFFLINE"
    assert lamps["Downloads"]["text"] == "1 STUCK" and lamps["Approvals"]["text"] == "3 WAITING"
    assert lamps["Disks"]["level"] == "warn" and lamps["Claude"]["text"] == "23% OF WEEK"
    assert [n["src"] for n in v["needs"]] == ["WILLOW", "WILLOW", "SEERR", "PULSE"], [n["src"] for n in v["needs"]]
    # Pass to Willow: on problems and warnings (with the Pulse id), not on Willow's approvals or Seerr requests
    fixneed = v["needs"][1]
    assert fixneed["label"] == "Fix · approval" and fixneed["detail"].endswith("The fix: Restart sonarr (on servarr).")
    passed = [a for a in v["needs"][3]["actions"] if a[0] == "pass"]
    assert passed and passed[0][1].startswith(v["needs"][3]["title"] + "\n") and "Pulse problem id 76." in passed[0][1]
    assert not any(a[0] == "pass" for n in v["needs"][:3] for a in n["actions"])
    assert all(any(a[0] == "pass" for a in w["actions"]) for w in v["warnings"] if w["src"] != "WILLOW")
    s = demo_sources()
    s["willow"]["tasks"].append({"id": "efghijklmnop", "state": "running", "title": "x", "request": v["warnings"][0]["title"] + "\nPassed to you from NEXUS."})
    w0 = build(s)["warnings"][0]
    assert w0.get("with_willow") and not any(a[0] == "pass" for a in w0.get("actions", []))
    assert v["todo"][0]["title"] == "Gift for Sabrina" and v["todo"][-1]["kind"] == "todo"
    assert v["warnings"][0]["level"] == "bad" and "offline" in v["warnings"][0]["title"]
    g = {g["name"]: g for h in v["map"]["hosts"] for g in h["guests"]}
    assert g["servarr"]["led"] == "warn" and g["servarr"]["state"] == "up" and g["games"]["led"] == "on"
    assert g["crafty"]["state"] == "down" and g["crafty"]["led"] is None  # retired: dim, not an alarm
    assert not any("crafty" in w["title"] for w in v["warnings"])
    hosts = {h["id"]: h for h in v["map"]["hosts"]}
    assert hosts["nas"]["led"] == "off" and hosts["nas"]["why"] == "Offline" and hosts["nas"]["measured"]
    assert not hosts["mac-deb"]["measured"] and hosts["mac-deb"]["readings"]["containers"] == [8, 12]
    assert g["servarr"]["why"] == "1 open problem" and g["servarr"]["issues"][0]["service"] == "Sonarr"
    # the map's colours: amber for any open problem however serious, red only when down; acknowledged ones don't count
    s = demo_sources()
    s["status"]["problems"] += [{"id": 90, "severity": "critical", "device": "proxy", "title": "x", "acknowledged": False},
                                {"id": 91, "severity": "critical", "device": "frigate", "title": "y", "acknowledged": True}]
    s["status"]["checks_failing"] = [{"name": "backup", "device": "Authentik"}]
    g2 = {g["name"]: g for h in build(s)["map"]["hosts"] for g in h["guests"]}
    assert g2["proxy"]["led"] == "warn" and g2["frigate"]["led"] == "on" and g2["authentik"]["led"] == "warn"
    assert service_of({"is_download": True, "title": "Weapons (2025) didn't import"}) == "Radarr" and service_of({"title": "x"}) is None and g["servarr"]["issues"][0]["actions"][0][0] == "retry"
    assert g["games"]["why"].startswith("Running (no monitoring") and g["crafty"]["why"].startswith("Retired")
    assert {l["name"]: l for l in v["lamps"]}["Downloads"]["items"][0].startswith("South Park")
    assert v["map"]["wan"]["ip"] == "203.0.113.24"
    # the internet door: quiet when only the public names answer; red if an internal name answers on the public IP
    assert v["map"]["door"]["check"]["probe"][0] == "refused" and not any(w["src"] == "EDGE" for w in v["warnings"])
    s = demo_sources()
    s["door"] = {**s["door"], "probe": ["open", 200], "names": {**s["door"]["names"], "plex.wiles.au": ["silent", None]}}
    ew = [w for w in build(s)["warnings"] if w["src"] == "EDGE"]
    assert ew[0]["level"] == "bad" and "pbs.wiles.au" in ew[0]["title"] and "192.168.50.172:8443" in ew[0]["detail"]
    assert ew[1]["level"] == "warn" and ew[1]["title"].startswith("plex.wiles.au")
    g = {g["name"]: g for h in v["map"]["hosts"] for g in h["guests"]}
    assert v["media"]["recent"][0]["title"] == "Severance" and v["media"]["now"][0]["pct"] == 38
    assert len(v["willow"]["turns"]) == 3 and v["willow"]["recent"][0]["state"] == "done"
    # a guest that should run but has no interface is a warning
    s = demo_sources()
    del s["net"]["guests"]["104"]
    assert any(w["title"].startswith("104 proxy") for w in build(s)["warnings"])
    # a dead source is a warning and a red lamp, not a crash
    v = build({"status": OSError("timed out"), "devices": None, "willow": None, "usage": None})
    assert v["warnings"][0]["title"] == "Can't reach the health checks" and {l["name"]: l["level"] for l in v["lamps"]}["Machines"] == "bad"
    # the runner refusing (429) is a lamp, not a broken page (HTTPError pretends to have every attribute)
    import io
    import urllib.error
    s = demo_sources()
    s["usage"] = urllib.error.HTTPError("http://x", 429, "too many", {}, io.BytesIO(b""))
    assert {l["name"]: l for l in build(s)["lamps"]}["Claude"]["text"] == "NO ANSWER"
    # access: localhost, or NPM with Authentik's header; never the LAN, never NPM without the header, never in demo
    assert allowed("127.0.0.1", {}) and allowed(NPM_IP, {"X-authentik-username": "oliver"})
    assert not allowed(NPM_IP, {}) and not allowed("192.168.50.20", {"X-authentik-username": "x"})
    assert not allowed(NPM_IP, {"X-authentik-username": "oliver"}, demo=True)
    assert allowed(NPM_IP, {}, path="/app.css") and allowed(NPM_IP, {}, path="/icons/pulse.png")
    assert not allowed(NPM_IP, {}, path="/api/overview") and not allowed(NPM_IP, {}, path="/api/img/x.png")
    assert not allowed(NPM_IP, {}, path="/") and not allowed("192.168.50.20", {}, path="/app.js")
    for bad in (("approve", "../../x"), ("done", "abc"), ("retry", "76"), ("rm", 1), ("seerr-approve", "1; rm"), ("ack", True),
                ("pass", ""), ("pass", "  "), ("pass", "x" * 2001), ("pass", 5)):
        try:
            act(*bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert PLEX_THUMB.fullmatch("/library/metadata/12602/thumb/1790243614") and not PLEX_THUMB.fullmatch("/library/../etc/passwd")
    assert plex_sub({"media_type": "episode", "parent_media_index": 6, "media_index": 2, "title": "Daddy Issues"}) == "S06 · E02 · Daddy Issues"
    print("ok")


if __name__ == "__main__":
    if "--test" in sys.argv:
        test()
    else:
        Handler.demo = "--demo" in sys.argv
        host = "127.0.0.1" if Handler.demo else "0.0.0.0"
        print(f"nexus on {host}:{PORT}{' (demo data)' if Handler.demo else ''}", flush=True)
        ThreadingHTTPServer((host, PORT), Handler).serve_forever()
