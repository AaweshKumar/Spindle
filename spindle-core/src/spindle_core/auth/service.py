# Component: Spindle Architecture
# File: service.py
# Description: Auth service for the Spindle orchestration platform.

"""Auth service — the single swap point for user-lookup and credential verification.

All user lookup and password checking lives here so that moving to a DB-backed
user store later only requires editing this one module.  auth/router.py and
auth/dependencies.py call into this module; they do not contain credential
logic.
"""

from __future__ import annotations

import hmac

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError, InvalidHashError

from spindle_core.auth.models import User
from spindle_core.config import Settings

# One shared PasswordHasher instance so its defaults (time cost, memory cost,
# parallelism) are applied consistently.  It carries no mutable state after
# construction, so sharing is safe.
_ph = PasswordHasher()


def authenticate(username: str, password: str, settings: Settings) -> User | None:
    """Return the admin User if *username* and *password* match, else None.

    The argon2 verification is **always** run, even when the username does not
    match, so that both the wrong-user and wrong-password code paths take the
    same amount of time.  This eliminates username enumeration by timing
    (a constant-work design).

    hmac.compare_digest is used for the username comparison to prevent
    timing-based enumeration of the admin username.
    """
    stored_hash = settings.ADMIN_PASSWORD_HASH.get_secret_value()
    admin_username = settings.ADMIN_USERNAME

    # Always run the hash verification — even on a wrong username — to prevent
    # timing attacks that would reveal whether a username exists.
    password_ok = False
    try:
        password_ok = _ph.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        password_ok = False

    # Use hmac.compare_digest to avoid timing leaks on the username check.
    # Operands are encoded to UTF-8 bytes because hmac.compare_digest does not
    # support strings with non-ASCII characters.
    username_ok = hmac.compare_digest(username.encode("utf-8"), admin_username.encode("utf-8"))

    if password_ok and username_ok:
        return User(username=admin_username)
    return None


def load_user(username: str, settings: Settings) -> User | None:
    """Return the admin User if *username* matches, else None.

    This is the swap point: when DB-backed users land, this function is
    replaced with a DB lookup.  Nothing outside this module needs to change.
    """
    if hmac.compare_digest(username.encode("utf-8"), settings.ADMIN_USERNAME.encode("utf-8")):
        return User(username=settings.ADMIN_USERNAME)
    return None
