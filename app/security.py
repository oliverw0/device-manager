import secrets

from fastapi import Depends, HTTPException, Header, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlmodel import Session, select

from .config import settings
from .database import get_session
from .models import Device

basic_auth = HTTPBasic()


def require_admin(credentials: HTTPBasicCredentials = Depends(basic_auth)) -> None:
    valid_user = secrets.compare_digest(credentials.username, settings.admin_username)
    valid_pass = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (valid_user and valid_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def get_device_from_api_key(
    x_api_key: str = Header(...),
    session: Session = Depends(get_session),
) -> Device:
    device = session.exec(select(Device).where(Device.api_key == x_api_key)).first()
    if device is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown API key")
    return device
