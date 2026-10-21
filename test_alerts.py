"""Self-checks for the alerting logic. Run from host/:  python test_alerts.py"""
from app.monitor import _sustained_over

# --- sustained threshold ---
assert _sustained_over([92, 95, 91], 90) is True          # all over -> trips
assert _sustained_over([92, 40, 95], 90) is False         # one dip -> no
assert _sustained_over([95], 90) is False                 # single reading -> no
assert _sustained_over([], 90) is False                   # no data -> no

print("ok")
