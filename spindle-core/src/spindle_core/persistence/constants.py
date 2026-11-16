# Component: Spindle Architecture
# File: constants.py
# Description: Persistence layer constants and validation patterns.

"""Persistence-layer constants.

Defines the single source of truth for repository and database constraints,
such as entity name formats.
"""

from __future__ import annotations

import re

NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
