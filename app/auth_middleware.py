from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse
from urllib.parse import quote

PUBLIC_PATHS = {"/login", "/logout", "/favicon.ico"}
PUBLIC_PREFIXES = ("/static", "/api/v1")


class AdminAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)

        if not request.session.get("is_admin"):
            if path.endswith(".json"):
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            return RedirectResponse(url=f"/login?next={quote(path)}")

        return await call_next(request)
