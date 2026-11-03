"""Users page: everyone who has signed in, and how sign-in is set up.

Locked out (local account disabled and the provider down)? From the host:
    docker compose exec host python -m app.routers.users enable admin
"""
import sys

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from .. import oidc
from ..config import settings
from ..database import engine, get_session
from ..models import User
from .dashboard import templates

router = APIRouter()


async def _page(request: Request, form_error: str = "", saved: bool = False):
    with Session(engine) as session:
        users = session.exec(select(User).order_by(User.created_at)).all()
    provider, provider_error = None, None
    if oidc.enabled():
        try:
            provider = (await oidc.discovery()).get("issuer")
        except oidc.OidcError as exc:
            provider_error = str(exc)
    return templates.TemplateResponse("users.html", {
        "request": request, "users": users, "me": request.session.get("user_id"),
        "oidc_on": oidc.enabled(), "settings": settings,
        "redirect_uri": oidc.redirect_uri(str(request.base_url)),
        "provider": provider, "provider_error": provider_error,
        "form_error": form_error, "saved": saved,
    }, status_code=400 if form_error else 200)


@router.get("/users")
async def users_page(request: Request):
    return await _page(request)


@router.post("/users/sign-in")
async def save_sign_in(request: Request, issuer: str = Form(""), client_id: str = Form(""),
                       client_secret: str = Form(""), allowed_group: str = Form(""),
                       public_url: str = Form(""), action: str = Form("save")):
    """Set up or turn off authentik from the page. The issuer is checked before saving."""
    if action == "off":
        with Session(engine) as db:
            oidc.save(db, {"oidc_issuer": "", "oidc_client_id": "", "oidc_client_secret": ""})
        return RedirectResponse(url="/users#sign-in", status_code=303)
    issuer, client_id, public_url = issuer.strip(), client_id.strip(), public_url.strip().rstrip("/")
    secret = client_secret.strip() or settings.oidc_client_secret  # blank keeps the saved secret
    if not issuer.startswith(("https://", "http://")) or not client_id or not secret:
        return await _page(request, "Issuer URL, client ID and client secret are all needed.")
    if public_url and not public_url.startswith(("https://", "http://")):
        return await _page(request, "Dashboard URL must start with https:// or http://.")
    try:
        await oidc.discovery(issuer)
    except oidc.OidcError as exc:
        return await _page(request, f"Not saved. {exc}")
    with Session(engine) as db:
        oidc.save(db, {"oidc_issuer": issuer, "oidc_client_id": client_id, "oidc_client_secret": secret,
                       "oidc_allowed_group": allowed_group.strip(), "public_url": public_url})
    return await _page(request, saved=True)


@router.post("/users/{user_id}/toggle")
def toggle_user(user_id: int, request: Request, session: Session = Depends(get_session)):
    user = session.get(User, user_id)
    if user and user.id != request.session.get("user_id"):  # never lock yourself out
        user.disabled = not user.disabled
        session.add(user)
        session.commit()
    return RedirectResponse(url="/users", status_code=303)


@router.post("/users/{user_id}/delete")
def delete_user(user_id: int, request: Request, session: Session = Depends(get_session)):
    user = session.get(User, user_id)
    # the local account would be recreated by its next password sign-in: disable it instead
    if user and user.id != request.session.get("user_id") and user.source != "local":
        session.delete(user)
        session.commit()
    return RedirectResponse(url="/users", status_code=303)


def set_enabled(username: str, enabled: bool) -> bool:
    with Session(engine) as session:
        user = session.exec(select(User).where(User.username == username)).first()
        if not user:
            return False
        user.disabled = not enabled
        session.add(user)
        session.commit()
    return True


if __name__ == "__main__":  # lockout recovery: python -m app.routers.users enable|disable <username>
    if len(sys.argv) != 3 or sys.argv[1] not in ("enable", "disable"):
        sys.exit("usage: python -m app.routers.users enable|disable <username>")
    if not set_enabled(sys.argv[2], sys.argv[1] == "enable"):
        sys.exit(f"no user named {sys.argv[2]!r}")
    print(f"{sys.argv[2]} {sys.argv[1]}d")
