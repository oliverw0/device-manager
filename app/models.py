import secrets
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


def generate_api_key() -> str:
    return secrets.token_urlsafe(32)


class User(SQLModel, table=True):
    """Someone who signs in: from OIDC (created on first sign-in) or the
    ADMIN_USERNAME password login. Everyone has full access; disabled can't sign in."""
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    sub: Optional[str] = Field(default=None, unique=True, index=True)  # OIDC subject
    source: str = "oidc"  # oidc | local
    email: str = ""
    name: str = ""
    disabled: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_login_at: Optional[datetime] = None


class AppSetting(SQLModel, table=True):
    """A setting changed in the UI; overrides the .env value of the same name."""
    key: str = Field(primary_key=True)
    value: str = ""


class Device(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    api_key: str = Field(default_factory=generate_api_key, unique=True, index=True)
    notes: str = ""

    created_at: datetime = Field(default_factory=datetime.utcnow)
    report_interval_seconds: int = 60
    offline_after_seconds: int = 150

    last_seen_at: Optional[datetime] = None
    is_online: bool = False

    # last known state, used to detect transitions worth alerting on
    tailscale_connected: Optional[bool] = None
    apt_needs_update: Optional[bool] = None

    # denormalized snapshot of the most recent report, for quick dashboard rendering
    last_report_json: Optional[str] = None

    # source address of the most recent report (fallback SSH target)
    last_report_ip: Optional[str] = None

    # in-browser SSH terminal: off by default; host/port override the address
    # otherwise derived from the device's reported Tailscale IP / MagicDNS name.
    ssh_enabled: bool = False
    ssh_host: Optional[str] = None
    ssh_port: int = 22

    # alerts: mute suppresses ntfy pushes until this time (maintenance windows);
    # alert_state is a JSON object of currently-active threshold alerts ({"disk": true})
    # so a sustained-high condition pings once, not every sweep.
    alerts_muted_until: Optional[datetime] = None
    alert_state: Optional[str] = None

    def alerts_muted(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.utcnow()
        return self.alerts_muted_until is not None and self.alerts_muted_until > now


class ServiceCheck(SQLModel, table=True):
    """An HTTP or TCP health check the monitor runs on an interval. HTTP checks
    pass on any <400 status; TCP checks pass if the port accepts a connection.
    A heartbeat check is the other way round: a job calls /ping/<token> and the
    check is down once interval_seconds + grace_seconds pass without a ping
    (last_checked_at is then the last ping). A pbs check reads a Proxmox Backup
    Server's last 24h of tasks with an API token (secret); it's down if one failed."""
    id: Optional[int] = Field(default=None, primary_key=True)
    device_id: int = Field(foreign_key="device.id", index=True)
    name: str
    kind: str = "http"  # http | tcp | heartbeat | pbs
    target: str = ""    # URL for http, host:port for tcp, unused for heartbeat
    interval_seconds: int = 60
    grace_seconds: int = 0
    token: Optional[str] = Field(default=None, index=True)  # heartbeat ping URL secret
    secret: str = ""   # pbs: API token, USER@REALM!NAME:SECRET
    summary: str = ""  # pbs: what the last 24h of tasks did, for the daily summary
    enabled: bool = True
    cert_expires_at: Optional[datetime] = None  # https checks: the certificate's expiry
    cert_checked_at: Optional[datetime] = None

    last_status: str = "unknown"  # up | down | unknown
    last_checked_at: Optional[datetime] = None
    last_latency_ms: Optional[float] = None
    last_error: Optional[str] = None


class CheckDay(SQLModel, table=True):
    """Per-check, per-day pass count: the Problems page's 30-day uptime bars."""
    id: Optional[int] = Field(default=None, primary_key=True)
    check_id: int = Field(index=True)
    day: str = Field(index=True)  # YYYY-MM-DD, UTC
    ok: int = 0
    total: int = 0


class Problem(SQLModel, table=True):
    """Something wrong, open from when it's first seen until it clears.
    (device_id, key) identifies it while open: "offline", "disk:/",
    "container:mosquitto", "check:5", ..."""
    id: Optional[int] = Field(default=None, primary_key=True)
    device_id: int = Field(index=True)
    key: str
    severity: str  # high | warning | info
    source: str    # Device, Disk /, Container, Check, Heartbeat, Certificate, Updates, Client ...
    title: str
    detail: str = ""
    opened_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    resolved_at: Optional[datetime] = Field(default=None, index=True)
    acked_at: Optional[datetime] = None
    ack_note: str = ""


class ReportHistory(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    device_id: int = Field(foreign_key="device.id", index=True)
    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)

    cpu_percent: float = 0.0
    mem_percent: float = 0.0
    disk_percent: float = 0.0
    power_w: Optional[float] = None  # wall watts, when power tracking is on


class ContainerHistory(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    device_id: int = Field(foreign_key="device.id", index=True)
    container_name: str = Field(index=True)
    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)

    cpu_percent: float = 0.0
    mem_percent: float = 0.0


class AlertEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    device_id: int = Field(foreign_key="device.id", index=True)
    device_name: str
    kind: str  # offline | online | tailscale_down | tailscale_up
    message: str
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
