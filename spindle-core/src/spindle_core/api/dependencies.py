# Component: Spindle Architecture
# File: dependencies.py
# Description: FastAPI dependency providers for the sagas API in the Spindle platform.

"""API-layer dependency providers.

All providers read from ``request.app.state``, which is populated by the
lifespan in ``api/lifespan.py``.  Fixed provider names are part of the P2
contract and must not be renamed.

``TapestryLoadError`` is defined here because loading the tapestry is an
API-bootstrap concern, not a tapestry-layer concern.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import Request

from spindle_core.orchestrator.runner import Runner
from spindle_core.persistence.repositories import PgSagaRepository
from spindle_core.streams.producer import RedisCommandBus
from spindle_core.tapestry.loader import TapestryStore
from spindle_core.templates.loader import TemplateStore


class TapestryLoadError(ValueError):
    """Raised when the tapestry YAML cannot be loaded.

    The message always names the file path so operators can locate the
    offending file.  Use ``raise TapestryLoadError(...) from exc`` to
    preserve the original exception context.
    """


def load_tapestry_store(path: Path) -> TapestryStore:
    """Load a TapestryStore from the YAML file at *path*.

    Validates structure (must be a mapping) and delegates step parsing to
    TapestryStore.from_dict.  Missing file, YAML parse error, non-mapping
    top-level, and from_dict failures each raise TapestryLoadError naming
    the path so the operator has an actionable error message.

    yaml.safe_load is used exclusively — no arbitrary Python object
    deserialisation.
    """
    if not path.exists():
        raise TapestryLoadError(f"{path}: tapestry file not found")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TapestryLoadError(f"{path}: invalid YAML — {exc}") from exc

    if not isinstance(raw, dict):
        raise TapestryLoadError(
            f"{path}: tapestry file must be a YAML mapping, got {type(raw).__name__}"
        )

    try:
        return TapestryStore.from_dict(raw)
    except (ValueError, KeyError) as exc:
        raise TapestryLoadError(f"{path}: malformed tapestry — {exc}") from exc


# ---------------------------------------------------------------------------
# Fixed-name dependency providers (P2 contract)
# ---------------------------------------------------------------------------

def get_saga_repository(request: Request) -> PgSagaRepository:
    """Return a PgSagaRepository bound to the app's session factory.

    PgSagaRepository accepts a *session factory* (a callable that returns a
    new Session context manager), not a Session itself.  The factory is stored
    on app.state once at startup; each repository method opens and closes its
    own session internally, so no per-request session is created here.
    This matches how PgSagaRepository.load / .save each do
    ``with self._session_factory() as session``.
    """
    return PgSagaRepository(request.app.state.session_factory)


def get_command_bus(request: Request) -> RedisCommandBus:
    """Return a RedisCommandBus backed by the app's Redis client.

    No route in P2 uses the command bus directly; the provider exists so
    later tasks can inject it without modifying the dependency graph.
    """
    return RedisCommandBus(request.app.state.redis)


def get_runner(
    repo: PgSagaRepository = None,  # injected below via Depends
    request: Request = None,
) -> Runner:
    """Return a Runner wrapping the request-scoped PgSagaRepository.

    FastAPI resolves sub-dependencies, so the actual signature used by
    the router uses Depends(get_saga_repository).
    """
    # Note: the router uses Depends(get_saga_repository) directly; this
    # function is a named provider for external injection / testing.
    return Runner(repo)


def get_template_store(request: Request) -> TemplateStore:
    """Return the shared TemplateStore loaded at startup."""
    return request.app.state.templates
