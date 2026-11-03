"""Time spent gaming, read off the shape of a machine's power history.

No game detection: a gaming PC sits at idle draw most of the time and jumps to a
plateau well above it while a game runs. Idle is the 20th percentile of the
window's readings and the plateau is near the top (99th percentile); the line
between them is halfway. Readings over the line make a session, and dips back
under it (loading screens, menus, alt-tab, sensor noise) are bridged as long as
they last no more than BRIDGE. Sessions shorter than MIN_SESSION are spikes
(updates, an app starting), not gaming.
"""
from datetime import datetime, timedelta
from statistics import median

from sqlmodel import Session, select

from .models import ReportHistory

WINDOW_DAYS = 7
BRIDGE = timedelta(minutes=5)
MIN_SESSION = timedelta(minutes=15)
OFFLINE_GAP = timedelta(minutes=10)  # no report for this long: off or asleep, so a session ends
MIN_SWING_W = 40.0  # plateau must sit this far above idle, else there's no gaming to find


def _pct(sorted_vals: list[float], q: float) -> float:
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))]


def sessions(rows: list[tuple[datetime, float]]) -> tuple[float | None, list[tuple[datetime, datetime]]]:
    """rows: (timestamp, wall watts), oldest first. Returns the watts line used
    (None when there's no clear high state) and the (start, end) of each session."""
    rows = [(t, w) for t, w in rows if w is not None]
    if len(rows) < 10:
        return None, []
    vals = sorted(w for _, w in rows)
    idle, high = _pct(vals, 0.2), _pct(vals, 0.99)
    if high - idle < MIN_SWING_W:
        return None, []
    line = (idle + high) / 2
    # one reading stands for one report interval; count the last one in a session too
    step = min(timedelta(minutes=5), median(b[0] - a[0] for a, b in zip(rows, rows[1:])))

    found, start, last_high, prev_t = [], None, None, None

    def close():
        end = last_high + step
        if end - start >= MIN_SESSION:
            found.append((start, end))

    for t, w in rows:
        if start is not None and (t - prev_t > OFFLINE_GAP or t - last_high > BRIDGE):
            close()
            start = None
        if w >= line:
            start = start or t
            last_high = t
        prev_t = t
    if start is not None:
        close()
    return line, found


def week_summary(db: Session, device_id: int, retention_days: int, now: datetime | None = None) -> dict | None:
    """Gaming time over the last WINDOW_DAYS (or less, if history isn't kept that long)."""
    days = min(WINDOW_DAYS, retention_days)
    since = (now or datetime.utcnow()) - timedelta(days=days)
    rows = db.exec(
        select(ReportHistory.timestamp, ReportHistory.power_w)
        .where(ReportHistory.device_id == device_id, ReportHistory.timestamp >= since, ReportHistory.power_w != None)  # noqa: E711
        .order_by(ReportHistory.timestamp)
    ).all()
    line, found = sessions(rows)
    if line is None:
        return None
    total = sum(((e - s) for s, e in found), timedelta())
    minutes = round(total.total_seconds() / 60)
    return {
        "label": f"{minutes} min" if minutes < 60 else f"{minutes / 60:.1f} h",
        "sessions": len(found),
        "line_w": round(line),
        "period": "this week" if days == WINDOW_DAYS else f"last {days} days",
    }
