# Component: Spindle Architecture
# File: validation.py
# Description: Schema validation API router (dry-run, no side effects).

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from spindle_core.api.dependencies import (
    get_service_allowed_hosts,
    get_service_repository,
)
from spindle_core.api.errors import enforce_body_size
from spindle_core.auth.dependencies import get_current_user
from spindle_core.persistence.service_repository import ServiceRepository
from spindle_core.schema.errors import ValidationError
from spindle_core.schema.validator import validate_saga, validate_service

router = APIRouter(
    prefix="/validate",
    tags=["validation"],
    dependencies=[Depends(get_current_user), Depends(enforce_body_size)],
)


class ValidationResult(BaseModel):
    valid: bool
    errors: list[dict[str, str]]


@router.post("/service", response_model=ValidationResult)
def validate_service_endpoint(
    body: dict[str, Any],
    allowed_hosts: frozenset[str] | None = Depends(get_service_allowed_hosts),
) -> ValidationResult:
    try:
        validate_service(body, allowed_hosts=allowed_hosts)
        return ValidationResult(valid=True, errors=[])
    except ValidationError as exc:
        errs = [{"path": e.path, "message": e.message} for e in exc.errors]
        return ValidationResult(valid=False, errors=errs)


@router.post("/workflow", response_model=ValidationResult)
def validate_workflow_endpoint(
    body: dict[str, Any],
    service_repo: ServiceRepository = Depends(get_service_repository),
) -> ValidationResult:
    known_services = service_repo.names()
    try:
        validate_saga(body, known_services=known_services)
        return ValidationResult(valid=True, errors=[])
    except ValidationError as exc:
        errs = [{"path": e.path, "message": e.message} for e in exc.errors]
        return ValidationResult(valid=False, errors=errs)
