# Component: Spindle Architecture
# File: errors.py
# Description: Error handlers and shared response helpers for authoring API.

from __future__ import annotations

from typing import Any
from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import (
    request_validation_exception_handler as default_request_validation_error_handler,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from spindle_core.schema.errors import ValidationError

MAX_BODY_SIZE = 262144
AUTHORING_PREFIXES = ("/services", "/workflows", "/validate")


def format_loc(loc: tuple[int | str, ...] | list[int | str]) -> str:
    """Format FastAPI error loc into bracket notation without leading source."""
    parts = list(loc)
    if parts and parts[0] in ("body", "query", "path", "header", "cookie"):
        parts = parts[1:]
    if not parts:
        return ""
    result = ""
    for part in parts:
        if isinstance(part, int):
            result += f"[{part}]"
        else:
            if result and not result.endswith("]"):
                result += f".{part}"
            elif result and result.endswith("]"):
                result += f".{part}"
            else:
                result = str(part)
    return result


async def request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """422 handler for authoring routes: never echoes input, ctx, or url."""
    if request.url.path.startswith(AUTHORING_PREFIXES):
        errors: list[dict[str, str]] = []
        for err in exc.errors():
            if err.get("type") == "json_invalid":
                errors.append({"path": "", "message": "invalid JSON"})
                continue
            loc = err.get("loc", ())
            msg = err.get("msg", "")
            if msg == "Extra inputs are not permitted":
                msg = "Extra fields are not permitted"
            errors.append({"path": format_loc(loc), "message": msg})
        return JSONResponse(
            status_code=422,
            content={"detail": "validation failed", "errors": errors},
        )
    return await default_request_validation_error_handler(request, exc)


async def schema_validation_error_handler(
    request: Request, exc: ValidationError
) -> JSONResponse:
    """Transform schema.ValidationError into the authoring 422 shape."""
    errors = []
    for e in exc.errors:
        msg = e.message
        if msg == "Extra inputs are not permitted":
            msg = "Extra fields are not permitted"
        errors.append({"path": e.path, "message": msg})
    return JSONResponse(
        status_code=422,
        content={"detail": "validation failed", "errors": errors},
    )


def enforce_body_size(request: Request) -> None:
    """Reject requests with Content-Length > 262144 bytes with 413."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_BODY_SIZE:
                raise HTTPException(status_code=413, detail="Request entity too large")
        except ValueError:
            pass
