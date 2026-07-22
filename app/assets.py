"""A cache-busting version token for static assets.

Appended as ?v=<token> to app.js / style.css links so a redeploy always forces
browsers to fetch the new file instead of a stale cached copy (which otherwise
causes "X is not defined" errors when new HTML calls into newer JS).
"""
import hashlib
from pathlib import Path


def _compute() -> str:
    digest = hashlib.md5()
    base = Path("app/static")
    for name in ("app.js", "style.css"):
        try:
            digest.update((base / name).read_bytes())
        except OSError:
            pass
    return digest.hexdigest()[:8]


STATIC_VERSION = _compute()
