from sqlmodel import SQLModel, Session, create_engine
from sqlalchemy import text

from .config import settings

settings.data_dir()

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
)


# Columns added to existing tables after first release. create_all() only creates
# missing *tables*, never new columns, so we ALTER them in for older SQLite DBs.
_COLUMN_MIGRATIONS = {
    "device": {
        "ssh_enabled": "ALTER TABLE device ADD COLUMN ssh_enabled BOOLEAN NOT NULL DEFAULT 0",
        "ssh_host": "ALTER TABLE device ADD COLUMN ssh_host VARCHAR",
        "ssh_port": "ALTER TABLE device ADD COLUMN ssh_port INTEGER NOT NULL DEFAULT 22",
        "last_report_ip": "ALTER TABLE device ADD COLUMN last_report_ip VARCHAR",
        "apt_needs_update": "ALTER TABLE device ADD COLUMN apt_needs_update BOOLEAN",
        "alerts_muted_until": "ALTER TABLE device ADD COLUMN alerts_muted_until DATETIME",
        "alert_state": "ALTER TABLE device ADD COLUMN alert_state VARCHAR",
    },
}


def _apply_column_migrations() -> None:
    if "sqlite" not in settings.database_url:
        return
    with engine.begin() as conn:
        for table, columns in _COLUMN_MIGRATIONS.items():
            existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            if not existing:
                continue  # table doesn't exist yet; create_all handles it
            for column, ddl in columns.items():
                if column not in existing:
                    conn.execute(text(ddl))


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _apply_column_migrations()


def get_session():
    with Session(engine) as session:
        yield session
