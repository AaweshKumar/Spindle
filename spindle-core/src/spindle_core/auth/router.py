# Component: Spindle Architecture
# File: router.py
# Description: Auth router for the Spindle orchestration platform.

"""Auth router — POST /auth/token (OAuth2 password grant).

Returns an access token on success.  Both wrong-user and wrong-password
return 401 with the same body so an attacker cannot enumerate valid usernames
by comparing responses.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from spindle_core.auth.models import TokenResponse
from spindle_core.auth.service import authenticate
from spindle_core.auth.jwt import create_access_token
from spindle_core.config import Settings, get_settings

router = APIRouter(prefix="/auth", tags=["auth"])

_401 = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Incorrect username or password",
    headers={"WWW-Authenticate": "Bearer"},
)


@router.post("/token", response_model=TokenResponse)
def login(
    form: OAuth2PasswordRequestForm = Depends(),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    """Issue a JWT access token for valid admin credentials.

    Uses ``OAuth2PasswordRequestForm`` (form body, not JSON) as required by
    the OAuth2 password-grant spec.  The same 401 is returned for both wrong
    username and wrong password so the response leaks nothing about which
    field was incorrect.

    Passwords and tokens are never logged here.
    """
    user = authenticate(form.username, form.password, settings)
    if user is None:
        raise _401

    token, expires_in = create_access_token(
        subject=user.username,
        secret=settings.JWT_SECRET.get_secret_value(),
        expire_minutes=settings.JWT_EXPIRE_MINUTES,
    )
    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
    )
