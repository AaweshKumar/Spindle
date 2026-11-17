# Component: Spindle Architecture
# File: workflows.py
# Description: Workflows authoring API router.

from __future__ import annotations

from datetime import datetime
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError as PydanticValidationError

from spindle_core.api.dependencies import (
    get_service_repository,
    get_template_repository,
)
from spindle_core.api.errors import enforce_body_size, format_loc
from spindle_core.auth.dependencies import get_current_user
from spindle_core.auth.models import User
from spindle_core.persistence.constants import NAME_PATTERN, NAME_RE
from spindle_core.persistence.errors import TemplateNotFound, VersionConflict
from spindle_core.persistence.service_repository import ServiceRepository
from spindle_core.persistence.template_repository import TemplateRecord, TemplateRepository
from spindle_core.schema.errors import FieldError, ValidationError
from spindle_core.schema.inputs import missing_inputs
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


class WorkflowRunRequest(BaseModel):
    model_config = {"extra": "forbid"}

    version: Annotated[int | None, Field(default=None, ge=1)] = None
    input: Annotated[dict[str, Any] | None, Field(default=None)] = None


def prepare_run(
    record: TemplateRecord,
    input_data: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Prepare a workflow run snapshot and input payload for execution.

    Pure helper with no I/O beyond reads.
    Returns (definition_snapshot, copy-or-None).
    """
    snapshot = {
        "name": record.name,
        "version": record.version,
        "content_hash": record.content_hash,
        "definition": record.definition,
    }
    input_copy = dict(input_data) if input_data is not None else None
    return snapshot, input_copy


def _check_nesting_depth(val: Any, depth: int = 1) -> bool:
    if depth > 32:
        return False
    if isinstance(val, dict):
        return all(_check_nesting_depth(v, depth + 1) for v in val.values())
    if isinstance(val, (list, tuple)):
        return all(_check_nesting_depth(item, depth + 1) for item in val)
    return True


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


@router.post("/{name}/runs", status_code=status.HTTP_501_NOT_IMPLEMENTED)
def create_workflow_run(
    name: Annotated[str, Path(pattern=NAME_PATTERN, max_length=64)],
    body: dict[str, Any],
    template_repo: TemplateRepository = Depends(get_template_repository),
    service_repo: ServiceRepository = Depends(get_service_repository),
) -> None:
    """Validate workflow run inputs and preconditions, returning 501 until execution is enabled."""
    try:
        req = WorkflowRunRequest.model_validate(body)
    except PydanticValidationError as exc:
        pydantic_errors: list[FieldError] = []
        for err in exc.errors():
            loc = err.get("loc", ())
            msg = err.get("msg", "")
            if msg == "Extra inputs are not permitted":
                msg = "Extra fields are not permitted"
            pydantic_errors.append(FieldError(path=format_loc(loc), message=msg))
        raise ValidationError(pydantic_errors) from None

    input_errors: list[FieldError] = []
    if req.input is not None:
        try:
            if not _check_nesting_depth(req.input):
                input_errors.append(
                    FieldError(path="input", message="nesting too deep")
                )
        except RecursionError:
            input_errors.append(
                FieldError(path="input", message="nesting too deep")
            )

        if not input_errors:
            try:
                canonical_bytes = json.dumps(
                    req.input, separators=(",", ":"), sort_keys=True
                ).encode("utf-8")
                if len(canonical_bytes) > 65536:
                    input_errors.append(
                        FieldError(path="input", message="input too large")
                    )
            except (TypeError, ValueError):
                input_errors.append(
                    FieldError(path="input", message="must be an object")
                )
            except RecursionError:
                input_errors.append(
                    FieldError(path="input", message="nesting too deep")
                )

    if input_errors:
        raise ValidationError(input_errors)

    try:
        record = template_repo.get(name, version=req.version)
    except TemplateNotFound as exc:
        raise HTTPException(status_code=404, detail="Workflow not found") from exc

    known_services = service_repo.names()
    try:
        validated_saga = validate_saga(record.definition, known_services=known_services)
    except ValidationError as exc:
        steps = record.definition.get("steps", [])
        unregistered = sorted(
            {
                s["service"]
                for s in steps
                if isinstance(s, dict) and s.get("service") not in known_services
            }
        )
        if unregistered:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "detail": "workflow references unregistered services",
                    "services": unregistered,
                },
            )
        raise exc

    missing = missing_inputs(validated_saga, req.input or {})
    if missing:
        raise ValidationError(missing)

    snapshot, run_input = prepare_run(record, req.input)
    assert snapshot is not None  # ready for T6-4 execution wiring

    return JSONResponse(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        content={"detail": "workflow execution is not enabled yet"},
    )
