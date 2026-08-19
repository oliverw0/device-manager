"""Self-checks for the alerting logic. Run from host/:  python test_alerts.py"""
from app.monitor import _project_disk_full_days, _sustained_over

# --- sustained threshold ---
assert _sustained_over([92, 95, 91], 90) is True          # all over -> trips
assert _sustained_over([92, 40, 95], 90) is False         # one dip -> no
assert _sustained_over([95], 90) is False                 # single reading -> no
assert _sustained_over([], 90) is False                   # no data -> no

# --- disk-full projection ---
day = 86400
# climbing 1%/day; last point is 97% -> (100-97)/1 = ~3 days to full
pts = [(i * day, 90 + i) for i in range(8)]
d = _project_disk_full_days(pts)
assert d is not None and abs(d - 3) < 0.5, d

assert _project_disk_full_days([(i * day, 50) for i in range(8)]) is None   # flat -> None
assert _project_disk_full_days([(i * day, 90 - i) for i in range(8)]) is None  # falling -> None
assert _project_disk_full_days([(0, 90), (day, 91)]) is None               # too few points -> None
# very slow fill projects >180d -> None
assert _project_disk_full_days([(i * day, 50 + i * 0.01) for i in range(10)]) is None

print("ok")
