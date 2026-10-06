# Component: Spindle Architecture
# File: loader.py
# Description: Parses raw config dicts into TapestryStore.
#   The storage backend (DB table, YAML file, dict literal, …) is a concern
#   for the layer above — this module only requires a parsed list of raw
#   config dicts conforming to the wire format documented on TapestryStore.from_dict.
#
#   No imports from connectors/ or workers/.

from __future__ import annotations

from .models import CompensationConfig, RequestConfig, StepConfig, SuccessCheck


# ---------------------------------------------------------------------------
# Internal parsers (not part of the public API)
# ---------------------------------------------------------------------------

def _parse_success_check(raw: dict) -> SuccessCheck:
    ok_codes_raw = raw.get("ok_status_codes")
    ok_codes = (
        frozenset(ok_codes_raw)
        if ok_codes_raw is not None
        else frozenset(range(200, 300))
    )
    return SuccessCheck(
        ok_status_codes=ok_codes,
        body_field=raw.get("body_field"),
        body_field_value=raw.get("body_field_value"),
    )


def _parse_request_config(raw: dict) -> RequestConfig:
    return RequestConfig(
        url=raw["url"],  # required — KeyError on missing
        method=raw.get("method", "POST"),
        field_mapping=dict(raw.get("field_mapping") or {}),
        headers=dict(raw.get("headers") or {}),
        timeout_seconds=float(raw.get("timeout_seconds", 30.0)),
    )


def _parse_compensation_config(raw: dict) -> CompensationConfig:
    requires_manual_review = bool(raw.get("requires_manual_review", False))
    if requires_manual_review:
        return CompensationConfig(
            requires_manual_review=True,
            request=None,
            success_check=None,
        )
    request_raw = raw.get("request")
    if request_raw is None:
        raise ValueError(
            "compensation config must have either requires_manual_review=true "
            "or a 'request' block"
        )
    success_check_raw = raw.get("success_check")
    return CompensationConfig(
        requires_manual_review=False,
        request=_parse_request_config(request_raw),
        success_check=(
            _parse_success_check(success_check_raw) if success_check_raw else None
        ),
    )


def _parse_step_config(raw: dict) -> StepConfig:
    step_type = raw["step_type"]  # required
    execute_raw = raw.get("execute")
    if not execute_raw:
        raise ValueError(f"step_type {step_type!r} is missing the required 'execute' block")

    execute_success_check_raw = raw.get("execute_success_check")
    compensation_raw = raw.get("compensation")

    return StepConfig(
        step_type=step_type,
        execute=_parse_request_config(execute_raw),
        execute_success_check=(
            _parse_success_check(execute_success_check_raw)
            if execute_success_check_raw
            else SuccessCheck()
        ),
        compensation=(
            _parse_compensation_config(compensation_raw)
            if compensation_raw
            else None
        ),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class TapestryStore:
    """
    In-memory store of per-step Tapestry configuration.

    The storage backend (DB row, YAML file, dict literal, …) is a concern
    for the caller — this class only requires a list of StepConfig objects or
    a raw dict in the wire format.

    Does not import from connectors/ or workers/.
    """

    def __init__(self, configs: list[StepConfig]) -> None:
        self._by_type: dict[str, StepConfig] = {c.step_type: c for c in configs}

    def get(self, step_type: str) -> StepConfig | None:
        """Return the StepConfig for *step_type*, or None if not registered."""
        return self._by_type.get(step_type)

    def all_step_types(self) -> list[str]:
        return list(self._by_type.keys())

    @classmethod
    def from_dict(cls, data: dict) -> "TapestryStore":
        """
        Build a TapestryStore from a plain dict in the canonical wire format.

        Wire format example::

            {
                "steps": [
                    {
                        "step_type": "ReserveInventory",
                        "execute": {
                            "url": "https://inventory.svc/reserve",
                            "method": "POST",
                            "field_mapping": {"order_id": "saga_id"},
                            "timeout_seconds": 10
                        },
                        "execute_success_check": {
                            "body_field": "status",
                            "body_field_value": "reserved"
                        },
                        "compensation": {
                            "requires_manual_review": false,
                            "request": {
                                "url": "https://inventory.svc/release",
                                "method": "POST"
                            }
                        }
                    },
                    {
                        "step_type": "ChargePayment",
                        "execute": {"url": "https://payments.svc/charge"},
                        "compensation": {"requires_manual_review": true}
                    }
                ]
            }

        Raises ValueError/KeyError on malformed entries.
        """
        steps_raw = data.get("steps") or []
        configs = [_parse_step_config(s) for s in steps_raw]
        return cls(configs)
