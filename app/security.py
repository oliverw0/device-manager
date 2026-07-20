import secrets

from fastapi import Depends, HTTPException, Header, status
from sqlmodel import Session, select

from .config import settings
from .database import get_session
from .models import Device


def credentials_valid(username: str, password: str) -> bool:
    # secrets.compare_digest works byte-for-byte on whatever the browser sends,
    # so any character the user can type into the login form is handled fine.
    valid_user = secrets.compare_digest(username, settings.admin_username)
    valid_pass = secrets.compare_digest(password, settings.resolved_admin_password())
    return valid_user and valid_pass


def get_device_from_api_key(
    x_api_key: str = Header(...),
    session: Session = Depends(get_session),
) -> Device:
    device = session.exec(select(Device).where(Device.api_key == x_api_key)).first()
    if device is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown API key")
    return device
