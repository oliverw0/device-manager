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
        "parent_id": "ALTER TABLE device ADD COLUMN parent_id INTEGER",
        "offline_quiet": "ALTER TABLE device ADD COLUMN offline_quiet BOOLEAN NOT NULL DEFAULT 0",
        "alert_cpu_percent": "ALTER TABLE device ADD COLUMN alert_cpu_percent INTEGER",
        "alert_mem_percent": "ALTER TABLE device ADD COLUMN alert_mem_percent INTEGER",
        "alert_disk_percent": "ALTER TABLE device ADD COLUMN alert_disk_percent INTEGER",
        "container_restarts": "ALTER TABLE device ADD COLUMN container_restarts VARCHAR",
        "offline_ok": "ALTER TABLE device ADD COLUMN offline_ok BOOLEAN NOT NULL DEFAULT 0",
    },
    "servicecheck": {
        "grace_seconds": "ALTER TABLE servicecheck ADD COLUMN grace_seconds INTEGER NOT NULL DEFAULT 0",
        "token": "ALTER TABLE servicecheck ADD COLUMN token VARCHAR",
        "cert_expires_at": "ALTER TABLE servicecheck ADD COLUMN cert_expires_at DATETIME",
        "cert_checked_at": "ALTER TABLE servicecheck ADD COLUMN cert_checked_at DATETIME",
        "secret": "ALTER TABLE servicecheck ADD COLUMN secret VARCHAR NOT NULL DEFAULT ''",
        "summary": "ALTER TABLE servicecheck ADD COLUMN summary VARCHAR NOT NULL DEFAULT ''",
        "expect": "ALTER TABLE servicecheck ADD COLUMN expect VARCHAR NOT NULL DEFAULT ''",
        "group": "ALTER TABLE servicecheck ADD COLUMN \"group\" VARCHAR NOT NULL DEFAULT ''",
        "issues": "ALTER TABLE servicecheck ADD COLUMN issues VARCHAR NOT NULL DEFAULT ''",
    },
    "problem": {
        "reminded_at": "ALTER TABLE problem ADD COLUMN reminded_at DATETIME",
    },
    "reporthistory": {
        "power_w": "ALTER TABLE reporthistory ADD COLUMN power_w FLOAT",
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
