from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    def data_dir(self) -> Path:
        path = Path("./data")
        path.mkdir(parents=True, exist_ok=True)
        return path


settings = Settings()
