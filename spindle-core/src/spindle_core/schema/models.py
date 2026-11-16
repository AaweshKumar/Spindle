# Component: Spindle Architecture
# File: models.py
# Description: Pydantic v2 models for user-defined service and saga definitions.

"""Domain models for user-defined sagas.

Every model inherits from ``BaseModel`` with ``extra = "forbid"`` so unknown
fields are strictly rejected during parsing.  The structure is dual-parseable
from YAML and JSON into identical Python instances.

Important note on effective vs stored values:
    Optional fields like ``Step.retry``, ``Step.success``, and compensation
    defaults (e.g. timeout falling back to step timeout) are resolved during
    *validation* in ``schema.validator``; the models themselves hold the raw
    user-declared values (or explicit field defaults).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from spindle_core.callspec.placeholders import SECRET_NAME_RE
from spindle_core.schema.constants import ALLOWED_AUTH_TYPES


class SchemaBase(BaseModel):
    """Shared base model enforcing strict field rejection."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class AuthReference(SchemaBase):
    """Authentication reference for a registered service.

    Inline secrets or tokens are strictly forbidden; ``secret_ref`` names a
    credential that the calling environment or secret store resolves.
    """

    type: str = Field(
        ...,
        description="Auth type, e.g. 'bearer'. Must be in ALLOWED_AUTH_TYPES.",
    )
    secret_ref: str = Field(
        ...,
        description="Reference name for the secret. Must match ^[A-Z][A-Z0-9_]*$",
    )

    @field_validator("type")
    @classmethod
    def _validate_auth_type(cls, value: str) -> str:
        if value not in ALLOWED_AUTH_TYPES:
            raise ValueError(
                f"auth type not allowed; must be one of {sorted(ALLOWED_AUTH_TYPES)}"
            )
        return value

    @field_validator("secret_ref")
    @classmethod
    def _validate_secret_ref(cls, value: str) -> str:
        if not SECRET_NAME_RE.fullmatch(value):
            raise ValueError(
                "secret_ref must be a reference name matching ^[A-Z][A-Z0-9_]*$"
            )
        return value


class ServiceDefinition(SchemaBase):
    """Service registration model (Layer 1)."""

    name: str
    base_url: str
    health: str = Field(..., description="Health check path, e.g. '/health'")
    auth: AuthReference


class Action(SchemaBase):
    """Action definition for an execute or compensate call."""

    method: str
    path: str
    body: Any = Field(
        default=None,
        description="Optional request body template (JSON value or template string).",
    )


class SuccessCriteria(SchemaBase):
    """Acceptable status codes for step or compensation execution."""

    status: list[int] = Field(
        ...,
        description="List of acceptable HTTP status codes, each in range 100..599.",
    )

    @field_validator("status")
    @classmethod
    def _validate_status_codes(cls, value: list[int]) -> list[int]:
        if not value:
            raise ValueError("status list must not be empty")
        for code in value:
            if not isinstance(code, int) or isinstance(code, bool):
                raise ValueError("status codes must be integers")
            if not 100 <= code <= 599:
                raise ValueError(f"status code {code} out of range 100..599")
        return value


class RetryPolicy(SchemaBase):
    """Retry policy declaration matching callspec parameters.

    Field names mirror ``spindle_core.callspec.retry.RetryPolicy`` so schema
    definitions map 1:1 onto callspec runtime objects.
    """

    max_attempts: int = Field(default=3, description="1 = no retries.")
    initial_backoff_seconds: float = 0.5
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 10.0


class Compensation(SchemaBase):
    """Compensation call definition for a step."""

    method: str
    path: str
    body: Any = Field(default=None)
    timeout_s: float | None = Field(default=None)
    retry: RetryPolicy | None = Field(default=None)
    success: SuccessCriteria | None = Field(default=None)


class Step(SchemaBase):
    """A single step within a saga workflow definition."""

    name: str
    service: str
    mode: Literal["sync"] = "sync"
    action: Action
    compensation: Compensation | None = Field(default=None)
    non_compensable: bool = False
    timeout_s: float
    retry: RetryPolicy | None = Field(default=None)
    idempotency_header: str | None = Field(default=None)
    success: SuccessCriteria | None = Field(default=None)


class SagaDefinition(SchemaBase):
    """Saga definition model (Layer 2)."""

    name: str
    steps: list[Step]
