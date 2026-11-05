"""DNS check: ask one specific server for a name's A record.

A check on AdGuard's web page only proves the web page is up; this proves the
resolver answers. Target "name@server[:port]", e.g. "pulse.wiles.au@192.168.50.146".
Plain UDP, no dependency: the query is a dozen bytes.
"""
import asyncio
import ipaddress
import random
import socket
import struct

RCODES = {1: "format error", 2: "server failure", 3: "no such name (NXDOMAIN)", 4: "not implemented", 5: "refused"}


def parse_target(target: str) -> tuple[str, str, int]:
    name, _, server = target.strip().partition("@")
    if not name or not server:
        raise ValueError("DNS target is name@server, e.g. example.com@192.168.1.1")
    host, port = server, 53
    if server.count(":") == 1:  # IPv4 or hostname with a port; bare IPv6 has several colons
        host, p = server.split(":")
        port = int(p)
    return name.rstrip("."), host.strip("[]"), port


def build_query(name: str, qid: int) -> bytes:
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)  # recursion desired, one question
    qname = b"".join(bytes([len(p)]) + p.encode("idna") for p in name.split(".") if p) + b"\0"
    return header + qname + struct.pack(">HH", 1, 1)  # A, IN


def _skip_name(data: bytes, i: int) -> int:
    while True:
        n = data[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:  # compression pointer
            return i + 2
        i += n + 1


def parse_answer(data: bytes, qid: int) -> list[str]:
    """A-record addresses in the reply. Raises ValueError on an error reply."""
    rid, flags, qd, an, _, _ = struct.unpack(">HHHHHH", data[:12])
    if rid != qid:
        raise ValueError("reply doesn't match the query")
    rcode = flags & 0xF
    if rcode:
        raise ValueError(RCODES.get(rcode, f"error code {rcode}"))
    i = 12
    for _ in range(qd):
        i = _skip_name(data, i) + 4
    out = []
    for _ in range(an):
        i = _skip_name(data, i)
        rtype, _, _, rdlen = struct.unpack(">HHIH", data[i:i + 10])
        i += 10
        if rtype == 1 and rdlen == 4:
            out.append(str(ipaddress.IPv4Address(data[i:i + 4])))
        i += rdlen
    return out


def _query(name: str, host: str, port: int, timeout: float) -> list[str]:
    qid = random.randint(0, 0xFFFF)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(build_query(name, qid), (host, port))
        data, _ = s.recvfrom(4096)
    return parse_answer(data, qid)


async def check(target: str, expect: str = "", timeout: float = 4) -> tuple[bool, str | None]:
    """-> (up, error). Up when the server answers with at least one address
    (and, with expect set, that address is among them)."""
    try:
        name, host, port = parse_target(target)
        addrs = await asyncio.to_thread(_query, name, host, port, timeout)
    except socket.timeout:
        return False, "no answer from the DNS server (timed out)"
    except (OSError, ValueError, struct.error, IndexError) as exc:
        return False, str(exc) or type(exc).__name__
    if not addrs:
        return False, f"{name}: no A record in the answer"
    if expect and expect.strip() not in addrs:
        return False, f"{name} resolved to {', '.join(addrs)}, expected {expect.strip()}"
    return True, None


if __name__ == "__main__":  # ponytail: self-check
    assert parse_target("a.example@10.0.0.1") == ("a.example", "10.0.0.1", 53)
    assert parse_target("a.example.@10.0.0.1:5353") == ("a.example", "10.0.0.1", 5353)
    q = build_query("pulse.wiles.au", 0x1234)
    assert q[:2] == b"\x12\x34" and b"\x05pulse\x05wiles\x02au\x00" in q
    # reply: same id, flags 0x8180, 1 question, 1 answer (name by pointer to offset 12)
    reply = (struct.pack(">HHHHHH", 0x1234, 0x8180, 1, 1, 0, 0) + q[12:]
             + b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 300, 4) + bytes([192, 168, 50, 225]))
    assert parse_answer(reply, 0x1234) == ["192.168.50.225"]
    nx = struct.pack(">HHHHHH", 7, 0x8183, 1, 0, 0, 0) + q[12:]
    try:
        parse_answer(nx, 7)
        raise AssertionError("NXDOMAIN should raise")
    except ValueError as exc:
        assert "NXDOMAIN" in str(exc)
    print("ok")
