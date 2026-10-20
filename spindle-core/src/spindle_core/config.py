# Component: Spindle Architecture
# File: config.py
# Description: Application settings for the Spindle orchestration platform.

"""Application-wide settings loaded from environment variables.

Run command (factory mode, no module-level app):
    uvicorn spindle_core.main:create_app --factory

CORS_ALLOW_ORIGINS format (set as an environment variable):
    JSON list of origin strings, e.g.:
        CORS_ALLOW_ORIGINS='["https://app.example.com", "https://admin.example.com"]'
    Omit the variable (or set it to "[]") to deny all cross-origin requests.

Credential fields use SecretStr so they are redacted in repr() and str()
output.  To read the actual value in application code, call:
    settings.DATABASE_URL.get_secret_value()
    settings.REDIS_URL.get_secret_value()
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated, immutable application configuration.

    All fields are read from environment variables with the exact names shown.
    Required fields have no default; a missing variable raises a
    ValidationError at startup so misconfigured deploys fail fast rather than
    silently connecting to a wrong database or broker.
    """

    model_config = SettingsConfigDict(
        # Variable names match field names exactly — no prefix.
        env_prefix="",
        # Unrelated environment variables are silently discarded.
        extra="ignore",
        # Prevent field values from leaking in ValidationError messages.
        hide_input_in_errors=True,
        # Settings instance is immutable after creation.
        frozen=True,
    )

    # -- Connection strings (required, credentials redacted in repr/str) ----
    # Consumers must call .get_secret_value() to obtain the raw string.
    DATABASE_URL: SecretStr
    REDIS_URL: SecretStr

    # -- CORS ----------------------------------------------------------------
    # JSON list of allowed origins.  Default is an empty list (deny all
    # cross-origin requests) — see module docstring for format.
    CORS_ALLOW_ORIGINS: list[str] = []

    # -- Auth ----------------------------------------------------------------
    # JWT_SECRET must be at least 32 characters so HS256 has adequate entropy.
    JWT_SECRET: SecretStr
    # 60 minutes default; bounded so misconfiguration cannot create tokens that
    # last days (upper) or are useless (lower).
    JWT_EXPIRE_MINUTES: int = 60
    ADMIN_USERNAME: str
    # ADMIN_PASSWORD_HASH must be an argon2 hash (starts with "$argon2").
    # Validated at startup so a bcrypt or plaintext value fails fast rather
    # than silently passing all logins or verifying with the wrong algorithm.
    ADMIN_PASSWORD_HASH: SecretStr

    # -- Saga templates -------------------------------------------------------
    # Directory that contains <name>@v<N>.yaml template files.
    # Settings does NOT touch the filesystem; the template loader raises if the
    # path does not exist.  A relative path is resolved from the working directory
    # at startup time.
    SAGA_TEMPLATES_DIR: Path = Path("saga_templates")
    # Path to the tapestry YAML file (defines step types and their HTTP config).
    # Same laziness contract as SAGA_TEMPLATES_DIR.
    TAPESTRY_PATH: Path = Path("tapestry.yaml")

    # -- Runtime environment -------------------------------------------------
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"

    @field_validator("JWT_SECRET")
    @classmethod
    def _jwt_secret_min_length(cls, v: SecretStr) -> SecretStr:
        """Enforce a 32-character minimum so HS256 has sufficient entropy."""
        if len(v.get_secret_value()) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters")
        return v

    @field_validator("JWT_EXPIRE_MINUTES")
    @classmethod
    def _expire_minutes_bounds(cls, v: int) -> int:
        """Bound the token lifetime to avoid misconfigured extremes."""
        if not (1 <= v <= 1440):
            raise ValueError("JWT_EXPIRE_MINUTES must be between 1 and 1440")
        return v

    @field_validator("ADMIN_PASSWORD_HASH")
    @classmethod
    def _hash_must_be_argon2(cls, v: SecretStr) -> SecretStr:
        """Require an argon2 hash so plaintext passwords fail at startup."""
        if not v.get_secret_value().startswith("$argon2"):
            raise ValueError(
                "ADMIN_PASSWORD_HASH must be an argon2 hash (starts with '$argon2')"
            )
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance, constructed once from the
    environment.

    Cached so that repeated calls in dependency-injection chains are cheap.
    The cache is intentionally not invalidated at runtime; restart the process
    to pick up env changes.
    """
    return Settings()
