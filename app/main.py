import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session
from starlette.middleware.sessions import SessionMiddleware

from . import monitor, oidc, ssh_keys
from .auth_middleware import AdminAuthMiddleware
from .config import settings
from .database import engine, init_db
from .routers import api, auth, checks, dashboard, hooks, mcp, metrics, power, problems, storage, terminal, updates, users, views

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with Session(engine) as db:
        oidc.local_user(db, settings.admin_username, signing_in=False)
        oidc.load_saved(db)
    ssh_keys.ensure_host_keypair()
    task = asyncio.create_task(monitor.run_forever())
    yield
    task.cancel()


class PageSessionMiddleware(SessionMiddleware):
    """No session (so no Set-Cookie) on static files. A caching proxy (NPM's
    "Cache Assets") stores the Set-Cookie along with the file and replays it
    to everyone for 30 minutes, overwriting their session with a stale one."""
    NO_SESSION = ("/static/", "/sw.js", "/favicon.ico")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(self.NO_SESSION):
            return await self.app(scope, receive, send)
        await super().__call__(scope, receive, send)


app = FastAPI(title="Pulse", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Order matters: Starlette wraps the most-recently-added middleware outermost,
# so SessionMiddleware must be added last to run before AdminAuthMiddleware
# reads request.session on every request.
app.add_middleware(AdminAuthMiddleware)
app.add_middleware(
    PageSessionMiddleware,
    secret_key=settings.resolved_session_secret(),
    session_cookie="dm_session",
    max_age=60 * 60 * 24 * 30,  # 30 days — persistent so phones aren't re-prompted twice a day
    same_site="lax",
    https_only=settings.session_https_only,
)

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return RedirectResponse(url="/static/favicon.svg")


# Served from root so the service worker's scope covers the whole app (a
# /static/ URL would only control /static/). AdminAuthMiddleware must let it
# through unauthenticated — the SW is fetched before login.
@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse("app/static/sw.js", media_type="application/javascript")


app.include_router(api.router)
app.include_router(auth.router)
app.include_router(checks.router)
app.include_router(dashboard.router)
app.include_router(terminal.router)
app.include_router(updates.router)
app.include_router(storage.router)
app.include_router(power.router)
app.include_router(problems.router)
app.include_router(users.router)
app.include_router(views.router)
app.include_router(hooks.router)
app.include_router(metrics.router)
app.include_router(mcp.router)
