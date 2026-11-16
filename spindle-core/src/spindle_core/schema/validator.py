# Component: Spindle Architecture
# File: validator.py
# Description: Schema validator for user-defined sagas and service definitions.

"""Validator and parser layer for user-defined services and sagas.

All validation functions collect **every** error in the input structure before
raising a single ``ValidationError`` containing all accumulated ``FieldError``s.
Pydantic structural errors are caught internally and converted into bracket-notation
field paths (e.g. ``steps[2].compensation``) so callers never handle a raw
``pydantic.ValidationError``.

No I/O, no DB access, and no environment reads.
"""

from __future__ import annotations

import json
from typing import Any, Collection

import pydantic as _pydantic
import yaml

from spindle_core.callspec.placeholders import (
    iter_placeholders,
    PlaceholderSyntaxError,
)
from spindle_core.callspec.retry import RetryPolicy as CallspecRetryPolicy
from spindle_core.callspec.url_rules import check_url_template
from spindle_core.schema.constants import ALLOWED_TEMPLATE_ROOTS, HTTP_METHODS
from spindle_core.schema.errors import FieldError, ValidationError
from spindle_core.schema.models import (
    Compensation,
    RetryPolicy as SchemaRetryPolicy,
    SagaDefinition,
    ServiceDefinition,
    Step,
    SuccessCriteria,
)


def _pydantic_loc_to_path(loc: tuple[int | str, ...]) -> str:
    """Convert a Pydantic error loc tuple into a bracket-notation path string."""
    if not loc:
        return ""
    parts: list[str] = []
    for item in loc:
        if isinstance(item, int):
            parts.append(f"[{item}]")
        else:
            if parts and not parts[-1].endswith("]"):
                parts.append(f".{item}")
            else:
                parts.append(str(item))
    return "".join(parts)


def _convert_pydantic_error(exc: _pydantic.ValidationError) -> list[FieldError]:
    """Convert a pydantic.ValidationError into a list of FieldErrors."""
    errors: list[FieldError] = []
    for err in exc.errors():
        path = _pydantic_loc_to_path(err["loc"])
        msg = err["msg"]
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, ") :]
        errors.append(FieldError(path=path, message=msg))
    return errors


def _build_callspec_retry(
    retry: SchemaRetryPolicy | None, path: str, errors: list[FieldError]
) -> CallspecRetryPolicy | None:
    """Attempt to build a CallspecRetryPolicy from a SchemaRetryPolicy.

    If retry is None, returns NO_RETRY (max_attempts=1).
    If construction fails, appends a FieldError at `path` and returns None.
    """
    if retry is None:
        return CallspecRetryPolicy(max_attempts=1)
    try:
        return CallspecRetryPolicy(
            max_attempts=retry.max_attempts,
            initial_backoff_seconds=retry.initial_backoff_seconds,
            backoff_multiplier=retry.backoff_multiplier,
            max_backoff_seconds=retry.max_backoff_seconds,
        )
    except ValueError as exc:
        errors.append(FieldError(path=path, message=str(exc)))
        return None


def _validate_path_format(path_val: str, path_loc: str, errors: list[FieldError]) -> None:
    """Rule 7 helper: path must start with '/', no scheme, no leading '//'."""
    if not isinstance(path_val, str):
        errors.append(FieldError(path=path_loc, message="path must be a string"))
        return
    if "://" in path_val:
        errors.append(FieldError(path=path_loc, message="path must not contain scheme ('://')"))
        return
    if not path_val.startswith("/"):
        errors.append(FieldError(path=path_loc, message="path must start with '/'"))
        return
    if path_val.startswith("//"):
        errors.append(FieldError(path=path_loc, message="path must not start with '//'"))


def _validate_method_allowlist(method_val: str, path_loc: str, errors: list[FieldError]) -> None:
    """Rule 7 helper: method must be in HTTP_METHODS allowlist."""
    if not isinstance(method_val, str):
        errors.append(FieldError(path=path_loc, message="method must be a string"))
        return
    upper = method_val.upper()
    if upper not in HTTP_METHODS:
        errors.append(
            FieldError(
                path=path_loc,
                message=f"HTTP method '{method_val}' not allowed; must be one of {sorted(HTTP_METHODS)}",
            )
        )


