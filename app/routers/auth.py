from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from .. import oidc
from ..assets import STATIC_VERSION
from ..config import settings
from ..database import engine
from ..security import credentials_valid

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["static_version"] = STATIC_VERSION
templates.env.globals["oidc_enabled"] = oidc.enabled
templates.env.globals["settings"] = settings


def _safe_next(path: str) -> str:
    # Only allow same-site relative paths, otherwise this becomes an open redirect.
    if not path or not path.startswith("/") or path.startswith("//"):
        return "/"
    return path


@router.get("/login")
def login_form(request: Request, next: str = "/"):
    next = _safe_next(next)
    if request.session.get("is_admin"):
        return RedirectResponse(url=next)
    return _login_page(request, next, None)


def _login_page(request: Request, next: str, error: str | None, status_code: int = 200):
    return templates.TemplateResponse("login.html", {"request": request, "next": next, "error": error}, status_code=status_code)


def _sign_in(request: Request, user) -> None:
    request.session.clear()
    request.session.update(is_admin=True, user_id=user.id, username=user.username)


@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    next = _safe_next(next)
    if settings.password_login and credentials_valid(username, password):
        with Session(engine) as db:
            user = oidc.local_user(db, username)
        if user.disabled:
            return _login_page(request, next, "This account is disabled.", 403)
        _sign_in(request, user)
        return RedirectResponse(url=next, status_code=303)
    return _login_page(request, next, "Invalid username or password", 401)


@router.get("/login/oidc")
async def login_oidc(request: Request, next: str = "/"):
    if not oidc.enabled():
        return RedirectResponse(url="/login")
    try:
        url = await oidc.authorize_url(request.session, str(request.base_url), _safe_next(next))
    except oidc.OidcError as exc:
        return _login_page(request, _safe_next(next), str(exc), 502)
    return RedirectResponse(url=url, status_code=302)


@router.get("/login/oidc/callback")
async def login_oidc_callback(request: Request, code: str = "", state: str = "", error: str = "", error_description: str = ""):
    if error:
        return _login_page(request, "/", f"{settings.oidc_name} said: {error_description or error}", 401)
    try:
        claims = await oidc.finish(request.session, code, state, str(request.base_url))
        with Session(engine) as db:
            user = oidc.user_for(db, claims)
    except oidc.OidcError as exc:
        return _login_page(request, "/", str(exc), 401)
    _sign_in(request, user)
    return RedirectResponse(url=_safe_next(claims["_next"]), status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
