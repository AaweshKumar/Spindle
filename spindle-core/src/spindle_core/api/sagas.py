# Component: Spindle Architecture
# File: sagas.py
# Description: Sagas API router for the Spindle orchestration platform.

"""Sagas API router.

Provides:
  POST /sagas  — start a new saga from a named template
  GET  /sagas  — list sagas newest first with keyset pagination and status filter
  GET  /sagas/{saga_id} — fetch the current status and step details of a saga

Both endpoints are protected by ``get_current_user`` (declared at router level
so every future route automatically requires auth without per-route boilerplate).

All handler functions are synchronous (sync ``def``) because Runner and the
PgSagaRepository are synchronous — they use SQLAlchemy sessions and blocking
database calls.  Using async def with blocking I/O would starve the event loop.
"""

from __future__ import annotations

from datetime import datetime
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from spindle_core.api.cursor import InvalidCursor, decode_cursor, encode_cursor
from spindle_core.api.dependencies import (
    get_runner,
    get_saga_repository,
    get_template_store,
)
from spindle_core.auth.dependencies import get_current_user
from spindle_core.engine import SagaStatus, start_saga
from spindle_core.orchestrator.ports import ConcurrencyConflict
from spindle_core.orchestrator.runner import Runner
from spindle_core.persistence.repositories import PgSagaRepository, SagaSummary
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
    """Response for POST /sagas."""

    saga_id: str
    status: str


class StepResponse(BaseModel):
    """Step execution status within a saga."""

    step_id: str
    step_type: str
    status: str


class SagaDetailResponse(SagaResponse):
    """Detailed response for saga views including metadata and step states."""

    template_name: str | None
    template_version: int | None
    created_at: datetime
    updated_at: datetime
    steps: list[StepResponse]


class SagaListResponse(BaseModel):
    """Paginated list of sagas with opaque continuation cursor."""

    items: list[SagaDetailResponse]
    next_cursor: str | None


def _to_detail(summary: SagaSummary) -> SagaDetailResponse:
    """Project a repository SagaSummary into an API detail response."""
    steps = [
        StepResponse(
            step_id=step.step_id,
            step_type=step.step_type,
            status=step.status.value,
        )
        for step in summary.state.steps
    ]
    return SagaDetailResponse(
        saga_id=summary.state.saga_id,
        status=summary.state.status.value,
        template_name=summary.template_name,
        template_version=summary.template_version,
        created_at=summary.created_at,
        updated_at=summary.updated_at,
        steps=steps,
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

@router.post("", status_code=status.HTTP_201_CREATED, response_model=SagaResponse)
def start_saga_handler(
    body: StartSagaRequest,
    template_store: TemplateStore = Depends(get_template_store),
    runner: Runner = Depends(get_runner),
) -> SagaResponse:
    """Start a new saga from a named template."""
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
        runner.start(
            state,
            commands,
            template_name=template.name,
            template_version=template.version,
        )
    except ConcurrencyConflict:
        # UUID collision or duplicate — should not happen in practice.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Saga could not be started due to a conflict",
        )

    return SagaResponse(saga_id=saga_id, status=state.status.value)


@router.get("", response_model=SagaListResponse)
def list_sagas_handler(
    status_filter: Annotated[SagaStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    repo: PgSagaRepository = Depends(get_saga_repository),
) -> SagaListResponse:
    """List sagas newest first with optional status filtering and keyset pagination."""
    after = None
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursor:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Invalid cursor",
            )

    rows = repo.list_summaries(status=status_filter, limit=limit + 1, after=after)
    page = rows[:limit]
    next_cursor = (
        encode_cursor(page[-1].created_at, page[-1].state.saga_id)
        if len(rows) > limit
        else None
    )
    return SagaListResponse(
        items=[_to_detail(r) for r in page],
        next_cursor=next_cursor,
    )


@router.get("/{saga_id}", response_model=SagaDetailResponse)
def get_saga_handler(
    saga_id: Annotated[str, Field(max_length=64)],
    repo: PgSagaRepository = Depends(get_saga_repository),
) -> SagaDetailResponse:
    """Return the current status and step details of a saga."""
    summary = repo.get_summary(saga_id)
    if summary is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Saga not found",
        )
    return _to_detail(summary)

