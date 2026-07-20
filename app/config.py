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
