from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ..assets import STATIC_VERSION
from ..security import credentials_valid

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["static_version"] = STATIC_VERSION


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
    return templates.TemplateResponse("login.html", {"request": request, "next": next, "error": None})


@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    next = _safe_next(next)
    if credentials_valid(username, password):
        request.session["is_admin"] = True
        return RedirectResponse(url=next, status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "next": next, "error": "Invalid username or password"},
        status_code=401,
    )


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
