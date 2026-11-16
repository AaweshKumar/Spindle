# Component: Spindle Architecture
# File: errors.py
# Description: Persistence layer exception types.

"""Persistence-layer error types.

Defines repository and storage exception types. Messages may include entity
identifiers, names, and version numbers, but never definition payload contents.
"""

from __future__ import annotations


class PersistenceError(Exception):
    """Base exception for persistence operations."""


class VersionConflict(PersistenceError):
    """Raised when a template publish encounters a version conflict."""


class TemplateNotFound(PersistenceError):
    """Raised when a requested template is not found."""


class ServiceNotFound(PersistenceError):
    """Raised when a requested service is not found."""


class InvalidNameError(PersistenceError, ValueError):
    """Raised when an entity name does not match the required naming pattern."""