def _validate_template_placeholders(
    value: Any,
    base_path: str,
    step_idx: int,
    earlier_step_names: set[str],
    errors: list[FieldError],
    *,
    allow_current_step: bool = False,
    current_step_name: str | None = None,
) -> None:
    """Rule 5: check all placeholders in a string or structure.

    Roots must be 'input' or 'steps'.
    'steps.<step_name>.response[...]' must name an earlier step
    (or current step if allow_current_step=True).
    'saga' and 'secret' are rejected with clear messages.
    """
    if value is None:
        return

    try:
        placeholder_tuples = list(iter_placeholders(value, where=base_path))
    except PlaceholderSyntaxError as exc:
        errors.append(FieldError(path=base_path, message=str(exc)))
        return
    except Exception as exc:
        errors.append(FieldError(path=base_path, message=f"invalid placeholder syntax: {exc}"))
        return

    for loc, ph in placeholder_tuples:
        ns = ph.namespace
        if ns not in ALLOWED_TEMPLATE_ROOTS:
            if ns == "saga":
                errors.append(
                    FieldError(
                        path=loc,
                        message="'saga' placeholders are not allowed in saga step templates",
                    )
                )
            elif ns == "secret":
                errors.append(
                    FieldError(
                        path=loc,
                        message="'secret' placeholders are not allowed in saga step templates",
                    )
                )
            else:
                errors.append(
                    FieldError(
                        path=loc,
                        message=f"unknown placeholder namespace '{ns}'; allowed roots are 'input', 'steps'",
                    )
                )
            continue

        if ns == "steps":
            # path is tuple of segments after 'steps', e.g. ('s1', 'response', 'id')
            if len(ph.path) < 2 or ph.path[1] != "response":
                errors.append(
                    FieldError(
                        path=loc,
                        message=f"steps reference must follow 'steps.<step_name>.response[.<field>]', got '{{{{{ph.raw}}}}}'",
                    )
                )
                continue

            ref_step = ph.path[0]
            valid_refs = set(earlier_step_names)
            if allow_current_step and current_step_name:
                valid_refs.add(current_step_name)

            if ref_step not in valid_refs:
                if ref_step == current_step_name and not allow_current_step:
                    errors.append(
                        FieldError(
                            path=loc,
                            message=f"step '{current_step_name}' cannot reference its own response in action",
                        )
                    )
                else:
                    errors.append(
                        FieldError(
                            path=loc,
                            message=f"step reference '{ref_step}' is unknown or refers to a later step; must name an earlier step",
                        )
                    )


def service_names(services: Collection[ServiceDefinition]) -> frozenset[str]:
    """Pure helper: extract service names from a collection of ServiceDefinitions."""
    return frozenset(s.name for s in services)


def validate_service(
    data: Any, *, allowed_hosts: frozenset[str] | None = None
) -> ServiceDefinition:
    """Validate a single ServiceDefinition (dict or raw object).

    Collects all errors and raises ValidationError if any are found.
    """
    errors: list[FieldError] = []

    # Step 1: Pydantic parsing / structural validation
    if isinstance(data, ServiceDefinition):
        svc = data
    else:
        try:
            svc = ServiceDefinition.model_validate(data)
        except _pydantic.ValidationError as exc:
            errors.extend(_convert_pydantic_error(exc))
            raise ValidationError(errors) from None

    # Step 2: Layer 1 specific rules.
    # Auth type and secret_ref format are enforced by AuthReference's Pydantic
    # field validators, which raise here and convert to FieldErrors at
    # auth.type / auth.secret_ref via _convert_pydantic_error.

    # Base URL check via callspec url_rules
    url_problems = check_url_template(svc.base_url, allowed_hosts=allowed_hosts)
    for prob in url_problems:
        errors.append(FieldError(path="base_url", message=prob))

    # Health path check
    _validate_path_format(svc.health, "health", errors)

    if errors:
        raise ValidationError(errors)
    return svc


def validate_services(
    items: Any, *, allowed_hosts: frozenset[str] | None = None
) -> list[ServiceDefinition]:
    """Validate a list of ServiceDefinitions."""
    if not isinstance(items, list):
        raise ValidationError([FieldError(path="", message="services must be a list")])

    errors: list[FieldError] = []
    validated: list[ServiceDefinition] = []

    for i, item in enumerate(items):
        try:
            sv = validate_service(item, allowed_hosts=allowed_hosts)
            validated.append(sv)
        except ValidationError as exc:
            for err in exc.errors:
                path = f"[{i}].{err.path}" if err.path else f"[{i}]"
                errors.append(FieldError(path=path, message=err.message))

    if errors:
        raise ValidationError(errors)
    return validated


