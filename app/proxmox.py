"""Proxmox VE: what the host's own client can't see, read from the PVE API
with a read-only token (role PVEAuditor on /).

- storage filling up (local-lvm, ZFS pools, NFS mounts; anything PVE lists)
- LVM thin pool metadata filling up: a full thin pool, data or metadata, puts
  every guest on it into I/O errors
- disks whose SMART health isn't PASSED/OK
- guests set to start at boot that are stopped
- cluster nodes that are offline

The 'proxmox' check runs check() on its interval; each finding is its own problem.
"""
from urllib.parse import urlsplit

import httpx


class PveError(Exception):
    """PVE couldn't be read; the message says why."""


def normalize_url(target: str) -> str:
    """'pve.lan' or '10.0.0.5' -> https://host:8006 (PVE's port)."""
    url = target.strip().rstrip("/")
    url = url if "://" in url else "https://" + url
    return url if urlsplit(url).port else url + ":8006"


def auth_header(token: str) -> str:
    """PVE wants USER@REALM!NAME=SECRET; PBS's form uses ':' before the secret. Take either."""
    token = token.strip()
    if "=" not in token and "!" in token:
        head, _, tail = token.partition("!")
        name, sep, secret = tail.partition(":")
        token = f"{head}!{name}={secret}" if sep else token
    return f"PVEAPIToken={token}"


def _issue(key, severity, title, detail=""):
    return {"key": key, "severity": severity, "title": title, "detail": detail}


def _gib(b) -> str:
    return f"{(b or 0) / 1024 ** 3:.0f} GiB"


def assess(nodes: list[dict], per_node: dict[str, dict], full_percent: int = 90) -> tuple[str, list[dict]]:
    """per_node: {node: {"storage": [...], "thin": [...], "disks": [...], "guests": [...]}},
    guests as {vmid, name, type, status, template, onboot}. -> (summary, issues)."""
    issues = []
    stopped = storage_n = 0
    worst = None
    for n in nodes:
        name = n.get("node")
        if n.get("status") not in (None, "online"):
            issues.append(_issue(f"node:{name}", "high", f"Node {name} is {n.get('status')}"))
            continue
        d = per_node.get(name) or {}
        for st in d.get("storage") or []:
            if not st.get("active") or not st.get("total"):
                continue
            storage_n += 1
            pct = (st.get("used") or 0) / st["total"] * 100
            worst = max(worst or 0, pct)
            if pct >= full_percent > 0:
                issues.append(_issue(f"storage:{name}/{st.get('storage')}", "high" if pct >= 95 else "warning",
                                     f"Storage {st.get('storage')} is {pct:.0f}% full",
                                     f"{st.get('type')} on {name}: {_gib(st.get('avail'))} left of {_gib(st['total'])}."))
        for t in d.get("thin") or []:
            if t.get("metadata_size"):
                pct = (t.get("metadata_used") or 0) / t["metadata_size"] * 100
                if pct >= min(full_percent, 80) > 0:  # metadata runs out with no warning from PVE
                    issues.append(_issue(f"thinmeta:{name}/{t.get('vg')}/{t.get('lv')}", "high" if pct >= 90 else "warning",
                                         f"Thin pool {t.get('vg')}/{t.get('lv')} metadata {pct:.0f}% full",
                                         "When thin pool metadata fills up, every guest on the pool gets I/O errors. "
                                         "Grow it with lvextend --poolmetadatasize."))
        for k in d.get("disks") or []:
            health = (k.get("health") or "").upper()
            if health and health not in ("PASSED", "OK", "UNKNOWN"):
                issues.append(_issue(f"smart:{name}:{k.get('devpath')}", "high",
                                     f"Disk {k.get('devpath')} SMART health: {k.get('health')}",
                                     f"{k.get('model') or ''} on {name}. Back up what's on it and plan a replacement.".strip()))
        for g in d.get("guests") or []:
            if g.get("template") or g.get("status") == "running":
                continue
            if g.get("onboot"):
                stopped += 1
                label = g.get("name") or f"{g.get('type')}/{g.get('vmid')}"
                issues.append(_issue(f"guest:{name}:{g.get('vmid')}", "warning",
                                     f"{label} ({g.get('vmid')}) is {g.get('status') or 'stopped'}",
                                     f"Set to start at boot on {name}, but not running. Start it, or turn off Start at boot."))
    guests = sum(len((per_node.get(n.get("node")) or {}).get("guests") or []) for n in nodes)
    running = sum(1 for n in nodes for g in ((per_node.get(n.get("node")) or {}).get("guests") or []) if g.get("status") == "running")
    parts = [f"{running}/{guests} guests running"]
    if worst is not None:
        parts.append(f"fullest storage {worst:.0f}%")
    if stopped:
        parts.append(f"{stopped} stopped that should run")
    return ", ".join(parts), issues


