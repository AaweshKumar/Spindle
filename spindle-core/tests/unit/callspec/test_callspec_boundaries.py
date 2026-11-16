import dataclasses
import pytest

from spindle_core.callspec.placeholders import (
    MAX_BODY_BYTES,
    MAX_DEPTH,
    MAX_HEADER_VALUE_CHARS,
    MAX_URL_CHARS,
    DisallowedPlaceholderError,
    MissingValueError,
    Placeholder,
    RenderContext,
    RenderLimitError,
    UnsupportedValueError,
    render_body,
    render_headers,
    render_url,
    render_value,
)
from spindle_core.callspec.retry import RetryPolicy
from spindle_core.callspec.url_rules import check_url_template


def test_boundary_url_length() -> None:
    # URL rendering and checking boundaries at exactly MAX_URL_CHARS.
    assert MAX_URL_CHARS == 2_048
    assert MAX_DEPTH == 32
    prefix = "http://h/"
    exact_len = MAX_URL_CHARS - len(prefix)
    ctx_exact = RenderContext(
        saga_id="sg-1",
        input={"x": "a" * exact_len},
        step_responses={},
        secrets={},
    )
    url_rendered = render_url(f"{prefix}{{{{input.x}}}}", ctx_exact)
    assert len(url_rendered) == MAX_URL_CHARS

    ctx_over = RenderContext(
        saga_id="sg-1",
        input={"x": "a" * (exact_len + 1)},
        step_responses={},
        secrets={},
    )
    with pytest.raises(RenderLimitError):
        render_url(f"{prefix}{{{{input.x}}}}", ctx_over)

    template_exact = prefix + ("a" * exact_len)
    assert len(template_exact) == MAX_URL_CHARS
    assert check_url_template(template_exact) == []

    template_over = template_exact + "a"
    problems = check_url_template(template_over)
    assert any("maximum length" in p for p in problems)


def test_boundary_header_length() -> None:
    # Header values up to MAX_HEADER_VALUE_CHARS are accepted; +1 raises RenderLimitError.
    assert MAX_HEADER_VALUE_CHARS == 4_096
    ctx = RenderContext(saga_id="sg-1", input={}, step_responses={}, secrets={})
    h_exact, _ = render_headers({"X": "a" * MAX_HEADER_VALUE_CHARS}, ctx)
    assert len(h_exact["X"]) == MAX_HEADER_VALUE_CHARS

    with pytest.raises(RenderLimitError):
        render_headers({"X": "a" * (MAX_HEADER_VALUE_CHARS + 1)}, ctx)


def test_boundary_body_size() -> None:
    # Body serialized size up to MAX_BODY_BYTES is accepted; +1 raises RenderLimitError.
    assert MAX_BODY_BYTES == 262_144
    ctx = RenderContext(saga_id="sg-1", input={}, step_responses={}, secrets={})
    n = MAX_BODY_BYTES - len('{"a":""}')
    body_exact = render_body({"a": "x" * n}, ctx)
    assert len(body_exact) == MAX_BODY_BYTES

    with pytest.raises(RenderLimitError):
        render_body({"a": "x" * (n + 1)}, ctx)


def test_boundary_control_characters_in_url() -> None:
    # Whitespace and ASCII control characters in URL templates are detected.
    p1 = check_url_template("http://h/a\x1fb")
    assert any("whitespace" in p or "control" in p for p in p1)

    p2 = check_url_template("http://h/a\x7fb")
    assert any("whitespace" in p or "control" in p for p in p2)

    p3 = check_url_template("http://h/a b")
    assert any("whitespace" in p or "control" in p for p in p3)


def test_boundary_ports() -> None:
    # Port range 1..65535 is accepted; 0 and 65536 are rejected.
    assert check_url_template("http://h:1/x") == []
    assert check_url_template("http://h:65535/x") == []

    p0 = check_url_template("http://h:0/x")
    assert any("invalid port" in p for p in p0)

    p65536 = check_url_template("http://h:65536/x")
    assert any("invalid port" in p for p in p65536)

    assert any("credentials" in p for p in check_url_template("://user@host/x"))
    assert any("credentials" in p for p in check_url_template("http://@evil.com/x"))


def test_boundary_retry_policy_bounds() -> None:
    # Parameter boundary and type validation for RetryPolicy configurations.
    assert RetryPolicy(max_attempts=1).max_attempts == 1
    assert RetryPolicy(max_attempts=10).max_attempts == 10
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=11)
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=True)

    assert RetryPolicy(initial_backoff_seconds=0.001, max_backoff_seconds=100.0)
    assert RetryPolicy(initial_backoff_seconds=60.0, max_backoff_seconds=100.0)
    with pytest.raises(ValueError):
        RetryPolicy(initial_backoff_seconds=61.0, max_backoff_seconds=100.0)
    with pytest.raises(ValueError):
        RetryPolicy(initial_backoff_seconds=0.0, max_backoff_seconds=100.0)
    with pytest.raises(ValueError):
        RetryPolicy(initial_backoff_seconds=True)
    with pytest.raises(ValueError):
        RetryPolicy(initial_backoff_seconds="0.5")

    assert RetryPolicy(backoff_multiplier=1.0).backoff_multiplier == 1.0
    assert RetryPolicy(backoff_multiplier=10.0).backoff_multiplier == 10.0
    with pytest.raises(ValueError):
        RetryPolicy(backoff_multiplier=0.99)
    with pytest.raises(ValueError):
        RetryPolicy(backoff_multiplier=10.01)

    assert RetryPolicy(initial_backoff_seconds=5.0, max_backoff_seconds=5.0)
    assert RetryPolicy(initial_backoff_seconds=1.0, max_backoff_seconds=300.0)
    with pytest.raises(ValueError):
        RetryPolicy(initial_backoff_seconds=5.0, max_backoff_seconds=4.99)
    with pytest.raises(ValueError):
        RetryPolicy(initial_backoff_seconds=1.0, max_backoff_seconds=301.0)


