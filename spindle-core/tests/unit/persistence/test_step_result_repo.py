import pytest
from unittest.mock import MagicMock

from spindle_core.persistence.step_result_repository import (
    ResponseTooLarge,
    StepResultRepository,
)


def test_put_invalid_kind():
    repo = StepResultRepository(MagicMock())
    with pytest.raises(ValueError, match="invalid kind"):
        repo.put("s1", "step1", "invalid", {"status": 200})


def test_put_oversize_response_raises_response_too_large():
    repo = StepResultRepository(MagicMock())
    large_payload = {"key": "x" * 70000}
    with pytest.raises(ResponseTooLarge) as exc_info:
        repo.put("s1", "step1", "execute", large_payload)

    err_msg = str(exc_info.value)
    assert "exceeds limit" in err_msg
    assert "65536" in err_msg
    # Message must never leak content
    assert "x" * 50 not in err_msg


def test_put_non_serializable_response():
    repo = StepResultRepository(MagicMock())
    with pytest.raises(ValueError, match="JSON-serializable"):
        repo.put("s1", "step1", "execute", object())
