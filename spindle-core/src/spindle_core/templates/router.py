# Component: Spindle Architecture
# File: router.py
# Description: Source code module for the Spindle workflow orchestration platform.

"""Templates API router.

Provides:
  GET /templates — list all registered saga templates and their steps
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from spindle_core.api.dependencies import get_template_store
from spindle_core.auth.dependencies import get_current_user
from spindle_core.templates.loader import TemplateStore

router = APIRouter(
    prefix="/templates",
    tags=["templates"],
    dependencies=[Depends(get_current_user)],
)


class TemplateStepResponse(BaseModel):
    """Step reference within a template response."""

    id: str
    type: str


class TemplateResponse(BaseModel):
    """Template summary with ordered step definitions."""

    name: str
    version: int
    steps: list[TemplateStepResponse]


@router.get("", response_model=list[TemplateResponse])
def list_templates_handler(
    template_store: TemplateStore = Depends(get_template_store),
) -> list[TemplateResponse]:
    """Return all registered saga templates ordered by name and version."""
    templates = template_store.all()
    return [
        TemplateResponse(
            name=t.name,
            version=t.version,
            steps=[TemplateStepResponse(id=s.id, type=s.type) for s in t.steps],
        )
        for t in templates
    ]
