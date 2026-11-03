"""Self-check for OIDC sign-in against a fake provider. Run from host/:  python test_oidc.py

Covers: redirect to the provider with state + PKCE, the callback creating the
user on first sign-in, a forged state being refused, the allowed-group rule,
a disabled user being signed out on their next request, and the Users page.
"""
import base64
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

DB = Path("data/test_oidc.db")
DB.unlink(missing_ok=True)
os.environ.update(
    DATABASE_URL=f"sqlite:///./{DB.as_posix()}", ADMIN_USERNAME="admin", ADMIN_PASSWORD="hunter2",
    SESSION_HTTPS_ONLY="false", OIDC_ISSUER="https://auth.test/application/o/dm/",
    OIDC_CLIENT_ID="dm-client", OIDC_CLIENT_SECRET="s3cret",
)

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import oidc  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402

ISSUER = "https://auth.test/application/o/dm/"
FAKE = {"nonce": None, "groups": ["homelab"]}


def b64(d):
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


async def fake_get(url, **kw):
    if url.endswith("/.well-known/openid-configuration"):
        return {"issuer": ISSUER, "authorization_endpoint": "https://auth.test/authorize",
                "token_endpoint": "https://auth.test/token", "userinfo_endpoint": "https://auth.test/userinfo"}
    assert kw["headers"]["Authorization"] == "Bearer at-1"
    return {"sub": "u-123", "email": "oliver@example.com", "preferred_username": "oliver", "groups": FAKE["groups"]}


async def fake_post(url, data, auth):
    assert auth == ("dm-client", "s3cret") and data["code"] == "code-1" and data["code_verifier"]
    idt = b64({"iss": ISSUER, "aud": "dm-client", "sub": "u-123", "nonce": FAKE["nonce"], "exp": 4102444800, "name": "Oliver"})
    return {"id_token": f"e30.{idt}.sig", "access_token": "at-1"}


oidc._get_json, oidc._post_json = fake_get, fake_post


def sign_in(c, state=None):
    r = c.get("/login/oidc?next=/problems", follow_redirects=False)
    assert r.status_code == 302, r.status_code
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"] == ["http://testserver/login/oidc/callback"]
    FAKE["nonce"] = q["nonce"][0]
    return c.get(f"/login/oidc/callback?code=code-1&state={state or q['state'][0]}", follow_redirects=False)


with TestClient(app, base_url="http://testserver") as c:
    assert "Sign in with authentik" in c.get("/login").text

    r = sign_in(c, state="forged")
    assert r.status_code == 401 and "Try again" in r.text, r.status_code

    r = sign_in(c)
    assert r.status_code == 303 and r.headers["location"] == "/problems", (r.status_code, r.text[:300])
    with Session(engine) as db:
        u = db.exec(select(User).where(User.sub == "u-123")).one()
        assert (u.username, u.email, u.name, u.source) == ("oliver", "oliver@example.com", "Oliver", "oidc")
    page = c.get("/users").text
    assert "oliver@example.com" in page and ">you<" in page

    with TestClient(app, base_url="http://testserver") as admin:  # password login shows up too
        admin.post("/login", data={"username": "admin", "password": "hunter2", "next": "/"})
        admin.post(f"/users/{u.id}/toggle")                           # disable oliver
    r = c.get("/problems", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"].startswith("/login"), r.status_code
    r = sign_in(c)
    assert r.status_code == 401 and "disabled" in r.text

    settings.oidc_allowed_group = "admins"
    with Session(engine) as db:
        db.delete(db.get(User, u.id))
        db.commit()
    r = sign_in(c)
    assert r.status_code == 401 and "admins" in r.text and "group" in r.text
    settings.oidc_allowed_group = ""

    with Session(engine) as db:
        assert db.exec(select(User).where(User.source == "local")).one().username == "admin"

    # setup panel: provider reachable, the redirect URI to register
    page = c.get("/users").text  # c is signed out (oliver was removed): use a password session
    with TestClient(app, base_url="http://testserver") as admin:
        admin.post("/login", data={"username": "admin", "password": "hunter2", "next": "/"})
        page = admin.get("/users").text
        assert "http://testserver/login/oidc/callback" in page and ">ready<" in page, page[-1500:]
    # local account: switched off blocks the password, can't be removed, and the recovery command restores it
    from app.routers.users import set_enabled
    with Session(engine) as db:
        local = db.exec(select(User).where(User.source == "local")).one()
    with TestClient(app, base_url="http://testserver") as other:
        other.post("/login", data={"username": "admin", "password": "hunter2", "next": "/"})
        set_enabled("admin", False)
        assert other.get("/", follow_redirects=False).status_code == 307   # signed out at once
        r = other.post("/login", data={"username": "admin", "password": "hunter2", "next": "/"}, follow_redirects=False)
        assert r.status_code == 403, r.status_code
        assert set_enabled("admin", True)
        r = other.post("/login", data={"username": "admin", "password": "hunter2", "next": "/"}, follow_redirects=False)
        assert r.status_code == 303
        other.post(f"/users/{local.id}/delete")  # own row: refused anyway; a local account is never removed
    with Session(engine) as db:
        assert db.get(User, local.id) is not None

engine.dispose()  # Windows won't delete an open database
DB.unlink(missing_ok=True)
print("oidc self-checks passed")
