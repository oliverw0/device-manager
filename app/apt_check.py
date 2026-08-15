"""Decide whether a device's apt packages are stale enough to flag.

Kept pure and separate so it's the single source of truth for both the alert
(api.py) and the UI badge, and so it has one runnable self-check.
"""
from typing import Optional


def evaluate(apt: Optional[dict], stale_days: int, upgradable_threshold: int) -> tuple[bool, str]:
    """Returns (needs_update, short_label). label is chip text like "45d stale"
    or "62 updates"; empty when there's nothing to flag."""
    if not apt or not apt.get("available"):
        return False, ""

    age = apt.get("last_update_age_seconds")
    upgradable = apt.get("upgradable")

    age_days = int(age // 86400) if age is not None else None
    stale = age_days is not None and age_days >= stale_days
    many = upgradable is not None and upgradable >= upgradable_threshold

    if not (stale or many):
        return False, ""

    # Prefer the count when it's the offender (more actionable), else the age.
    if many:
        return True, f"{upgradable} updates"
    return True, f"{age_days}d stale"


def demo() -> None:
    assert evaluate(None, 30, 50) == (False, "")
    assert evaluate({"available": False}, 30, 50) == (False, "")
    assert evaluate({"available": True, "last_update_age_seconds": 10 * 86400, "upgradable": 3}, 30, 50) == (False, "")
    assert evaluate({"available": True, "last_update_age_seconds": 45 * 86400, "upgradable": None}, 30, 50) == (True, "45d stale")
    assert evaluate({"available": True, "last_update_age_seconds": 1 * 86400, "upgradable": 62}, 30, 50) == (True, "62 updates")
    # count wins the label even when both trip
    assert evaluate({"available": True, "last_update_age_seconds": 40 * 86400, "upgradable": 80}, 30, 50) == (True, "80 updates")
    print("apt_check demo ok")


if __name__ == "__main__":
    demo()
