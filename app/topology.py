"""Which device runs on which: an LXC or VM on its Proxmox host.

Set by hand on a device's Settings tab (Device.parent_id), or else taken from
the host's power report, which lists its guests by name. Used to fold a guest's
offline alert into its host's, and for the Map page.
"""
import json

from .models import Device


def _names(d: Device, rep: dict) -> set[str]:
    out = {d.name.lower()}
    host = (rep.get("system") or {}).get("hostname")
    if host:
        out.add(host.lower())
    return out


def parents(devices: list[Device]) -> dict[int, int]:
    """{child id: parent id}. A hand-set parent wins; never a cycle or self."""
    reports = {d.id: (json.loads(d.last_report_json) if d.last_report_json else None) or {} for d in devices}
    by_name: dict[str, int] = {}
    for d in devices:
        for n in _names(d, reports[d.id]):
            by_name.setdefault(n, d.id)
    out: dict[int, int] = {}
    for host in devices:
        for g in (reports[host.id].get("power") or {}).get("guests") or []:
            child = by_name.get((g.get("name") or "").lower())
            if child and child != host.id:
                out.setdefault(child, host.id)
    ids = {d.id for d in devices}
    for d in devices:
        if d.parent_id in ids and d.parent_id != d.id:
            out[d.id] = d.parent_id
        elif d.parent_id == 0:  # explicitly "none"
            out.pop(d.id, None)
    # drop anything that loops back on itself
    for child in list(out):
        seen, cur = {child}, out.get(child)
        while cur is not None:
            if cur in seen:
                out.pop(child, None)
                break
            seen.add(cur)
            cur = out.get(cur)
    return out


def inferred_parent(devices: list[Device], device: Device) -> int | None:
    """The parent the power report gives, ignoring a hand-set one."""
    saved = device.parent_id
    device.parent_id = None
    try:
        return parents(devices).get(device.id)
    finally:
        device.parent_id = saved


if __name__ == "__main__":  # ponytail: self-check
    pve = Device(id=1, name="pve", last_report_json=json.dumps({"power": {"guests": [
        {"name": "services", "vmid": 103}, {"name": "plex-server", "vmid": 109}, {"name": "gone", "vmid": 1}]}}))
    lxc = Device(id=2, name="LXC 103", last_report_json=json.dumps({"system": {"hostname": "services"}}))
    plex = Device(id=3, name="plex-server")
    mac = Device(id=4, name="mac-deb", parent_id=0)
    odd = Device(id=5, name="odd", parent_id=4)
    assert parents([pve, lxc, plex, mac, odd]) == {2: 1, 3: 1, 5: 4}
    plex.parent_id = 0  # "none" overrides the power report
    assert parents([pve, lxc, plex, mac, odd]) == {2: 1, 5: 4}
    assert inferred_parent([pve, lxc, plex, mac, odd], plex) == 1 and plex.parent_id == 0
    a, b = Device(id=6, name="a", parent_id=7), Device(id=7, name="b", parent_id=6)
    assert parents([a, b]) in ({7: 6}, {6: 7})  # a loop loses one link
    print("ok")
