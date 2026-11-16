# Component: Spindle Architecture
# File: __init__.py
# Description: Source code module for the Spindle user-defined saga schema layer.

"""Public surface of the schema layer.

Re-exports the models, error types, and validators so callers can import from
``spindle_core.schema`` directly.

Import boundary (enforced by ``tests/test_import_boundaries.py``): modules under
``schema/`` may import only the Python standard library, ``pydantic``, ``yaml``,
other ``spindle_core.schema.*`` modules, and ``spindle_core.callspec.*``.  There
is no ``server/``, ``worker/`` or ``cli/`` package in this repo, so the old
blocklist was vacuous; the allowlist is the real contract.
"""

from spindle_core.schema.errors import FieldError, ValidationError
from spindle_core.schema.models import (
    Action,
    AuthReference,
    Compensation,
    RetryPolicy,
    SagaDefinition,
    ServiceDefinition,
    Step,
    SuccessCriteria,
)
from spindle_core.schema.validator import (
    parse_saga,
    parse_service,
    service_names,
    validate_saga,
    validate_service,
    validate_services,
)

__all__ = [
    "Action",
    "AuthReference",
    "Compensation",
    "FieldError",
    "RetryPolicy",
    "SagaDefinition",
    "ServiceDefinition",
    "Step",
    "SuccessCriteria",
    "ValidationError",
    "parse_saga",
    "parse_service",
    "service_names",
    "validate_saga",
    "validate_service",
    "validate_services",
]