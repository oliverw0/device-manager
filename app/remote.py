"""Commands the host runs on a device over SSH, per OS.

Linux devices get the original POSIX shell commands. Windows devices get
PowerShell sent with -EncodedCommand (base64 UTF-16LE), which runs the same
whether sshd's DefaultShell is cmd.exe (the Windows default) or PowerShell,
and means no quoting survives to be mangled by either.

Container names passed in here are always whitelisted against the device's
last report first (see terminal._known_container), so they're a safe charset.
"""
import base64
import json

import asyncssh

from . import ssh_keys

# Where install.ps1 puts the client; "$env:ProgramFiles" expands on the device.
WIN_CLIENT_DIR = r"$env:ProgramFiles\DeviceManagerClient"
# The client repo, and its main branch's raw files (also where the latest version is read).
CLIENT_REPO = "https://github.com/oliverw0/device-manager-client"
CLIENT_RAW = "https://raw.githubusercontent.com/oliverw0/device-manager-client/main"


def _report(device) -> dict:
    return (json.loads(device.last_report_json) if device.last_report_json else None) or {}


def device_os(device) -> str:
    """"windows" or "linux". Clients from before OS reporting are all Linux."""
    return "windows" if (_report(device).get("system") or {}).get("os") == "windows" else "linux"


def is_windows(device) -> bool:
    return device_os(device) == "windows"


def _windows_info(device) -> dict:
    return _report(device).get("windows") or {}


def expected_hostname(device) -> str | None:
    """The machine name the device reported, to check we SSH'd into the right box."""
    return (_report(device).get("system") or {}).get("hostname") or None


class WrongMachine(OSError):
    """Connected, but to a different machine than the device (e.g. an address
    that belongs to someone else on this network). Callers try the next address."""


def describe_error(exc: BaseException) -> str:
    """Readable reason an SSH attempt failed. Timeouts have an empty str(), which
    used to show up as "Could not reach device ()"."""
    if isinstance(exc, asyncssh.PermissionDenied):
        return "key not authorized for this user"
    if isinstance(exc, TimeoutError):
        return "timed out"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused (is SSH enabled?)"
    return str(exc) or type(exc).__name__


def unreachable(tried: list[str]) -> str:
    """'Could not reach device: 100.64.0.9: timed out; 192.168.50.4: …'"""
    return "Could not reach device: " + ("; ".join(tried) if tried else "no address to try")


def _short(name: str) -> str:
    return name.strip().split(".")[0].lower()


async def connect(addr: str, port: int, user: str, expected: str | None):
    """asyncssh.connect, then confirm the machine is the device by hostname.
    Host keys aren't pinned (no known_hosts yet), so without this an address
    that now points at another machine (a Docker bridge IP, a reused DHCP lease)
    silently opens a shell — or runs an update — on the wrong box."""
    conn = await asyncssh.connect(
        addr, port=port, username=user,
        client_keys=[ssh_keys.private_key_path()], known_hosts=None, connect_timeout=8,
    )
    if expected:
        try:
            r = await conn.run("hostname", check=False, timeout=10)
            actual = (r.stdout or "").strip() if r.exit_status == 0 else ""
        except (OSError, asyncssh.Error):
            actual = ""  # couldn't ask: don't block a working connection on it
        if actual and _short(actual) != _short(expected):
            conn.close()
            raise WrongMachine(f"{addr} is {actual}, not {expected}")
    return conn


def powershell(script: str, exe: str = "powershell", interactive: bool = False) -> str:
    """A command line that runs `script` in PowerShell and exits with its
    $LASTEXITCODE-aware status."""
    prelude = "$ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding=[Text.Encoding]::UTF8\n"
    encoded = base64.b64encode((prelude + script).encode("utf-16-le")).decode("ascii")
    flags = "-NoLogo -NoProfile -ExecutionPolicy Bypass -OutputFormat Text" + ("" if interactive else " -NonInteractive")
    return f"{exe} {flags} -EncodedCommand {encoded}"


def update_user(device) -> str:
    """Account for package/client updates: root on Linux; on Windows the admin
    account the client reported (the one install.ps1 authorized)."""
    if not is_windows(device):
        return "root"
    info = _windows_info(device)
    users = _report(device).get("ssh_users") or []
    return info.get("admin_user") or (users[0] if users else "Administrator")


def shell_command(device):
    """Interactive terminal: None means sshd's login shell (Linux). Windows
    always opens PowerShell, 7 (pwsh) when the client found it installed."""
    if not is_windows(device):
        return None
    exe = "pwsh" if _windows_info(device).get("shell") == "pwsh" else "powershell"
    return f"{exe} -NoLogo"


def container_exec_command(device, name: str) -> str:
    if not is_windows(device):
        return f"docker exec -it {name} /bin/bash 2>/dev/null || docker exec -it {name} /bin/sh"
    # Fall back only when the shell isn't in the image (docker exits 126/127),
    # not when the user's last command failed. Windows containers have neither
    # bash nor sh, so try their shells last.
    script = (
        f"foreach ($sh in 'bash', 'sh', 'powershell', 'cmd') {{ "
        f"docker exec -it {name} $sh; if ($LASTEXITCODE -ne 126 -and $LASTEXITCODE -ne 127) {{ break }} }}"
    )
    return powershell(script, interactive=True)


