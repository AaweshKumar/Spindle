# Component: Spindle Architecture
# File: sagas.py
# Description: Sagas API router for the Spindle orchestration platform.

"""Sagas API router.

Provides:
  POST /sagas  — start a new saga from a named template
  GET  /sagas/{saga_id} — fetch the current status of a saga

Both endpoints are protected by ``get_current_user`` (declared at router level
so every future route automatically requires auth without per-route boilerplate).

All handler functions are synchronous (sync ``def``) because Runner and the
PgSagaRepository are synchronous — they use SQLAlchemy sessions and blocking
database calls.  Using async def with blocking I/O would starve the event loop.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from spindle_core.api.dependencies import (
    get_runner,
    get_saga_repository,
    get_template_store,
)
from spindle_core.auth.dependencies import get_current_user
from spindle_core.engine import start_saga
from spindle_core.orchestrator.ports import ConcurrencyConflict
from spindle_core.orchestrator.runner import Runner
from spindle_core.persistence.repositories import PgSagaRepository
from spindle_core.templates.loader import TemplateStore
from spindle_core.templates.models import TemplateNotFound

router = APIRouter(
    prefix="/sagas",
    tags=["sagas"],
    dependencies=[Depends(get_current_user)],
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class StartSagaRequest(BaseModel):
    """Body for POST /sagas.

    ``extra="forbid"`` ensures that passing an inline ``steps`` key (deferred
    feature) results in a 422 rather than silently ignoring it.
    """

    # INLINE-STEPS (deferred)
    model_config = {"extra": "forbid"}

    template: Annotated[
        str,
        Field(
            pattern=r"^[a-z0-9][a-z0-9_-]*$",
            max_length=64,
            description="Name of the saga template to use.",
        ),
    ]
    version: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description="Template version. Omit to use the latest version.",
        ),
    ] = None


class SagaResponse(BaseModel):
    """Response for POST /sagas and GET /sagas/{saga_id}."""

    saga_id: str
    status: str


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

@router.post("", status_code=status.HTTP_201_CREATED, response_model=SagaResponse)
def start_saga_handler(
    body: StartSagaRequest,
    template_store: TemplateStore = Depends(get_template_store),
    runner: Runner = Depends(get_runner),
) -> SagaResponse:
    """Start a new saga from a named template.

    Flow:
    1. Resolve the template (name + optional version) from the store.
    2. Convert steps to (step_id, step_type) pairs expected by the engine.
    3. Call engine.start_saga to get initial state and commands.
    4. Persist via Runner.start (which calls repo.save at version=0).
    5. Return the saga_id and status string.

    ConcurrencyConflict on Runner.start is returned as 409.  UUID collisions
    are astronomically unlikely but the engine's unique-insert guard catches
    them, so the 409 path is reachable in theory.
    """
    try:
        template = template_store.get(body.template, body.version)
    except TemplateNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Saga template not found",
        )

    steps = [(s.id, s.type) for s in template.steps]
    saga_id = str(uuid.uuid4())
    state, commands = start_saga(saga_id, steps)

    try:
        runner.start(state, commands)
    except ConcurrencyConflict:
        # UUID collision or duplicate — should not happen in practice.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Saga could not be started due to a conflict",
        )

    return SagaResponse(saga_id=saga_id, status=state.status.value)


@router.get("/{saga_id}", response_model=SagaResponse)
def get_saga_handler(
    saga_id: Annotated[str, Field(max_length=64)],
    repo: PgSagaRepository = Depends(get_saga_repository),
) -> SagaResponse:
    """Return the current status of a saga.

    Only ``saga_id`` and ``status`` are returned; internal fields such as
    step-level state are intentionally not exposed at this endpoint.
    """
    loaded = repo.load(saga_id)
    if loaded is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Saga not found",
        )
    return SagaResponse(saga_id=loaded.state.saga_id, status=loaded.state.status.value)