async def check(url: str, token: str, full_percent: int = 90) -> tuple[bool, str, str | None, list[dict]]:
    """-> (all ok, summary, error text, issues). Raises PveError in plain words."""
    try:
        # PVE ships a self-signed certificate. ponytail: pin its fingerprint if the LAN isn't trusted
        async with httpx.AsyncClient(timeout=15, verify=False, headers={"Authorization": auth_header(token)}) as c:
            async def get(path, required=False):
                r = await c.get(f"{url}/api2/json{path}")
                if r.status_code == 401:
                    raise PveError(f"Proxmox at {url} refused the API token (401). Check it's USER@REALM!NAME=SECRET.")
                if r.status_code >= 400:
                    if required:
                        raise PveError(f"Proxmox at {url} answered {r.status_code} for {path}: {r.text[:150]}")
                    return []
                return r.json().get("data") or []

            nodes = await get("/nodes", required=True)
            if not nodes:
                raise PveError(f"Proxmox at {url} lists no nodes: give the token the PVEAuditor role on /.")
            per_node = {}
            for n in nodes:
                name = n.get("node")
                if n.get("status") not in (None, "online"):
                    continue
                guests = []
                for kind in ("lxc", "qemu"):
                    for g in await get(f"/nodes/{name}/{kind}"):
                        g["type"] = kind
                        if g.get("status") != "running" and not g.get("template"):
                            cfg = await get(f"/nodes/{name}/{kind}/{g.get('vmid')}/config")
                            g["onboot"] = bool(int((cfg or {}).get("onboot") or 0)) if isinstance(cfg, dict) else False
                        guests.append(g)
                per_node[name] = {
                    "storage": await get(f"/nodes/{name}/storage"),
                    "thin": await get(f"/nodes/{name}/disks/lvmthin"),
                    "disks": await get(f"/nodes/{name}/disks/list"),
                    "guests": guests,
                }
    except httpx.HTTPError as exc:
        raise PveError(f"Couldn't reach Proxmox at {url}: {exc or type(exc).__name__}") from exc
    summary, issues = assess(nodes, per_node, full_percent)
    error = "; ".join(i["title"] for i in issues[:3]) + (f" (+{len(issues) - 3} more)" if len(issues) > 3 else "") if issues else None
    return not issues, summary, error, issues


if __name__ == "__main__":  # ponytail: self-check
    assert normalize_url("pve.lan") == "https://pve.lan:8006"
    assert auth_header("root@pam!pulse:abc-123") == "PVEAPIToken=root@pam!pulse=abc-123"
    assert auth_header("root@pam!pulse=abc-123") == "PVEAPIToken=root@pam!pulse=abc-123"
    G = 1024 ** 3
    nodes = [{"node": "pve", "status": "online"}, {"node": "pve2", "status": "offline"}]
    per = {"pve": {
        "storage": [{"storage": "local-lvm", "type": "lvmthin", "active": 1, "total": 141 * G, "used": 134 * G, "avail": 7 * G},
                    {"storage": "local", "type": "dir", "active": 1, "total": 100 * G, "used": 20 * G, "avail": 80 * G},
                    {"storage": "hdd1tb", "type": "dir", "active": 0, "total": 0}],
        "thin": [{"vg": "pve", "lv": "data", "metadata_size": 1000, "metadata_used": 850}],
        "disks": [{"devpath": "/dev/sda", "health": "PASSED"}, {"devpath": "/dev/sdb", "health": "FAILED", "model": "WD"},
                  {"devpath": "/dev/nvme0n1", "health": "OK"}],
        "guests": [{"vmid": 101, "name": "authentik", "type": "lxc", "status": "running"},
                   {"vmid": 105, "name": "adguard", "type": "lxc", "status": "stopped", "onboot": True},
                   {"vmid": 115, "name": "crafty", "type": "lxc", "status": "stopped", "onboot": False},
                   {"vmid": 9000, "name": "tmpl", "type": "qemu", "status": "stopped", "template": 1, "onboot": True}],
    }}
    summary, issues = assess(nodes, per)
    keys = {i["key"]: i for i in issues}
    assert set(keys) == {"node:pve2", "storage:pve/local-lvm", "thinmeta:pve/pve/data", "smart:pve:/dev/sdb", "guest:pve:105"}, keys
    assert keys["storage:pve/local-lvm"]["title"] == "Storage local-lvm is 95% full" and keys["storage:pve/local-lvm"]["severity"] == "high"
    assert keys["thinmeta:pve/pve/data"]["severity"] == "warning"
    assert keys["guest:pve:105"]["title"] == "adguard (105) is stopped"
    assert summary == "1/4 guests running, fullest storage 95%, 1 stopped that should run", summary
    print("ok")
