import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import monitor, ssh_keys
from .auth_middleware import AdminAuthMiddleware
from .config import settings
from .database import init_db
from .routers import api, auth, checks, dashboard, terminal

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ssh_keys.ensure_host_keypair()
    task = asyncio.create_task(monitor.run_forever())
    yield
    task.cancel()


app = FastAPI(title="DeviceManager", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Order matters: Starlette wraps the most-recently-added middleware outermost,
# so SessionMiddleware must be added last to run before AdminAuthMiddleware
# reads request.session on every request.
app.add_middleware(AdminAuthMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.resolved_session_secret(),
    session_cookie="dm_session",
    max_age=60 * 60 * 24 * 30,  # 30 days — persistent so phones aren't re-prompted twice a day
    same_site="lax",
    https_only=True,  # cookie only sent over HTTPS (app runs behind the TLS proxy)
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
