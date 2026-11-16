import urllib.error
import pytest

from spindle_core.callspec.placeholders import (
    DisallowedPlaceholderError,
    MissingValueError,
    Placeholder,
    PlaceholderSyntaxError,
    RenderContext,
    RenderLimitError,
    UnsafeValueError,
    UnsupportedValueError,
    _resolve_placeholder,
    check_references,
    find_placeholders,
    iter_placeholders,
    redact_value,
    render_body,
    render_headers,
    render_url,
    render_value,
)
from spindle_core.callspec.retry import is_retryable_exception
from spindle_core.callspec.url_rules import check_url_template


def test_f1_anchoring_trailing_newlines_rejected() -> None:
    # Trailing newline characters in segments and header names must be rejected.
    with pytest.raises(PlaceholderSyntaxError):
        find_placeholders("{{input.a\n.b}}")

    with pytest.raises(PlaceholderSyntaxError, match="empty placeholder"):
        find_placeholders("{{}}")

    with pytest.raises(PlaceholderSyntaxError, match="empty segment"):
        find_placeholders("{{input..a}}")

    with pytest.raises(PlaceholderSyntaxError, match="unknown placeholder namespace"):
        find_placeholders("{{unknown.foo}}")

    phs = find_placeholders("{{saga.id}}{{saga.id}}")
    assert len(phs) == 2

    ctx = RenderContext(
        saga_id="sg-1",
        input={"a": 1},
        step_responses={},
        secrets={"K": "secret-value"},
    )
    with pytest.raises(PlaceholderSyntaxError):
        render_headers({"X-A\n": "v"}, ctx)

    headers, secrets = render_headers({"X": "{{secret.K}}"}, ctx)
    assert headers == {"X": "secret-value"}
    assert secrets == frozenset({"secret-value"})

    sec_ph = Placeholder("secret.K", "secret", ("K",), 0, 10)
    with pytest.raises(DisallowedPlaceholderError):
        _resolve_placeholder(sec_ph, ctx, allow_secrets=False)
    with pytest.raises(DisallowedPlaceholderError):
        _resolve_placeholder(sec_ph, ctx)
    with pytest.raises(MissingValueError):
        _resolve_placeholder(
            Placeholder("secret.MISSING", "secret", ("MISSING",), 0, 16),
            ctx,
            allow_secrets=True,
        )


def test_f2_depth_limit() -> None:
    # Nesting depth is capped at 32 levels to prevent recursion failure.
    ctx = RenderContext(
        saga_id="sg-1",
        input={"a": 1},
        step_responses={},
        secrets={},
    )
    d32: object = "{{saga.id}}"
    for _ in range(32):
        d32 = {"k": d32}

    rendered = render_value(d32, ctx)
    curr: object = rendered
    for _ in range(32):
        assert isinstance(curr, dict)
        curr = curr["k"]
    assert curr == "sg-1"
    assert len(list(iter_placeholders(d32))) == 1

    d33 = {"k": d32}
    with pytest.raises(RenderLimitError, match="nesting too deep"):
        render_value(d33, ctx)

    with pytest.raises(RenderLimitError, match="nesting too deep"):
        render_body(d33, ctx)

    with pytest.raises(RenderLimitError, match="nesting too deep"):
        list(iter_placeholders(d33))

    d32_empty: object = 1
    for _ in range(32):
        d32_empty = {"k": d32_empty}
    assert check_references(d32_empty, allow_secrets=False) == []

    p33 = check_references(d33, allow_secrets=False)
    assert any("too deeply nested" in p for p in p33)

    d3000: object = "{{saga.id}}"
    for _ in range(3000):
        d3000 = {"k": d3000}
    p3000 = check_references(d3000, allow_secrets=False)
    assert any("too deeply nested" in p for p in p3000)

    lst32: object = "{{saga.id}}"
    for _ in range(32):
        lst32 = [lst32]
    curr_l = render_value(lst32, ctx)
    for _ in range(32):
        assert isinstance(curr_l, list)
        curr_l = curr_l[0]
    assert curr_l == "sg-1"
    assert len(list(iter_placeholders(lst32))) == 1

    lst32_empty: object = 1
    for _ in range(32):
        lst32_empty = [lst32_empty]
    assert check_references(lst32_empty, allow_secrets=False) == []

    lst33 = [lst32]
    with pytest.raises(RenderLimitError, match="nesting too deep"):
        render_value(lst33, ctx)

    with pytest.raises(RenderLimitError, match="nesting too deep"):
        list(iter_placeholders(lst33))

    p33_lst = check_references(lst33, allow_secrets=False)
    assert any("too deeply nested" in p for p in p33_lst)


def test_f3_header_type_enforcement() -> None:
    # Header values must be strings and report the header name on error.
    ctx = RenderContext(saga_id="sg-1", input={}, step_responses={}, secrets={})

    for bad_val in (3, None, ["a"], True):
        with pytest.raises(UnsupportedValueError) as exc_info:
            render_headers({"X": bad_val}, ctx)
        assert "X" in str(exc_info.value)
        assert "3" not in str(exc_info.value)

    with pytest.raises(PlaceholderSyntaxError):
        render_headers({3: "v"}, ctx)


