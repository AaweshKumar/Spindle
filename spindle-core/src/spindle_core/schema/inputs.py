# Component: Spindle Architecture
# File: schema/inputs.py
# Description: Pure helper functions for resolving and validating workflow inputs.

"""Pure schema helpers for inspecting workflow input references and validating runtime inputs."""

from __future__ import annotations

from typing import Any, Mapping

from spindle_core.callspec.placeholders import iter_placeholders
from spindle_core.schema.errors import FieldError
from spindle_core.schema.models import SagaDefinition


def referenced_input_paths(definition: SagaDefinition) -> list[tuple[str, ...]]:
    """Return every unique input path referenced across steps and compensations."""
    seen: set[tuple[str, ...]] = set()
    result: list[tuple[str, ...]] = []

    for step in definition.steps:
        # Step action path
        for _, ph in iter_placeholders(step.action.path, "action.path"):
            if ph.namespace == "input" and ph.path not in seen:
                seen.add(ph.path)
                result.append(ph.path)

        # Step action body
        if step.action.body is not None:
            for _, ph in iter_placeholders(step.action.body, "action.body"):
                if ph.namespace == "input" and ph.path not in seen:
                    seen.add(ph.path)
                    result.append(ph.path)

        # Step compensation
        if step.compensation is not None:
            for _, ph in iter_placeholders(step.compensation.path, "compensation.path"):
                if ph.namespace == "input" and ph.path not in seen:
                    seen.add(ph.path)
                    result.append(ph.path)

            if step.compensation.body is not None:
                for _, ph in iter_placeholders(
                    step.compensation.body, "compensation.body"
                ):
                    if ph.namespace == "input" and ph.path not in seen:
                        seen.add(ph.path)
                        result.append(ph.path)

    return result


def _is_present(data: Any, path: tuple[str, ...]) -> bool:
    curr = data
    for seg in path:
        if isinstance(curr, Mapping):
            if seg not in curr:
                return False
            curr = curr[seg]
        elif isinstance(curr, (list, tuple)):
            if not seg.isdigit():
                return False
            idx = int(seg)
            if idx < 0 or idx >= len(curr):
                return False
            curr = curr[idx]
        else:
            return False
    return True


def missing_inputs(
    definition: SagaDefinition, input_dict: dict | None
) -> list[FieldError]:
    """Check required input paths against input_dict and return a FieldError for each missing path."""
    paths = referenced_input_paths(definition)
    errors: list[FieldError] = []

    for path in paths:
        if not _is_present(input_dict, path):
            dotted = ".".join(path)
            errors.append(
                FieldError(path=f"input.{dotted}", message="required input missing")
            )

    return errors
