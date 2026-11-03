from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse
from urllib.parse import quote

from sqlmodel import Session

from .database import engine
from .models import User

PUBLIC_PATHS = {"/login", "/logout", "/favicon.ico", "/sw.js", "/login/oidc", "/login/oidc/callback"}
PUBLIC_PREFIXES = ("/static", "/api/v1", "/ping/")  # /ping/<token>: heartbeat checks


class AdminAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)

        # a user disabled or deleted on the Users page is signed out on their next request
        uid = request.session.get("user_id")
        if uid is not None:
            with Session(engine) as db:
                user = db.get(User, uid)
            if user is None or user.disabled:
                request.session.clear()

        if not request.session.get("is_admin"):
            if path.endswith(".json"):
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            return RedirectResponse(url=f"/login?next={quote(path)}")

        return await call_next(request)
