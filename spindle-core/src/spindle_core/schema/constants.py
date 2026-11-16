# Component: Spindle Architecture
# File: constants.py
# Description: Shared constants for the user-defined saga schema layer.

"""Schema-layer constants.

Single source of truth for allowlisted values in the schema, so the models and
the validator cannot drift.  These describe *structure*, not
environment-specific configuration, so they live here rather than in Settings.

Note: there is deliberately no separate timeout-cap or retry-max constant.
Per-step timeout and retry bounds are enforced indirectly through
``callspec.retry.RetryPolicy.fits_budget``, which folds both into the single
``MAX_STEP_BUDGET_SECONDS`` step budget.  Keeping the two in one place means a
mis-set timeout or an over-eager retry policy is caught by the same rule.
"""

from __future__ import annotations

# Rule 7: the only HTTP methods a step action or compensation may use.
HTTP_METHODS: frozenset[str] = frozenset(
    {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
)

# Rule 5: the only template-reference roots a step may use. Anything else
# (e.g. ``saga.*``, ``secret.*``) is rejected as an unknown variable root.
ALLOWED_TEMPLATE_ROOTS: frozenset[str] = frozenset({"input", "steps"})

# Auth type allowlist.  "bearer" is the only supported mechanism today.
ALLOWED_AUTH_TYPES: frozenset[str] = frozenset({"bearer"})