def validate_saga(data: Any, *, known_services: Collection[str]) -> SagaDefinition:
    """Validate a SagaDefinition against known_services.

    Collects ALL errors across rules 1-8 before raising a single ValidationError.
    """
    errors: list[FieldError] = []

    # Structural parse via Pydantic
    if isinstance(data, SagaDefinition):
        saga = data
    else:
        try:
            saga = SagaDefinition.model_validate(data)
        except _pydantic.ValidationError as exc:
            errors.extend(_convert_pydantic_error(exc))
            raise ValidationError(errors) from None

    # Check non-empty steps
    if not saga.steps:
        errors.append(FieldError(path="steps", message="saga must have at least one step"))
        raise ValidationError(errors)

    known_set = set(known_services)
    seen_step_names: set[str] = set()
    earlier_step_names: set[str] = set()
    num_steps = len(saga.steps)

    for i, step in enumerate(saga.steps):
        step_path = f"steps[{i}]"

        # Rule 1: Unique step names
        if step.name in seen_step_names:
            errors.append(
                FieldError(
                    path=f"{step_path}.name",
                    message=f"duplicate step name '{step.name}' in saga",
                )
            )
        seen_step_names.add(step.name)

        # Rule 6: Service must exist in known_services
        if step.service not in known_set:
            errors.append(
                FieldError(
                    path=f"{step_path}.service",
                    message=f"service '{step.service}' is unknown; not in known_services",
                )
            )

        # Rule 2: XOR compensation and non_compensable
        has_comp = step.compensation is not None
        is_nc = step.non_compensable is True
        if has_comp and is_nc:
            errors.append(
                FieldError(
                    path=f"{step_path}.compensation",
                    message="step cannot declare both compensation and non_compensable: true",
                )
            )
        elif not has_comp and not is_nc:
            errors.append(
                FieldError(
                    path=f"{step_path}.compensation",
                    message="step must declare compensation OR non_compensable: true",
                )
            )

        # Rule 3: non_compensable only on final step (pivot rule)
        if is_nc and i != num_steps - 1:
            errors.append(
                FieldError(
                    path=f"{step_path}.non_compensable",
                    message="non_compensable step is only allowed as the final step in a saga",
                )
            )

        # Rule 4: timeout_s required and > 0, then fits_budget check
        fwd_retry = _build_callspec_retry(step.retry, f"{step_path}.retry", errors)
        if step.timeout_s <= 0:
            errors.append(
                FieldError(
                    path=f"{step_path}.timeout_s",
                    message="timeout_s must be > 0",
                )
            )
        elif fwd_retry is not None:
            if not fwd_retry.fits_budget(step.timeout_s):
                errors.append(
                    FieldError(
                        path=f"{step_path}.timeout_s",
                        message=f"worst-case execution duration ({fwd_retry.worst_case_seconds(step.timeout_s):.1f}s) exceeds maximum step budget of 120.0s",
                    )
                )

        # Rule 7: Action method & path
        _validate_method_allowlist(step.action.method, f"{step_path}.action.method", errors)
        _validate_path_format(step.action.path, f"{step_path}.action.path", errors)

        # Rule 5: Action placeholders (path & body)
        _validate_template_placeholders(
            step.action.path,
            f"{step_path}.action.path",
            i,
            earlier_step_names,
            errors,
            current_step_name=step.name,
        )
        _validate_template_placeholders(
            step.action.body,
            f"{step_path}.action.body",
            i,
            earlier_step_names,
            errors,
            current_step_name=step.name,
        )

        # Compensation checks
        if step.compensation is not None:
            comp = step.compensation
            comp_path = f"{step_path}.compensation"

            # Rule 7: Compensation method & path
            _validate_method_allowlist(comp.method, f"{comp_path}.method", errors)
            _validate_path_format(comp.path, f"{comp_path}.path", errors)

            # Effective compensation timeout and retry
            comp_timeout = comp.timeout_s if comp.timeout_s is not None else step.timeout_s
            comp_retry = (
                _build_callspec_retry(comp.retry, f"{comp_path}.retry", errors)
                if comp.retry is not None
                else fwd_retry
            )

            # Rule 4 on compensation
            if comp.timeout_s is not None and comp.timeout_s <= 0:
                errors.append(
                    FieldError(
                        path=f"{comp_path}.timeout_s",
                        message="compensation timeout_s must be > 0",
                    )
                )
            elif comp_retry is not None and comp_timeout > 0:
                if not comp_retry.fits_budget(comp_timeout):
                    retry_path = (
                        f"{comp_path}.retry" if comp.retry is not None else f"{comp_path}.timeout_s"
                    )
                    errors.append(
                        FieldError(
                            path=retry_path,
                            message=f"worst-case compensation duration ({comp_retry.worst_case_seconds(comp_timeout):.1f}s) exceeds maximum step budget of 120.0s",
                        )
                    )

            # Rule 5: Compensation placeholders (allows self-reference)
            _validate_template_placeholders(
                comp.path,
                f"{comp_path}.path",
                i,
                earlier_step_names,
                errors,
                allow_current_step=True,
                current_step_name=step.name,
            )
            _validate_template_placeholders(
                comp.body,
                f"{comp_path}.body",
                i,
                earlier_step_names,
                errors,
                allow_current_step=True,
                current_step_name=step.name,
            )

        earlier_step_names.add(step.name)

    if errors:
        raise ValidationError(errors)
    return saga


def parse_service(
    text: str, *, allowed_hosts: frozenset[str] | None = None
) -> ServiceDefinition:
    """Parse YAML or JSON text into a validated ServiceDefinition."""
    try:
        data = yaml.safe_load(text)
    except Exception as exc:
        raise ValidationError([FieldError(path="", message=f"invalid YAML/JSON text: {exc}")]) from None
    return validate_service(data, allowed_hosts=allowed_hosts)


def parse_saga(text: str, *, known_services: Collection[str]) -> SagaDefinition:
    """Parse YAML or JSON text into a validated SagaDefinition."""
    try:
        data = yaml.safe_load(text)
    except Exception as exc:
        raise ValidationError([FieldError(path="", message=f"invalid YAML/JSON text: {exc}")]) from None
    return validate_saga(data, known_services=known_services)
