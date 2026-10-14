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
from typing import Literal

from pydantic import SecretStr
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

    # -- Runtime environment -------------------------------------------------
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance, constructed once from the
    environment.

    Cached so that repeated calls in dependency-injection chains are cheap.
    The cache is intentionally not invalidated at runtime; restart the process
    to pick up env changes.
    """
    return Settings()
