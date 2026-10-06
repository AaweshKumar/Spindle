# Component: Spindle Architecture
# File: models.py
# Description: Per-step Tapestry configuration model.
#   Defines the data shapes that describe, for each step type:
#     - how to build the HTTP request (RequestConfig)
#     - how to detect success/failure from the response (SuccessCheck)
#     - whether/how to call compensation (CompensationConfig)
#   No I/O, no external imports.  Pure Python dataclasses.

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SuccessCheck:
    """
    Defines how to determine whether a call to a microservice succeeded.

    Evaluation order (both checks must pass):
      1. HTTP status code must be in ok_status_codes.
      2. If body_field is set, the JSON response body is inspected:
         - body_field_value is non-None  →  body[body_field] must equal that value.
         - body_field_value is None      →  body[body_field] must be present and truthy.
    """

    # HTTP status codes that count as success.  Defaults to 200-299 inclusive.
    ok_status_codes: frozenset[int] = field(
        default_factory=lambda: frozenset(range(200, 300))
    )
    # Optional: name of a JSON response-body field to inspect.
    body_field: str | None = None
    # Required only when body_field is set and an exact string match is needed.
    body_field_value: str | None = None


@dataclass
class RequestConfig:
    """How to build an HTTP request for a step (execute or compensate) call."""

    url: str
    method: str = "POST"
    # Explicit field mapping: {http_body_key: source_message_field}.
    # If empty, the full incoming message dict is forwarded as the JSON body.
    field_mapping: dict[str, str] = field(default_factory=dict)
    # Extra HTTP headers merged on top of {"Content-Type": "application/json"}.
    headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 30.0


@dataclass
class CompensationConfig:
    """
    Compensation call config for a step, OR a declaration that compensation
    is not automatable and requires manual review.

    Invariant enforced by the loader:
      requires_manual_review=True   →  request must be None.
      requires_manual_review=False  →  request must be non-None.
    """

    requires_manual_review: bool = False
    # The HTTP call to make for automatic compensation.  None only when
    # requires_manual_review=True.
    request: RequestConfig | None = None
    # How to evaluate the compensation response.  If None, falls back to
    # the step's execute_success_check.
    success_check: SuccessCheck | None = None


@dataclass
class StepConfig:
    """Complete per-step Tapestry configuration entry."""

    step_type: str
    execute: RequestConfig
    execute_success_check: SuccessCheck = field(default_factory=SuccessCheck)
    # None means compensation is not defined for this step type.
    # connectors/ will publish CompensationFailed with an explanatory reason.
    compensation: CompensationConfig | None = None
