# Component: Spindle Architecture
# File: main.py
# Description: FastAPI application factory for the Spindle orchestration platform.

"""FastAPI application factory.

Run via uvicorn's factory mode so that module-level import does not require
env vars to be set:

    uvicorn spindle_core.main:create_app --factory

For local development with auto-reload:

    uvicorn spindle_core.main:create_app --factory --reload
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from spindle_core.api.lifespan import make_lifespan
from spindle_core.api.sagas import router as sagas_router
from spindle_core.auth.router import router as auth_router
from spindle_core.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construct and return a configured FastAPI application.

    Args:
        settings: An explicit Settings instance.  When None (the default),
            ``get_settings()`` is called so that the factory mode invocation
            (``uvicorn … --factory``) picks up environment variables
            automatically.  Passing an explicit instance keeps app construction
            free of global state, which simplifies testing.

    Returns:
        A fully configured FastAPI application with the auth and sagas routers
        mounted and the outbox-relay lifespan wired in.
    """
    if settings is None:
        settings = get_settings()

    app = FastAPI(
        title="Spindle",
        # Version comes from the installed package metadata if available,
        # otherwise falls back to "0.0.0".
        version=_package_version(),
        description="Saga-orchestration platform.",
        lifespan=make_lifespan(settings),
    )

    # Settings override: every dependency that calls Depends(get_settings) —
    # including auth — will receive the Settings instance passed to create_app.
    # This is the single source of truth for configuration across all layers.
    app.dependency_overrides[get_settings] = lambda: settings

    # CORS is the only middleware added at bootstrap.
    # Origins come exclusively from Settings, so the default (empty list)
    # denies all cross-origin requests.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ALLOW_ORIGINS,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Auth router (P1).
    app.include_router(auth_router)
    # Sagas router (P2).
    app.include_router(sagas_router)

    return app


def _package_version() -> str:
    """Return the installed package version, or '0.0.0' if unavailable."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("spindle_core")
    except PackageNotFoundError:
        return "0.0.0"
