import secrets
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _clean_env_value(value: str) -> str:
    """Normalize a value pulled from an env var/.env file.

    Different loaders disagree on quoting: python-dotenv strips matching
    wrapping quotes, but Docker Compose's `env_file` and some systemd
    versions pass them through literally. Stripping here means the same
    .env file behaves the same way regardless of which one loaded it, and
    a stray trailing \\r (Windows-edited file mounted into Linux) or
    accidental whitespace from copy-paste doesn't silently change the value.
    """
    value = value.strip().strip("\r\n")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str = "sqlite:///./data/devicemanager.db"

    ntfy_url: str = ""  # full topic URL, e.g. https://ntfy.sh/my-private-topic
    ntfy_default_priority: str = "default"

    admin_username: str = "admin"
    admin_password: str = "change-me"
    # Alternative to ADMIN_PASSWORD: path to a file containing just the password.
    # Docker Compose applies ${VAR} interpolation to env_file values, so a literal
    # "$" in ADMIN_PASSWORD gets silently mangled (e.g. "p@ss$Kword" -> "p@ss")
    # unless escaped as "$$". A mounted file's contents aren't parsed by Compose
    # at all, so this sidesteps that entirely - use it if your password has a $ in it.
    admin_password_file: str = ""

    check_interval_seconds: int = 15
    history_retention_days: int = 7

    # apt staleness: flag a device (orange badge + ntfy) when its cache hasn't
    # been refreshed in this many days, OR this many packages are upgradable.
    apt_stale_days: int = 30
    apt_upgradable_threshold: int = 50

    # Resource threshold alerts: ntfy once when a metric stays at/above the
    # percentage for alert_sustained_minutes straight (set a threshold to 0 to
    # disable that metric). Sustained, not spiky — a brief peak won't page you.
    alert_cpu_percent: int = 95
    alert_mem_percent: int = 90
    alert_disk_percent: int = 90
    alert_sustained_minutes: int = 10

    # Problems: warn when an https check's certificate expires within this many
    # days (high within 3). The daily summary goes to ntfy at this hour, host
    # local time (set TZ on the container); -1 turns it off.
    tls_warn_days: int = 14
    digest_hour: int = 8

    # High problems nobody has acknowledged get a "still open" push every this
    # many hours (0 turns reminders off).
    reminder_hours: int = 8

    # Watchdog: a URL Pulse calls after every healthy sweep (healthchecks.io,
    # an Uptime Kuma push monitor, ...), so something outside the lab notices when
    # Pulse itself, its machine or its alerting stops. Skipped while the last
    # ntfy push failed, so a broken alert channel also shows up there.
    watchdog_url: str = ""
    watchdog_interval_seconds: int = 60

    # Alert storms: when at least this many devices (and most of them) go quiet
    # in the same sweep, send one push about Pulse's own network instead of one each.
    storm_min_devices: int = 3

    # SSH: alert when this many failed logins land in one report's window
    # (SSH_LOG_WINDOW_MINUTES on the client; 0 = off), and on any successful
    # login from an address outside private, Tailscale and loopback ranges.
    ssh_failed_alert: int = 20
    ssh_outside_login_alert: bool = True

    # PBS checks: a backup group whose newest snapshot is older than this many
    # hours is late; older than PBS_RETIRED_DAYS it's taken as a removed guest.
    pbs_backup_max_age_hours: int = 26
    pbs_retired_days: int = 14

    # Hourly averages are kept this long for the 30-day / 90-day / 1-year charts.
    long_history_days: int = 400

    # Bearer token for /metrics (Prometheus) and /api/v1/summary (Homepage
    # widget). Empty: both are off.
    metrics_token: str = ""
    # Bearer token for /mcp, Pulse's MCP server (Willow, Claude Code). Empty: off.
    mcp_token: str = ""
    # A read-only status page at /status/public, outside the login: check
    # names and uptime only, no addresses or device names.
    public_status: bool = False

    # Sign in with OpenID Connect (e.g. authentik). Set OIDC_ISSUER to the
    # provider's issuer URL (authentik: https://auth.example.com/application/o/<slug>/)
    # plus the client id/secret; the redirect URI to register is
    # <PUBLIC_URL>/login/oidc/callback. First sign-in creates the user unless
    # OIDC_AUTO_CREATE is false; OIDC_ALLOWED_GROUP limits sign-in to one group.
    # PASSWORD_LOGIN=false hides the ADMIN_USERNAME form once OIDC works.
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_scopes: str = "openid email profile"
    oidc_name: str = "authentik"
    oidc_auto_create: bool = True
    oidc_allowed_group: str = ""
    password_login: bool = True
    # The dashboard's own address as browsers see it (https://dm.example.com),
    # for the OIDC redirect URI behind a proxy. Empty: taken from each request.
    public_url: str = ""

    # Set to true ONLY when the browser reaches the app over HTTPS (e.g. behind
    # a TLS-terminating proxy). Over plain http:// the browser silently drops a
    # Secure cookie, so the session never persists and login loops back to /login.
    session_https_only: bool = False

    # Leave unset: auto-generated on first run and persisted to data/session_secret.key.
    # Only set this yourself if you need the same secret across multiple host instances.
    session_secret: str = ""

    @field_validator("admin_username", "admin_password", "admin_password_file", "session_secret", mode="before")
    @classmethod
    def _sanitize(cls, v):
        return _clean_env_value(v) if isinstance(v, str) else v

    def data_dir(self) -> Path:
        path = Path("./data")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolved_admin_password(self) -> str:
        if self.admin_password_file:
            return _clean_env_value(Path(self.admin_password_file).read_text(encoding="utf-8"))
        return self.admin_password

    def resolved_session_secret(self) -> str:
        if self.session_secret:
            return self.session_secret
        secret_path = self.data_dir() / "session_secret.key"
        if secret_path.exists():
            return secret_path.read_text(encoding="utf-8").strip()
        new_secret = secrets.token_hex(32)
        secret_path.write_text(new_secret, encoding="utf-8")
        return new_secret


settings = Settings()
