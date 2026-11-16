# Component: Spindle Architecture
# File: errors.py
# Description: Error types for the user-defined saga schema layer.

"""Schema-layer error types.

Two tiers, matching the two failure modes of the validator:

* ``FieldError`` — a single problem at a specific field path, e.g.
  ``steps[2].compensation``.  Always carries a human-readable message.
* ``ValidationError`` — the aggregate raised by ``validate_*`` when one or
  more ``FieldError``s are collected.  The validator never stops at the first
  problem; it accumulates every violation and raises once.

These are plain data objects (frozen dataclasses) so they are safe to serialise
into an API error response and safe to pass around without copying.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldError:
    """A single validation failure, pinned to a field path.

    ``path`` uses bracket notation for list indices, e.g. ``steps[2].compensation``,
    matching the dotted-path convention used throughout this schema layer.
    """

    path: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        return f"{self.path}: {self.message}"


class ValidationError(ValueError):
    """Raised by ``validate_*`` when one or more ``FieldError``s are collected.

    The validator accumulates *all* violations before raising, so callers can
    surface every problem in one response instead of iterating round-trips.
    """

    def __init__(self, errors: list[FieldError]) -> None:
        if not errors:
            raise ValueError("ValidationError requires at least one FieldError")
        # Deduplicate while preserving order.
        seen: set[tuple[str, str]] = set()
        unique: list[FieldError] = []
        for err in errors:
            key = (err.path, err.message)
            if key not in seen:
                seen.add(key)
                unique.append(err)
        self.errors: tuple[FieldError, ...] = tuple(unique)
        count = len(self.errors)
        summary = "; ".join(str(e) for e in self.errors)
        super().__init__(f"{count} validation error(s): {summary}")

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        return self.args[0] if self.args else "validation failed"

    def __reduce__(self) -> tuple[type["ValidationError"], tuple[list[FieldError]]]:
        # Default pickle/copy behaviour reconstructs via __init__, which re-dedupes
        # and re-validates the error list — safe, but it also re-raises on an empty
        # list.  Rebuild explicitly from the stored tuple so copy/deepcopy/pickle
        # never hit the constructor's guard and never lose the original errors.
        return (type(self), (list(self.errors),))