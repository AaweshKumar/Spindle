# Component: Spindle Architecture
# File: jwt.py
# Description: JWT encode/decode for the Spindle orchestration platform.

"""JWT encode and decode helpers.

The algorithm is HS256 and is hard-coded here, not configurable via Settings.
A configurable algorithm would allow ``alg: none`` or RS256/HS256 confusion
attacks; keeping it as a module constant closes that class of misconfiguration.

IMPORTANT: import this module only via absolute import paths (e.g.
``from spindle_core.auth.jwt import ...``).  Never run this file directly
with ``python jwt.py`` because the bare name ``jwt`` would shadow PyJWT.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import jwt as _pyjwt

from spindle_core.auth.models import TokenPayload

# The signing algorithm is a constant, not a setting, so it cannot be
# downgraded to ``alg: none`` by misconfiguration or a crafted token header.
_ALGORITHM = "HS256"


class InvalidToken(Exception):
    """Raised for any JWT decode failure.

    Wraps every PyJWT exception in one type so callers have a single
    ``except InvalidToken`` rather than a fragile list of PyJWT error classes.
    """


def create_access_token(
    subject: str,
    secret: str,
    expire_minutes: int,
    now: datetime | None = None,
) -> tuple[str, int]:
    """Encode a signed HS256 JWT and return ``(token, expires_in_seconds)``.

    Args:
        subject: Value for the ``sub`` claim (typically the username).
        secret: HMAC signing key obtained from Settings.JWT_SECRET.
        expire_minutes: Token lifetime; added to *now* for the ``exp`` claim.
        now: Override for the current time.  Defaults to UTC now.  Passing an
            explicit value lets callers create tokens that are already expired
            (useful for smoke tests).

    Returns:
        A 2-tuple of (encoded_jwt_string, expires_in_seconds).
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)

    iat = int(now.timestamp())
    expires_in = expire_minutes * 60
    exp = iat + expires_in

    payload: dict[str, Any] = {"sub": subject, "iat": iat, "exp": exp}
    token = _pyjwt.encode(payload, secret, algorithm=_ALGORITHM)
    return token, expires_in


def decode_access_token(token: str, secret: str) -> TokenPayload:
    """Decode and verify a JWT, returning a ``TokenPayload``.

    Args:
        token: The raw JWT string from the ``Authorization: Bearer`` header.
        secret: The HMAC key used to verify the signature.

    Returns:
        A ``TokenPayload`` with all required claims present.

    Raises:
        InvalidToken: For any decode error — expired, bad signature, missing
            claims, wrong algorithm, ``alg: none``, etc.  A single exception
            type is intentional; callers must not distinguish error subtypes,
            because different error messages would aid token-forgery attacks.

    Security notes:
        - ``algorithms=["HS256"]`` is passed explicitly so PyJWT rejects any
          token whose header declares a different algorithm, including
          ``alg: none``.
        - ``options={"require": ["exp", "iat", "sub"]}`` ensures PyJWT raises
          on a token that omits any required claim rather than returning None.
    """
    try:
        payload = _pyjwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"require": ["exp", "iat", "sub"]},
        )
    except Exception as exc:
        raise InvalidToken("token validation failed") from exc

    try:
        return TokenPayload(
            sub=str(payload["sub"]),
            iat=int(payload["iat"]),
            exp=int(payload["exp"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidToken("malformed token payload") from exc