def test_boundary_budget_calculations() -> None:
    # Worst-case timeout budgets and parameter type checks.
    p = RetryPolicy(max_attempts=1)
    assert p.fits_budget(120.0) is True
    assert p.fits_budget(120.001) is False
    assert p.worst_case_seconds(0.5) > 0
    assert p.fits_budget(0.5) is True

    with pytest.raises(ValueError):
        p.worst_case_seconds(True)
    with pytest.raises(ValueError):
        p.worst_case_seconds(0)

    p2 = RetryPolicy(max_attempts=2)
    with pytest.raises(ValueError):
        p2.delay_before_retry(True)
    with pytest.raises(ValueError):
        p2.delay_before_retry(1.0)


def test_boundary_defaults() -> None:
    # Default RetryPolicy configurations and geometric delay progressions.
    assert RetryPolicy().max_backoff_seconds == 10.0
    expected = [0.5, 1.0, 2.0, 4.0, 8.0, 10.0, 10.0, 10.0, 10.0]
    assert RetryPolicy(max_attempts=10).delays() == expected


def test_boundary_immutability() -> None:
    # Frozen dataclasses cannot be mutated after instantiation.
    ph = Placeholder(raw="saga.id", namespace="saga", path=("id",), start=0, end=9)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ph.raw = "other"  # type: ignore[misc]

    ctx = RenderContext(saga_id="sg-1", input={}, step_responses={}, secrets={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.saga_id = "sg-2"  # type: ignore[misc]

    rp = RetryPolicy()
    with pytest.raises(dataclasses.FrozenInstanceError):
        rp.max_attempts = 5  # type: ignore[misc]


def test_boundary_paths() -> None:
    # Path navigation semantics for input and step response namespaces.
    ctx = RenderContext(
        saga_id="sg-1",
        input={"lst": ["first", "last"], "scalar": 42},
        step_responses={"s": None, "s_ok": {"lst": ["first", "last"], "scalar": 42}},
        secrets={},
    )
    assert render_value("{{input.lst.0}}", ctx) == "first"
    assert render_value("{{input.lst.1}}", ctx) == "last"
    with pytest.raises(MissingValueError):
        render_value("{{input.lst.2}}", ctx)
    with pytest.raises(MissingValueError):
        render_value("{{input.lst.bad}}", ctx)
    with pytest.raises(MissingValueError):
        render_value("{{input.lst.-1}}", ctx)
    with pytest.raises(MissingValueError):
        render_value("{{input.scalar.child}}", ctx)

    assert render_value("{{steps.s_ok.response.lst.0}}", ctx) == "first"
    assert render_value("{{steps.s_ok.response.lst.1}}", ctx) == "last"
    with pytest.raises(MissingValueError):
        render_value("{{steps.s_ok.response.lst.2}}", ctx)
    with pytest.raises(MissingValueError):
        render_value("{{steps.s_ok.response.lst.bad}}", ctx)
    with pytest.raises(MissingValueError):
        render_value("{{steps.s_ok.response.lst.-1}}", ctx)
    with pytest.raises(MissingValueError):
        render_value("{{steps.s_ok.response.scalar.child}}", ctx)

    with pytest.raises(MissingValueError):
        render_value("{{steps.missing.response}}", ctx)
    with pytest.raises(MissingValueError, match="cannot traverse"):
        render_value("{{steps.s.response.extra}}", ctx)
    assert render_value("{{steps.s.response}}", ctx) is None

    ctx_types = RenderContext(
        saga_id="sg-1",
        input={"nan": float("nan"), "inf": float("inf"), "st": {1}},
        step_responses={},
        secrets={},
    )
    with pytest.raises(UnsupportedValueError, match="non-finite float"):
        render_value("val {{input.nan}}", ctx_types)
    with pytest.raises(UnsupportedValueError, match="non-finite float"):
        render_value("val {{input.inf}}", ctx_types)
    with pytest.raises(UnsupportedValueError, match="unsupported value type"):
        render_value("val {{input.st}}", ctx_types)


def test_boundary_placement() -> None:
    # Placeholders directly on the host or inside body secrets are forbidden.
    ctx = RenderContext(
        saga_id="sg-1",
        input={"x": "a"},
        step_responses={},
        secrets={"K": "secret-value"},
    )
    with pytest.raises(DisallowedPlaceholderError):
        render_url("http://h{{input.x}}", ctx)

    with pytest.raises(DisallowedPlaceholderError):
        render_url("http://{{input.x}}/path", ctx)

    with pytest.raises(DisallowedPlaceholderError):
        render_body({"a": "{{secret.K}}"}, ctx)

    with pytest.raises(DisallowedPlaceholderError):
        render_value("{{secret.K}}", ctx)

    with pytest.raises(DisallowedPlaceholderError):
        render_value("prefix {{secret.K}}", ctx)
