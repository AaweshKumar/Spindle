# Component: Spindle Architecture
# File: loader.py
# Description: Loads saga templates from YAML files in the Spindle orchestration platform.

"""Saga template loader.

Loads ``<name>@v<N>.yaml`` files from a directory and validates them
against a TapestryStore.  The filename is the sole source of truth for
the template name and version.

Filename pattern (enforced, never skipped silently)::

    ^[a-z0-9][a-z0-9_-]*@v[1-9][0-9]*\\.yaml$

Any ``*.yaml`` file that does not match the pattern raises TemplateLoadError,
so operators know immediately if they have a misnamed file rather than
discovering a silent gap in the template registry.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from spindle_core.tapestry.loader import TapestryStore
from spindle_core.templates.models import (
    SagaTemplate,
    StepRef,
    TemplateLoadError,
    TemplateNotFound,
)

# Filename pattern: lowercase name, @ separator, version >= 1, .yaml extension.
_FILENAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*@v[1-9][0-9]*\.yaml$")


def _parse_filename(path: Path) -> tuple[str, int]:
    """Return (name, version) extracted from a valid template filename.

    Raises TemplateLoadError naming the path if the filename does not match
    the required pattern.
    """
    name = path.name
    if not _FILENAME_RE.match(name):
        raise TemplateLoadError(
            f"{path}: filename does not match required pattern "
            r"'^[a-z0-9][a-z0-9_-]*@v[1-9][0-9]*\.yaml$'"
        )
    stem = name[: -len(".yaml")]       # e.g. "order@v2"
    at_idx = stem.rindex("@v")
    template_name = stem[:at_idx]
    version = int(stem[at_idx + 2:])  # skip "@v"
    return template_name, version


def _load_one(path: Path, tapestry: TapestryStore) -> SagaTemplate:
    """Parse a single template YAML file and validate it against the tapestry.

    Raises TemplateLoadError naming the path for every structural or semantic
    problem.  yaml.safe_load is used exclusively so that ``!!python/object``
    payloads are rejected at the YAML layer.
    """
    template_name, version = _parse_filename(path)  # may raise

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TemplateLoadError(f"{path}: invalid YAML — {exc}") from exc

    if not isinstance(raw, dict):
        raise TemplateLoadError(f"{path}: content must be a YAML mapping, got {type(raw).__name__}")

    allowed_top_keys = {"steps"}
    extra_top = set(raw) - allowed_top_keys
    if extra_top:
        raise TemplateLoadError(f"{path}: unexpected top-level keys: {sorted(extra_top)}")

    steps_raw = raw.get("steps")
    if not steps_raw:
        raise TemplateLoadError(f"{path}: 'steps' must be a non-empty list")
    if not isinstance(steps_raw, list):
        raise TemplateLoadError(f"{path}: 'steps' must be a list, got {type(steps_raw).__name__}")

    seen_ids: set[str] = set()
    steps: list[StepRef] = []
    allowed_step_keys = {"id", "type"}

    for i, raw_step in enumerate(steps_raw):
        if not isinstance(raw_step, dict):
            raise TemplateLoadError(f"{path}: step {i} must be a mapping, got {type(raw_step).__name__}")
        extra_step = set(raw_step) - allowed_step_keys
        if extra_step:
            raise TemplateLoadError(f"{path}: step {i} has unexpected keys: {sorted(extra_step)}")

        step_id = raw_step.get("id")
        step_type = raw_step.get("type")
        if not isinstance(step_id, str):
            raise TemplateLoadError(f"{path}: step {i} 'id' must be a string")
        if not isinstance(step_type, str):
            raise TemplateLoadError(f"{path}: step {i} 'type' must be a string")

        if step_id in seen_ids:
            raise TemplateLoadError(f"{path}: duplicate step id {step_id!r}")
        seen_ids.add(step_id)

        if tapestry.get(step_type) is None:
            raise TemplateLoadError(
                f"{path}: step {step_id!r} references unknown step type {step_type!r} "
                f"(not in tapestry)"
            )

        steps.append(StepRef(id=step_id, type=step_type))

    return SagaTemplate(name=template_name, version=version, steps=tuple(steps))


class TemplateStore:
    """In-memory registry of validated saga templates.

    Build via ``TemplateStore.from_dir(path, tapestry)``; do not construct
    directly.  Thread-safe for reads (dict access is GIL-protected and the
    store is never mutated after construction).
    """

    def __init__(self, templates: list[SagaTemplate]) -> None:
        # Two-level dict: name -> version -> SagaTemplate
        self._by_name: dict[str, dict[int, SagaTemplate]] = {}
        for t in templates:
            self._by_name.setdefault(t.name, {})[t.version] = t

    def get(self, name: str, version: int | None = None) -> SagaTemplate:
        """Return the requested template.

        When *version* is None, return the highest registered version.
        Raises TemplateNotFound for an unknown name or missing version.
        """
        versions = self._by_name.get(name)
        if versions is None:
            raise TemplateNotFound(f"No template named {name!r}")
        if version is None:
            return versions[max(versions)]
        t = versions.get(version)
        if t is None:
            raise TemplateNotFound(f"Template {name!r} version {version} not found")
        return t

    def versions(self, name: str) -> tuple[int, ...]:
        """Return the registered versions for *name* in ascending order.

        Raises TemplateNotFound for an unknown name.
        """
        v = self._by_name.get(name)
        if v is None:
            raise TemplateNotFound(f"No template named {name!r}")
        return tuple(sorted(v))

    @classmethod
    def from_dir(cls, directory: Path, tapestry: TapestryStore) -> "TemplateStore":
        """Load and validate all templates from *directory*.

        Every ``*.yaml`` file must match the filename pattern; any mismatch
        raises immediately so silent omissions cannot occur.  Missing directory
        also raises.

        Raises TemplateLoadError naming the file path on any problem.
        """
        if not directory.exists():
            raise TemplateLoadError(f"{directory}: template directory does not exist")
        if not directory.is_dir():
            raise TemplateLoadError(f"{directory}: path is not a directory")

        yaml_files = sorted(directory.glob("*.yaml"))
        templates: list[SagaTemplate] = []
        for path in yaml_files:
            templates.append(_load_one(path, tapestry))  # may raise

        return cls(templates)
