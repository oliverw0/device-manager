"""Self-check for the login round-trip. Run from host/:  python test_login.py

Guards the loop bug: a Secure session cookie is silently dropped by the browser
over plain http://, so login "succeeds" and every next request bounces back to
/login. SESSION_HTTPS_ONLY must stay false unless the app is reached over HTTPS.
"""
import os
os.environ.update(ADMIN_USERNAME="admin", ADMIN_PASSWORD="hunter2", SESSION_HTTPS_ONLY="false")

from fastapi.testclient import TestClient
from app.main import app

with TestClient(app, base_url="http://testserver") as c:
    r = c.post("/login", data={"username": "admin", "password": "hunter2", "next": "/"}, follow_redirects=False)
    assert r.status_code == 303, r.status_code
    assert "dm_session" in c.cookies, "session cookie not stored -> login loops"

    r = c.get("/", follow_redirects=False)                # the request that used to bounce
    assert r.status_code == 200, f"still bounced: {r.status_code} -> {r.headers.get('location')}"

    r = c.post("/login", data={"username": "admin", "password": "wrong", "next": "/"}, follow_redirects=False)
    assert r.status_code == 401, r.status_code

print("login self-checks passed")
