"""Self-check for gaming time from power history. Run from host/:  python test_gaming.py

Covers: dips inside a session (loading screens) don't split it, short spikes
aren't counted, the machine going off ends a session, a server-like trace with
no high state finds nothing, and the summary reads it from the database.
"""
import os
import random
from datetime import datetime, timedelta
from pathlib import Path

DB = Path("data/test_gaming.db")
DB.unlink(missing_ok=True)
os.environ.update(DATABASE_URL=f"sqlite:///./{DB.as_posix()}")

from sqlmodel import Session  # noqa: E402

from app import gaming  # noqa: E402
from app.database import engine, init_db  # noqa: E402
from app.models import Device, ReportHistory  # noqa: E402

random.seed(1)
T0 = datetime(2026, 11, 1)


def trace(spec):
    """spec: (minutes, watts or None for 'off'); one reading a minute with noise."""
    rows, t = [], T0
    for minutes, watts in spec:
        for _ in range(minutes):
            if watts is not None:
                rows.append((t, watts + random.uniform(-6, 6)))
            t += timedelta(minutes=1)
    return rows


idle, game = 45, 190
week = trace([
    (600, idle), (2, 120), (300, idle),                                   # a 2-minute spike: not gaming
    (40, game), (1, idle), (30, game), (3, idle), (50, game), (2, 60),    # 2h session with dips
    (25, game), (400, idle),
    (60, game), (4, idle), (45, game), (500, idle),                       # 1h49 with a 4-minute dip
    (30, game), (20, None), (30, game), (300, idle),                      # off for 20 min: two sessions
    (20, game), (6, idle), (5, game), (800, idle),                        # 6-minute dip ends it; 5 min after is too short
])
line, found = gaming.sessions(week)
assert 100 < line < 140, line
mins = [round((e - s).total_seconds() / 60) for s, e in found]
assert mins == [151, 109, 30, 30, 20], mins

# a server: busy now and then, never a plateau far above idle
server = trace([(300, 60), (30, 85), (300, 60), (60, 90), (300, 60)])
assert gaming.sessions(server) == (None, [])
assert gaming.sessions(week[:5]) == (None, [])

init_db()
with Session(engine) as db:
    dev = Device(name="pc", api_key="k")
    db.add(dev)
    db.commit()
    db.refresh(dev)
    db.add_all(ReportHistory(device_id=dev.id, timestamp=t, power_w=w) for t, w in week)
    db.add(ReportHistory(device_id=dev.id, timestamp=T0 - timedelta(days=9), power_w=400))  # outside the week
    db.commit()
    now = week[-1][0] + timedelta(minutes=1)
    s = gaming.week_summary(db, dev.id, retention_days=7, now=now)
    assert s == {"label": "5.7 h", "sessions": 5, "line_w": s["line_w"], "period": "this week"}, s
    assert gaming.week_summary(db, dev.id, retention_days=3, now=now)["period"] == "last 3 days"

engine.dispose()  # Windows won't delete an open database
DB.unlink(missing_ok=True)
print("gaming self-checks passed")
