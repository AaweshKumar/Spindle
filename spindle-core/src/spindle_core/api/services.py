# Component: Spindle Architecture
# File: services.py
# Description: Services authoring API router.

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from spindle_core.api.dependencies import (
    get_service_allowed_hosts,
    get_service_repository,
    get_template_repository,
)
from spindle_core.api.errors import enforce_body_size
from spindle_core.auth.dependencies import get_current_user
from spindle_core.persistence.constants import NAME_PATTERN
from spindle_core.persistence.errors import ServiceNotFound
from spindle_core.persistence.service_repository import ServiceRecord, ServiceRepository
from spindle_core.persistence.template_repository import TemplateRepository
from spindle_core.schema.errors import FieldError, ValidationError
from spindle_core.schema.validator import validate_service

router = APIRouter(
    prefix="/services",
    tags=["services"],
    dependencies=[Depends(get_current_user), Depends(enforce_body_size)],
)


class ServiceResponse(BaseModel):
    name: str
    definition: dict[str, Any]
    revision: int
    created_at: datetime
    updated_at: datetime


def _to_service_response(record: ServiceRecord) -> ServiceResponse:
    return ServiceResponse(
        name=record.name,
        definition=record.definition,
        revision=record.revision,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


@router.put("/{name}", response_model=ServiceResponse)
def upsert_service(
    name: Annotated[str, Path(pattern=NAME_PATTERN, max_length=64)],
    body: dict[str, Any],
    response: Response,
    service_repo: ServiceRepository = Depends(get_service_repository),
    allowed_hosts: frozenset[str] | None = Depends(get_service_allowed_hosts),
) -> ServiceResponse:
    if body.get("name") != name:
        raise ValidationError(
            [FieldError(path="name", message=f"body name must equal path name '{name}'")]
        )

    validated = validate_service(body, allowed_hosts=allowed_hosts)
    record, created = service_repo.upsert(validated)
    if created:
        response.status_code = status.HTTP_201_CREATED
    else:
        response.status_code = status.HTTP_200_OK
    return _to_service_response(record)


@router.get("", response_model=list[ServiceResponse])
def list_services(
    service_repo: ServiceRepository = Depends(get_service_repository),
) -> list[ServiceResponse]:
    records = service_repo.list()
    return [_to_service_response(r) for r in records]


@router.get("/{name}", response_model=ServiceResponse)
def get_service(
    name: Annotated[str, Path(pattern=NAME_PATTERN, max_length=64)],
    service_repo: ServiceRepository = Depends(get_service_repository),
) -> ServiceResponse:
    try:
        record = service_repo.get(name)
    except ServiceNotFound as exc:
        raise HTTPException(status_code=404, detail="Service not found") from exc
    return _to_service_response(record)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_service(
    name: Annotated[str, Path(pattern=NAME_PATTERN, max_length=64)],
    service_repo: ServiceRepository = Depends(get_service_repository),
    template_repo: TemplateRepository = Depends(get_template_repository),
) -> Response:
    try:
        service_repo.get(name)
    except ServiceNotFound as exc:
        raise HTTPException(status_code=404, detail="Service not found") from exc

    referencing = template_repo.names_referencing_service(name)
    if referencing:
        return JSONResponse(
            status_code=409,
            content={
                "detail": f"cannot delete service '{name}': referenced by existing workflows",
                "referenced_by": referencing[:50],
            },
        )

    try:
        service_repo.delete(name)
    except ServiceNotFound as exc:
        raise HTTPException(status_code=404, detail="Service not found") from exc

    return Response(status_code=status.HTTP_204_NO_CONTENT)
