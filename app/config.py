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

    check_interval_seconds: int = 15
    history_retention_days: int = 7

    session_secret: str = "change-me-too"

    @field_validator("admin_username", "admin_password", "session_secret", mode="before")
    @classmethod
    def _sanitize(cls, v):
        return _clean_env_value(v) if isinstance(v, str) else v

    def data_dir(self) -> Path:
        path = Path("./data")
        path.mkdir(parents=True, exist_ok=True)
        return path


settings = Settings()