def container_action_command(device, action: str, name: str) -> str:
    if not is_windows(device):
        return f"docker {action} {name}"
    return powershell(f"docker {action} {name}; exit $LASTEXITCODE")


def container_logs_command(device, name: str) -> str:
    if not is_windows(device):
        return f"docker logs --tail 200 {name} 2>&1"
    # cmd merges stderr at the OS level; PowerShell 5.1 would wrap each stderr
    # line in an error record instead.
    return powershell(f"cmd /c 'docker logs --tail 200 {name} 2>&1'; exit $LASTEXITCODE")


def windows_update_command(kind: str) -> str:
    """kind "apt" (OS packages -> Windows Update) or "client".

    A client update runs the LATEST install.ps1 from GitHub, not the installed
    copy, so fixes to the updater itself apply on the first update; the
    installed copy is the fallback when GitHub can't be reached."""
    if kind == "apt":
        return powershell(f'& "{WIN_CLIENT_DIR}\\windows-update.ps1"; exit $LASTEXITCODE')
    return powershell(
        "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 'Tls12'\n"
        "$p = Join-Path $env:TEMP 'dm-install-latest.ps1'\n"
        f"try {{ Invoke-WebRequest -UseBasicParsing -Uri '{CLIENT_RAW}/install.ps1' -OutFile $p }}\n"
        f'catch {{ Write-Output "Using the installed updater ($($_.Exception.Message))"; $p = "{WIN_CLIENT_DIR}\\install.ps1" }}\n'
        "& $p -Update; exit $LASTEXITCODE"
    )


def display_path(path: str) -> str:
    """SFTP on Windows reports C:\\Users\\x as /C:/Users/x; show it natively."""
    if len(path) > 3 and path[0] == "/" and path[2] == ":":
        return path[1:].replace("/", "\\")
    return path


if __name__ == "__main__":  # ponytail: self-check
    class D:
        def __init__(self, report):
            self.last_report_json = json.dumps(report) if report is not None else None

    linux = D({"system": {"os": "linux"}})
    legacy = D({"system": {}})
    win = D({"system": {"os": "windows"}, "ssh_users": ["oliver", "bob"],
             "windows": {"shell": "pwsh", "admin_user": "oliver"}})
    assert device_os(D(None)) == "linux" and device_os(legacy) == "linux" and is_windows(win)
    assert update_user(linux) == "root" and update_user(win) == "oliver"
    assert update_user(D({"system": {"os": "windows"}, "ssh_users": ["bob"]})) == "bob"
    assert shell_command(linux) is None and shell_command(win) == "pwsh -NoLogo"
    assert container_action_command(linux, "stop", "plex") == "docker stop plex"

    cmd = container_logs_command(win, "plex")
    exe, *_, b64 = cmd.split()
    decoded = base64.b64decode(b64).decode("utf-16-le")
    assert exe == "powershell" and "-NonInteractive" in cmd
    assert "cmd /c 'docker logs --tail 200 plex 2>&1'; exit $LASTEXITCODE" in decoded
    assert "-NonInteractive" not in container_exec_command(win, "plex")
    upd = base64.b64decode(windows_update_command("client").split()[-1]).decode("utf-16-le")
    assert f"-Uri '{CLIENT_RAW}/install.ps1'" in upd and "& $p -Update; exit $LASTEXITCODE" in upd, upd
    assert '$p = "$env:ProgramFiles\\DeviceManagerClient\\install.ps1"' in upd, upd  # offline fallback
    apt = base64.b64decode(windows_update_command("apt").split()[-1]).decode("utf-16-le")
    assert '& "$env:ProgramFiles\\DeviceManagerClient\\windows-update.ps1"; exit $LASTEXITCODE' in apt, apt
    assert display_path("/C:/Users/oliver/a.txt") == "C:\\Users\\oliver\\a.txt"
    assert display_path("/root/a.txt") == "/root/a.txt"

    import asyncio
    from types import SimpleNamespace

    class FakeConn:
        def __init__(self, name, code=0):
            self.name, self.code, self.closed = name, code, False
        async def run(self, cmd, **kw):
            return SimpleNamespace(stdout=self.name + "\r\n", exit_status=self.code)
        def close(self):
            self.closed = True

    def with_box(conn):
        async def fake_connect(*a, **kw):
            return conn
        asyncssh.connect = fake_connect
        ssh_keys.private_key_path = lambda: "k"
        return conn

    async def check():
        c = with_box(FakeConn("frigate-lxc"))
        assert await connect("10.0.0.5", 22, "root", "frigate-lxc.lan") is c          # right box (FQDN vs short)
        c = with_box(FakeConn("MAC-DEB"))
        try:
            await connect("172.17.0.2", 22, "root", "frigate-lxc")
            raise AssertionError("connected to the wrong machine")
        except WrongMachine as exc:
            assert c.closed and "is MAC-DEB, not frigate-lxc" in str(exc)
        c = with_box(FakeConn("", code=1))
        assert await connect("10.0.0.5", 22, "root", "frigate-lxc") is c               # can't tell: allow
        assert await connect("10.0.0.5", 22, "root", None) is c                        # no reported name: allow
        assert isinstance(WrongMachine("x"), OSError)                                  # callers' except catches it
    asyncio.run(check())
    print("ok")
