"""
Unit tests for tapestry/loader.py (TapestryStore).
No mocking required — pure Python parsing.
"""

import pytest

from spindle_core.tapestry.loader import TapestryStore


# ---- fixture data ----

FULL_CONFIG = {
    "steps": [
        {
            "step_type": "ReserveInventory",
            "execute": {
                "url": "https://inventory.svc/reserve",
                "method": "POST",
                "field_mapping": {"order_id": "saga_id"},
                "timeout_seconds": 10,
            },
            "execute_success_check": {
                "body_field": "status",
                "body_field_value": "reserved",
            },
            "compensation": {
                "requires_manual_review": False,
                "request": {
                    "url": "https://inventory.svc/release",
                    "method": "POST",
                },
                "success_check": {
                    "body_field": "status",
                    "body_field_value": "released",
                },
            },
        },
        {
            "step_type": "ChargePayment",
            "execute": {"url": "https://payments.svc/charge"},
            "compensation": {"requires_manual_review": True},
        },
    ]
}


# ---- basic loading ----

def test_from_dict_discovers_all_step_types():
    store = TapestryStore.from_dict(FULL_CONFIG)
    assert set(store.all_step_types()) == {"ReserveInventory", "ChargePayment"}


def test_get_returns_none_for_unknown_step_type():
    store = TapestryStore.from_dict(FULL_CONFIG)
    assert store.get("DoesNotExist") is None


def test_empty_dict_produces_empty_store():
    store = TapestryStore.from_dict({})
    assert store.all_step_types() == []


def test_empty_steps_list_produces_empty_store():
    store = TapestryStore.from_dict({"steps": []})
    assert store.all_step_types() == []


# ---- execute config ----

def test_execute_url_parsed():
    store = TapestryStore.from_dict(FULL_CONFIG)
    cfg = store.get("ReserveInventory")
    assert cfg.execute.url == "https://inventory.svc/reserve"


def test_execute_method_parsed():
    store = TapestryStore.from_dict(FULL_CONFIG)
    cfg = store.get("ReserveInventory")
    assert cfg.execute.method == "POST"


def test_execute_field_mapping_parsed():
    store = TapestryStore.from_dict(FULL_CONFIG)
    cfg = store.get("ReserveInventory")
    assert cfg.execute.field_mapping == {"order_id": "saga_id"}


def test_execute_timeout_parsed():
    store = TapestryStore.from_dict(FULL_CONFIG)
    cfg = store.get("ReserveInventory")
    assert cfg.execute.timeout_seconds == 10.0


def test_execute_default_method_is_post():
    store = TapestryStore.from_dict(FULL_CONFIG)
    cfg = store.get("ChargePayment")
    assert cfg.execute.method == "POST"


# ---- execute success check ----

def test_execute_success_check_body_field_parsed():
    store = TapestryStore.from_dict(FULL_CONFIG)
    cfg = store.get("ReserveInventory")
    assert cfg.execute_success_check.body_field == "status"
    assert cfg.execute_success_check.body_field_value == "reserved"


def test_execute_success_check_defaults_to_2xx_when_omitted():
    store = TapestryStore.from_dict(FULL_CONFIG)
    cfg = store.get("ChargePayment")
    assert 200 in cfg.execute_success_check.ok_status_codes
    assert cfg.execute_success_check.body_field is None


# ---- compensation config ----

def test_automatic_compensation_parsed():
    store = TapestryStore.from_dict(FULL_CONFIG)
    cfg = store.get("ReserveInventory")
    comp = cfg.compensation
    assert comp is not None
    assert comp.requires_manual_review is False
    assert comp.request is not None
    assert comp.request.url == "https://inventory.svc/release"


def test_compensation_success_check_parsed():
    store = TapestryStore.from_dict(FULL_CONFIG)
    cfg = store.get("ReserveInventory")
    check = cfg.compensation.success_check
    assert check is not None
    assert check.body_field == "status"
    assert check.body_field_value == "released"


def test_manual_review_compensation_parsed():
    store = TapestryStore.from_dict(FULL_CONFIG)
    cfg = store.get("ChargePayment")
    assert cfg.compensation.requires_manual_review is True
    assert cfg.compensation.request is None


def test_no_compensation_results_in_none():
    config = {
        "steps": [{"step_type": "Foo", "execute": {"url": "http://foo"}}]
    }
    store = TapestryStore.from_dict(config)
    cfg = store.get("Foo")
    assert cfg.compensation is None


# ---- error cases ----

def test_missing_execute_block_raises():
    with pytest.raises((ValueError, KeyError)):
        TapestryStore.from_dict({"steps": [{"step_type": "Foo"}]})


def test_missing_url_in_execute_raises():
    with pytest.raises((ValueError, KeyError)):
        TapestryStore.from_dict({"steps": [{"step_type": "Foo", "execute": {}}]})


def test_compensation_with_no_request_and_no_manual_review_raises():
    with pytest.raises((ValueError, KeyError)):
        TapestryStore.from_dict({
            "steps": [{
                "step_type": "Foo",
                "execute": {"url": "http://foo"},
                "compensation": {"requires_manual_review": False},
            }]
        })


# ---- custom ok_status_codes ----

def test_custom_ok_status_codes_parsed():
    config = {
        "steps": [{
            "step_type": "Foo",
            "execute": {"url": "http://foo"},
            "execute_success_check": {"ok_status_codes": [201, 204]},
        }]
    }
    store = TapestryStore.from_dict(config)
    cfg = store.get("Foo")
    assert cfg.execute_success_check.ok_status_codes == frozenset({201, 204})
