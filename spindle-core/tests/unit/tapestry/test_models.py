"""
Unit tests for tapestry/models.py.
Pure dataclass model — no mocking required.
"""

from spindle_core.tapestry.models import (
    CompensationConfig,
    RequestConfig,
    StepConfig,
    SuccessCheck,
)


# ---- SuccessCheck ----

def test_success_check_defaults_to_2xx():
    check = SuccessCheck()
    assert 200 in check.ok_status_codes
    assert 299 in check.ok_status_codes
    assert 300 not in check.ok_status_codes
    assert 199 not in check.ok_status_codes


def test_success_check_body_field_defaults_to_none():
    check = SuccessCheck()
    assert check.body_field is None
    assert check.body_field_value is None


def test_success_check_custom_status_codes():
    check = SuccessCheck(ok_status_codes=frozenset({201, 204}))
    assert 201 in check.ok_status_codes
    assert 200 not in check.ok_status_codes


# ---- RequestConfig ----

def test_request_config_requires_url():
    cfg = RequestConfig(url="https://example.com")
    assert cfg.url == "https://example.com"


def test_request_config_default_method_is_post():
    cfg = RequestConfig(url="https://example.com")
    assert cfg.method == "POST"


def test_request_config_empty_mapping_and_headers_by_default():
    cfg = RequestConfig(url="https://example.com")
    assert cfg.field_mapping == {}
    assert cfg.headers == {}


def test_request_config_default_timeout():
    cfg = RequestConfig(url="https://example.com")
    assert cfg.timeout_seconds == 30.0


def test_request_config_field_mapping():
    cfg = RequestConfig(url="http://x", field_mapping={"order_id": "saga_id"})
    assert cfg.field_mapping == {"order_id": "saga_id"}


# ---- CompensationConfig ----

def test_compensation_manual_review():
    comp = CompensationConfig(requires_manual_review=True)
    assert comp.requires_manual_review is True
    assert comp.request is None
    assert comp.success_check is None


def test_compensation_automatic_with_request():
    req = RequestConfig(url="http://svc/release")
    comp = CompensationConfig(requires_manual_review=False, request=req)
    assert comp.requires_manual_review is False
    assert comp.request is req


# ---- StepConfig ----

def test_step_config_no_compensation_by_default():
    cfg = StepConfig(
        step_type="Foo",
        execute=RequestConfig(url="http://foo"),
    )
    assert cfg.compensation is None


def test_step_config_execute_success_check_defaults_to_2xx():
    cfg = StepConfig(step_type="Foo", execute=RequestConfig(url="http://foo"))
    assert 200 in cfg.execute_success_check.ok_status_codes


def test_step_config_with_compensation():
    cfg = StepConfig(
        step_type="Reserve",
        execute=RequestConfig(url="http://svc/reserve"),
        compensation=CompensationConfig(requires_manual_review=True),
    )
    assert cfg.compensation is not None
    assert cfg.compensation.requires_manual_review is True