def test_f4_dot_segment_rejection() -> None:
    # URL path segments that resolve to '.' or '..' are rejected.
    ctx = RenderContext(
        saga_id="sg-1",
        input={"dot": ".", "dots": "..", "ab": "a..b", "dx": "..x"},
        step_responses={},
        secrets={},
    )
    with pytest.raises(UnsafeValueError, match="rendered URL path contains a dot segment"):
        render_url("http://h/x/{{input.dots}}/y", ctx)

    with pytest.raises(UnsafeValueError, match="rendered URL path contains a dot segment"):
        render_url("http://h/x/{{input.dot}}/y", ctx)

    with pytest.raises(UnsafeValueError, match="rendered URL path contains a dot segment"):
        render_url("http://h/x/.{{input.dot}}", ctx)

    with pytest.raises(UnsafeValueError, match="rendered URL path contains a dot segment"):
        render_url("http://h/a/../b", ctx)

    assert render_url("http://h/x/{{input.ab}}", ctx) == "http://h/x/a..b"
    assert render_url("http://h/x/{{input.dx}}", ctx) == "http://h/x/..x"
    assert render_url("http://h/x?q={{input.dots}}", ctx) == "http://h/x?q=.."


def test_f5_redact_value() -> None:
    # Secrets are redacted from string keys and values without mutating the input.
    inp: dict[object, object] = {
        "k": 'p"q',
        "n": 1,
        "l": ["xx secret yy", {"z": "secret"}],
        "secret": "v",
    }
    out = redact_value(inp, ["secret", 'p"q', ""])
    assert out == {
        "k": "***",
        "n": 1,
        "l": ["xx *** yy", {"z": "***"}],
        "***": "v",
    }
    assert inp == {
        "k": 'p"q',
        "n": 1,
        "l": ["xx secret yy", {"z": "secret"}],
        "secret": "v",
    }

    d33: object = "val"
    for _ in range(33):
        d33 = {"k": d33}
    with pytest.raises(RenderLimitError, match="nesting too deep"):
        redact_value(d33, ["secret"])

    gen = (s for s in ["secret"])
    assert redact_value({"a": "secret", "b": "secret"}, gen) == {"a": "***", "b": "***"}

    lst33: object = "val"
    for _ in range(33):
        lst33 = [lst33]
    with pytest.raises(RenderLimitError, match="nesting too deep"):
        redact_value(lst33, ["secret"])

    tup33: object = "val"
    for _ in range(33):
        tup33 = (tup33,)
    with pytest.raises(RenderLimitError, match="nesting too deep"):
        redact_value(tup33, ["secret"])

    d32: object = "val"
    for _ in range(32):
        d32 = {"k": d32}
    assert redact_value(d32, ["secret"]) == d32

    lst32: object = "val"
    for _ in range(32):
        lst32 = [lst32]
    assert redact_value(lst32, ["secret"]) == lst32

    tup32: object = "val"
    for _ in range(32):
        tup32 = (tup32,)
    assert redact_value(tup32, ["secret"]) == tup32

    assert redact_value(("hello secret",), ["secret"]) == ("hello ***",)


def test_f6_retry_hop_limit() -> None:
    # URLError unwrap chains stop at 5 hops to prevent cycle hangs.
    self_ref = urllib.error.URLError("x")
    self_ref.reason = self_ref
    assert is_retryable_exception(self_ref) is False

    curr: BaseException = TimeoutError()
    for _ in range(5):
        curr = urllib.error.URLError(curr)
    assert is_retryable_exception(curr) is True

    curr6 = urllib.error.URLError(curr)
    assert is_retryable_exception(curr6) is False


def test_f7_host_ascii_validation() -> None:
    # Hostnames must be plain ASCII without non-punycode international characters.
    p1 = check_url_template("http://①②⑦.0.0.1/x")
    assert any("ASCII" in p for p in p1)

    p2 = check_url_template("http://exämple.com/x")
    assert any("ASCII" in p for p in p2)

    assert check_url_template("http://xn--bcher-kva.example/x") == []


def test_interpolation_messages_and_numbers() -> None:
    # Descriptive interpolation error messages and JSON number formatting.
    ctx = RenderContext(
        saga_id="sg-1",
        input={"null_val": None, "obj": {"x": 1}, "huge": 1e20, "dec": 1.5},
        step_responses={},
        secrets={},
    )
    with pytest.raises(UnsupportedValueError) as exc_null:
        render_value("prefix {{input.null_val}}", ctx)
    assert "null" in str(exc_null.value)

    with pytest.raises(UnsupportedValueError) as exc_obj:
        render_value("prefix {{input.obj}}", ctx)
    assert "whole value" in str(exc_obj.value)

    assert render_value("val {{input.huge}}", ctx) == "val 1e+20"
    assert render_value("val {{input.dec}}", ctx) == "val 1.5"
