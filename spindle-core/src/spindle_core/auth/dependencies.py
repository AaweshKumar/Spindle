# Component: Spindle Architecture
# File: dependencies.py
# Description: FastAPI dependencies for auth in the Spindle orchestration platform.

"""FastAPI dependency providers for authentication.

``get_current_user`` is the dependency contract used by all protected routes
(P2+).  Its exact name and location (spindle_core.auth.dependencies) are
fixed; P2 will import it as-is.

``Depends(get_settings)`` is used instead of reading a module-level settings
singleton so that tests can override ``get_settings`` via
``app.dependency_overrides`` without touching environment variables.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from spindle_core.auth.jwt import InvalidToken, decode_access_token
from spindle_core.auth.models import User
from spindle_core.auth.service import load_user
from spindle_core.config import Settings, get_settings

# tokenUrl must match the router's prefix + path so that the OpenAPI UI
# "Authorize" button points at the right endpoint.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

_401 = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    token: str = Depends(oauth2_scheme),
    settings: Settings = Depends(get_settings),
) -> User:
    """Validate the bearer token and return the authenticated User.

    Raises HTTP 401 for every failure (expired, bad signature, unknown subject,
    missing claims) with a generic detail string so no information about the
    failure mode leaks to the caller.

    ``Depends(get_settings)`` rather than a direct ``get_settings()`` call
    keeps this dependency overridable in tests without monkey-patching globals.
    """
    try:
        payload = decode_access_token(token, settings.JWT_SECRET.get_secret_value())
    except InvalidToken:
        raise _401

    user = load_user(payload.sub, settings)
    if user is None:
        raise _401
    return user
