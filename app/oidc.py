"""Sign in with an OpenID Connect provider (built for authentik).

Authorization code flow with PKCE. The ID token comes straight from the
provider's token endpoint over TLS, so its claims are trusted without checking
the signature (OIDC Core 3.1.3.7); iss, aud, exp and the nonce are still checked.
First sign-in creates the user (OIDC_AUTO_CREATE), optionally only for members
of OIDC_ALLOWED_GROUP.
"""
import base64
import hashlib
import json
import secrets
import time
from datetime import datetime
from urllib.parse import urlencode

import httpx
from sqlmodel import Session, select

from .config import settings
from .models import AppSetting, User

_discovery = {}  # issuer -> (fetched at, document)
# Set on the Users page (saved as AppSetting rows, which beat .env).
UI_KEYS = ("oidc_issuer", "oidc_client_id", "oidc_client_secret", "oidc_allowed_group", "public_url")


class OidcError(Exception):
    """Sign-in refused; the message is shown on the login page."""


def enabled() -> bool:
    return bool(settings.oidc_issuer and settings.oidc_client_id)


async def _get_json(url: str, **kw) -> dict:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(url, **kw)
        r.raise_for_status()
        return r.json()


async def _post_json(url: str, data: dict, auth: tuple) -> dict:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(url, data=data, auth=auth)
        if r.status_code >= 400:
            raise OidcError(f"The provider refused the sign-in code ({r.status_code}: {r.text[:200]})")
        return r.json()


async def discovery(issuer: str | None = None) -> dict:
    issuer = (issuer or settings.oidc_issuer).rstrip("/")
    at, doc = _discovery.get(issuer, (0.0, None))
    if not doc or time.time() - at > 3600:
        url = issuer + "/.well-known/openid-configuration"
        try:
            doc = await _get_json(url)
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcError(f"Couldn't reach {url}: {exc}") from exc
        if not doc.get("authorization_endpoint") or not doc.get("token_endpoint"):
            raise OidcError(f"{url} isn't an OpenID Connect provider.")
        _discovery[issuer] = (time.time(), doc)
    return doc


def load_saved(db: Session) -> None:
    """Apply the sign-in settings saved on the Users page over .env."""
    for row in db.exec(select(AppSetting).where(AppSetting.key.in_(UI_KEYS))).all():
        setattr(settings, row.key, row.value)


def save(db: Session, values: dict) -> None:
    for key, value in values.items():
        assert key in UI_KEYS
        db.merge(AppSetting(key=key, value=value))
        setattr(settings, key, value)
    db.commit()


def redirect_uri(base_url: str) -> str:
    return (settings.public_url or base_url).rstrip("/") + "/login/oidc/callback"


async def authorize_url(session: dict, base_url: str, next_path: str) -> str:
    doc = await discovery()
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    session["oidc"] = {"state": state, "nonce": nonce, "verifier": verifier, "next": next_path}
    return doc["authorization_endpoint"] + "?" + urlencode({
        "response_type": "code", "client_id": settings.oidc_client_id, "redirect_uri": redirect_uri(base_url),
        "scope": settings.oidc_scopes, "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })


def id_token_claims(token: str, issuer: str, nonce: str, now: float | None = None) -> dict:
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as exc:
        raise OidcError("The provider sent an unreadable ID token.") from exc
    aud = claims.get("aud")
    if claims.get("iss", "").rstrip("/") != issuer.rstrip("/"):
        raise OidcError(f"ID token is from {claims.get('iss')!r}, expected {issuer!r} (check OIDC_ISSUER).")
    if settings.oidc_client_id not in (aud if isinstance(aud, list) else [aud]):
        raise OidcError("ID token was issued for a different client (check OIDC_CLIENT_ID).")
    if claims.get("nonce") != nonce:
        raise OidcError("Sign-in didn't match this browser's request. Try again.")
    if claims.get("exp", 0) < (now or time.time()) - 60:
        raise OidcError("ID token has expired. Check the clocks on the host and the provider.")
    return claims


async def finish(session: dict, code: str, state: str, base_url: str) -> dict:
    """Code -> verified claims (ID token merged with userinfo)."""
    pending = session.pop("oidc", None)
    if not pending or not state or not secrets.compare_digest(state, pending["state"]):
        raise OidcError("Sign-in expired or was started in another browser. Try again.")
    doc = await discovery()
    tokens = await _post_json(doc["token_endpoint"], {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(base_url),
        "code_verifier": pending["verifier"],
    }, auth=(settings.oidc_client_id, settings.oidc_client_secret))
    claims = id_token_claims(tokens.get("id_token", ""), doc.get("issuer") or settings.oidc_issuer, pending["nonce"])
    if doc.get("userinfo_endpoint") and tokens.get("access_token"):
        try:
            info = await _get_json(doc["userinfo_endpoint"], headers={"Authorization": f"Bearer {tokens['access_token']}"})
            if info.get("sub") == claims["sub"]:
                claims = {**claims, **info}
        except (httpx.HTTPError, ValueError):
            pass  # the ID token alone is enough
    claims["_next"] = pending.get("next") or "/"
    return claims


def user_for(db: Session, claims: dict, now: datetime | None = None) -> User:
    """Find or create the user for verified claims; refuses disabled users,
    unknown users when auto-create is off, and non-members of the allowed group."""
    group = settings.oidc_allowed_group
    if group and group not in (claims.get("groups") or []):
        raise OidcError(f"Your account isn't in the {group!r} group, which is needed to use DeviceManager.")
    user = db.exec(select(User).where(User.sub == claims["sub"])).first()
    if user is None:
        if not settings.oidc_auto_create:
            raise OidcError("Your account hasn't been added to DeviceManager. Ask an admin to turn on OIDC_AUTO_CREATE.")
        base = claims.get("preferred_username") or (claims.get("email") or "").split("@")[0] or "user"
        name, n = base, 2
        while db.exec(select(User).where(User.username == name)).first():
            name, n = f"{base}{n}", n + 1
        user = User(username=name, sub=claims["sub"], source="oidc")
    if user.disabled:
        raise OidcError("This account is disabled in DeviceManager.")
    user.email = claims.get("email") or user.email
    user.name = claims.get("name") or user.name
    user.last_login_at = now or datetime.utcnow()
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def local_user(db: Session, username: str, signing_in: bool = True) -> User:
    """The ADMIN_USERNAME password account as a user, so it's on the Users page
    from the start (created at startup) and can be switched off there."""
    user = db.exec(select(User).where(User.username == username, User.source == "local")).first()
    if user and not signing_in:
        return user
    user = user or User(username=username, source="local")
    if signing_in:
        user.last_login_at = datetime.utcnow()
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
