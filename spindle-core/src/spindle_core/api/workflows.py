# Component: Spindle Architecture
# File: workflows.py
# Description: Workflows authoring API router.

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from pydantic import BaseModel

from spindle_core.api.dependencies import (
    get_service_repository,
    get_template_repository,
)
from spindle_core.api.errors import enforce_body_size
from spindle_core.auth.dependencies import get_current_user
from spindle_core.auth.models import User
from spindle_core.persistence.constants import NAME_PATTERN, NAME_RE
from spindle_core.persistence.errors import TemplateNotFound, VersionConflict
from spindle_core.persistence.service_repository import ServiceRepository
from spindle_core.persistence.template_repository import TemplateRecord, TemplateRepository
from spindle_core.schema.errors import FieldError, ValidationError
from spindle_core.schema.validator import validate_saga

router = APIRouter(
    prefix="/workflows",
    tags=["workflows"],
    dependencies=[Depends(get_current_user), Depends(enforce_body_size)],
)


class WorkflowResponse(BaseModel):
    name: str
    version: int
    definition: dict[str, Any]
    content_hash: str
    format_version: int
    created_at: datetime
    created_by: str | None


def _to_workflow_response(record: TemplateRecord) -> WorkflowResponse:
    return WorkflowResponse(
        name=record.name,
        version=record.version,
        definition=record.definition,
        content_hash=record.content_hash,
        format_version=record.format_version,
        created_at=record.created_at,
        created_by=record.created_by,
    )


@router.post("/{name}/versions", response_model=WorkflowResponse)
def publish_workflow_version(
    name: Annotated[str, Path(pattern=NAME_PATTERN, max_length=64)],
    body: dict[str, Any],
    response: Response,
    base_version: Annotated[int | None, Query(ge=0)] = None,
    user: User = Depends(get_current_user),
    template_repo: TemplateRepository = Depends(get_template_repository),
    service_repo: ServiceRepository = Depends(get_service_repository),
) -> WorkflowResponse:
    errors: list[FieldError] = []

    body_name = body.get("name") if isinstance(body, dict) else None
    if body_name != name:
        errors.append(
            FieldError(path="name", message=f"body name must equal path name '{name}'")
        )
    elif not NAME_RE.fullmatch(body_name):
        errors.append(
            FieldError(path="name", message="name does not match required pattern")
        )

    known_services = service_repo.names()
    try:
        validated = validate_saga(body, known_services=known_services)
    except ValidationError as val_exc:
        errors.extend(val_exc.errors)
        validated = None

    if errors:
        raise ValidationError(errors)

    assert validated is not None

    try:
        record, created = template_repo.publish(
            validated,
            base_version=base_version,
            created_by=user.username,
        )
    except VersionConflict as exc:
        raise HTTPException(status_code=409, detail="Version conflict") from exc
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail="Failed to publish workflow template"
        ) from exc

    if created:
        response.status_code = status.HTTP_201_CREATED
    else:
        response.status_code = status.HTTP_200_OK
    return _to_workflow_response(record)


@router.get("", response_model=list[WorkflowResponse])
def list_workflows(
    template_repo: TemplateRepository = Depends(get_template_repository),
) -> list[WorkflowResponse]:
    records = template_repo.list_latest()
    return [_to_workflow_response(r) for r in records]


@router.get("/{name}", response_model=WorkflowResponse)
def get_workflow(
    name: Annotated[str, Path(pattern=NAME_PATTERN, max_length=64)],
    template_repo: TemplateRepository = Depends(get_template_repository),
) -> WorkflowResponse:
    try:
        record = template_repo.get(name)
    except TemplateNotFound as exc:
        raise HTTPException(status_code=404, detail="Workflow not found") from exc
    return _to_workflow_response(record)


@router.get("/{name}/versions", response_model=list[WorkflowResponse])
def list_workflow_versions(
    name: Annotated[str, Path(pattern=NAME_PATTERN, max_length=64)],
    template_repo: TemplateRepository = Depends(get_template_repository),
) -> list[WorkflowResponse]:
    try:
        records = template_repo.list_versions(name)
    except TemplateNotFound as exc:
        raise HTTPException(status_code=404, detail="Workflow not found") from exc
    if not records:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return [_to_workflow_response(r) for r in records]


@router.get("/{name}/versions/{version}", response_model=WorkflowResponse)
def get_workflow_version(
    name: Annotated[str, Path(pattern=NAME_PATTERN, max_length=64)],
    version: Annotated[int, Path(ge=1)],
    template_repo: TemplateRepository = Depends(get_template_repository),
) -> WorkflowResponse:
    try:
        record = template_repo.get(name, version=version)
    except TemplateNotFound as exc:
        raise HTTPException(status_code=404, detail="Workflow version not found") from exc
    return _to_workflow_response(record)
