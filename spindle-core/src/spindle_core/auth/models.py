# Component: Spindle Architecture
# File: models.py
# Description: Auth domain models for the Spindle orchestration platform.

"""Auth domain models.

Frozen dataclasses are used for internal structures (User, TokenPayload) so
they are safe to pass between functions without defensive copying.  Pydantic
is used only at the API boundary (TokenResponse) where JSON serialisation
is needed.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel


@dataclass(frozen=True)
class User:
    """Authenticated principal.

    Frozen so callers cannot mutate it after construction — important since
    the same instance may be dependency-injected into multiple route handlers
    in the same request.
    """

    username: str


@dataclass(frozen=True)
class TokenPayload:
    """Decoded JWT claims.

    All three claims (sub, iat, exp) are required; decode_access_token raises
    InvalidToken if any is absent so callers never receive a partial payload.
    """

    sub: str
    iat: int
    exp: int


class TokenResponse(BaseModel):
    """JSON body returned by POST /auth/token.

    ``expires_in`` is seconds, matching RFC 6749 §5.1 so clients can derive
    the absolute expiry without knowing the server clock.
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int
