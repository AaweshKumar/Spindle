# Component: Spindle Architecture
# File: models.py
# Description: Domain models for saga templates in the Spindle orchestration platform.

"""Template domain models.

These are pure data objects — no I/O, no database access.  The loader
module is responsible for constructing them from YAML files.
"""

from __future__ import annotations

from dataclasses import dataclass


class TemplateNotFound(Exception):
    """Raised when a requested saga template name or version does not exist."""


class TemplateLoadError(ValueError):
    """Raised when a template file is malformed or references an unknown step type.

    The message always names the file path so the operator can locate the
    offending file without grepping.  Use ``raise TemplateLoadError(...) from exc``
    to preserve the original exception context.
    """


@dataclass(frozen=True)
class StepRef:
    """A reference to a tapestry step inside a saga template.

    ``id`` is unique within a template and becomes the step_id in SagaState.
    ``type`` must match a step_type registered in the TapestryStore.
    """

    id: str
    type: str


@dataclass(frozen=True)
class SagaTemplate:
    """An immutable, validated saga template.

    ``name`` and ``version`` are derived from the filename
    (``<name>@v<version>.yaml``), not from the file content.
    ``steps`` is ordered; the saga engine executes them left to right.
    """

    name: str
    version: int
    steps: tuple[StepRef, ...]
