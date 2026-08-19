import secrets
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


def generate_api_key() -> str:
    return secrets.token_urlsafe(32)


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
    pass on any <400 status; TCP checks pass if the port accepts a connection."""
    id: Optional[int] = Field(default=None, primary_key=True)
    device_id: int = Field(foreign_key="device.id", index=True)
    name: str
    kind: str = "http"  # http | tcp
    target: str         # URL for http, host:port for tcp
    interval_seconds: int = 60
    enabled: bool = True

    last_status: str = "unknown"  # up | down | unknown
    last_checked_at: Optional[datetime] = None
    last_latency_ms: Optional[float] = None
    last_error: Optional[str] = None


class ReportHistory(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    device_id: int = Field(foreign_key="device.id", index=True)
    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)

    cpu_percent: float = 0.0
    mem_percent: float = 0.0
    disk_percent: float = 0.0


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
