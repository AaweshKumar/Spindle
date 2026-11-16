# Component: Spindle Architecture
# File: test_import_boundaries.py
# Description: AST-based import boundary tests enforcing schema layer isolation.

"""Import boundary verification for spindle_core.schema.

Enforces an allowlist of permitted imports across all schema modules:
- Python standard library (via sys.stdlib_module_names)
- pydantic
- yaml
- spindle_core.schema.*
- spindle_core.callspec.*

Any other import fails naming the offending file and imported module.
"""

from __future__ import annotations

import ast
from pathlib import Path
import sys
import pytest


def _is_allowed_import(module_name: str) -> bool:
    root = module_name.split(".")[0]
    if root in sys.stdlib_module_names:
        return True
    if root in ("pydantic", "yaml"):
        return True
    if module_name.startswith("spindle_core.schema") or module_name.startswith(
        "spindle_core.callspec"
    ):
        return True
    return False


def _resolve_import_from(package: str, level: int, module: str | None) -> str:
    if level == 0:
        return module or ""
    pkg_parts = package.split(".")
    # level 1 is relative to current package, level 2 is parent, etc.
    cutoff = len(pkg_parts) - (level - 1)
    base = ".".join(pkg_parts[:cutoff])
    return f"{base}.{module}" if module else base


def test_schema_import_boundaries() -> None:
    # Discover all python files in spindle_core/schema
    schema_dir = Path(__file__).parent.parent.parent / "src" / "spindle_core" / "schema"
    py_files = sorted(schema_dir.glob("*.py"))
    assert py_files, f"No schema files found in {schema_dir}"

    package = "spindle_core.schema"
    violations: list[str] = []

    for file_path in py_files:
        tree = ast.parse(file_path.read_text(), filename=str(file_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if not _is_allowed_import(alias.name):
                        violations.append(
                            f"{file_path.name}: disallowed import '{alias.name}'"
                        )
            elif isinstance(node, ast.ImportFrom):
                resolved = _resolve_import_from(package, node.level, node.module)
                if not _is_allowed_import(resolved):
                    violations.append(
                        f"{file_path.name}: disallowed import '{resolved}'"
                    )

    assert not violations, "Import boundary violations found:\n" + "\n".join(violations)
