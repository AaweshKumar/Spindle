# Component: Spindle Architecture
# File: hashing.py
# Description: Canonical JSON content hashing for persistence entities.

"""Content hashing for services and templates.

Computes a deterministic SHA-256 hash over canonical JSON representation of
Pydantic model dumps (including default field values).
"""

from __future__ import annotations

import hashlib
import json
from pydantic import BaseModel


def compute_content_hash(model: BaseModel) -> tuple[str, dict]:
    """Compute sha256 hex digest of model dump and return (hash, dump_dict)."""
    dump = model.model_dump(mode="json")
    canonical = json.dumps(dump, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return content_hash, dump
