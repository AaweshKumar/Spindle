from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile


def test_api_contract_matches_generator() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    doc_path = repo_root / "docs" / "api-contract.md"
    script_path = repo_root / "scripts" / "gen_api_contract.py"

    committed_text = doc_path.read_text(encoding="utf-8")

    with tempfile.NamedTemporaryFile("r+", encoding="utf-8", suffix=".md") as tmp:
        res = subprocess.run(
            [sys.executable, str(script_path), tmp.name],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, f"Generator script failed: {res.stderr}"
        generated_text = Path(tmp.name).read_text(encoding="utf-8")

    assert committed_text == generated_text, (
        "docs/api-contract.md does not match scripts/gen_api_contract.py output. "
        "Regenerate with scripts/gen_api_contract.py."
    )
