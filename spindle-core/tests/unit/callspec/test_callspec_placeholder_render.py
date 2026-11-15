import copy
import pytest

from spindle_core.callspec.placeholders import (
    DisallowedPlaceholderError,
    MissingValueError,
    PlaceholderError,
    PlaceholderSyntaxError,
    RenderContext,
    RenderLimitError,
    UnsafeValueError,
    UnsupportedValueError,
    redact_text,
    render_body,
    render_headers,
    render_url,
    render_value,
)


@pytest.fixture
def sample_context() -> RenderContext:
    return RenderContext(
        saga_id="sg-1",
        input={
            "customer": {"id": 42, "name": "Ana"},
            "items": [{"sku": "A-1", "qty": 2}],
            "flag": True,
            "none": None,
            "ratio": 1.5,
            "text": "a b/c?d=e&f#g",
            "unicode": "é",
            "host": "evil.com",
            "port": 9999,
        },
        step_responses={
            "book-flight": {"booking_id": "BF-9", "legs": [{"id": 7}]},
            "null-step": None,
        },
        secrets={"PAYMENT_KEY": "s3cr3t-value", "OTHER": "tok"},
    )


@pytest.mark.parametrize(
    ("template", "expected_val", "expected_type"),
    [
        ("{{input.customer.id}}", 42, int),
        ("{{input.flag}}", True, bool),
        ("{{input.none}}", None, type(None)),
        ("{{input.ratio}}", 1.5, float),
        ("{{input.customer}}", {"id": 42, "name": "Ana"}, dict),
        ("{{input.items}}", [{"sku": "A-1", "qty": 2}], list),
        ("{{ saga.id }}", "sg-1", str),
        ("{{steps.null-step.response}}", None, type(None)),
    ],
)
def test_typed_whole_string_resolution(
    sample_context: RenderContext, template: str, expected_val, expected_type
):
    result = render_value(template, sample_context)
    assert result == expected_val
    assert type(result) is expected_type


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("id-{{input.customer.id}}", "id-42"),
        ("{{input.flag}}!", "true!"),
        ("r={{input.ratio}}", "r=1.5"),
        ("{{input.customer.name}}-{{saga.id}}", "Ana-sg-1"),
        ("leg-{{steps.book-flight.response.legs.0.id}}", "leg-7"),
    ],
)
def test_text_interpolation(sample_context: RenderContext, template: str, expected: str):
    assert render_value(template, sample_context) == expected


@pytest.mark.parametrize(
    "template",
    [
        "null-{{input.none}}",
        "x{{input.customer}}",
        "x{{input.items}}",
    ],
)
def test_interpolation_errors(sample_context: RenderContext, template: str):
    with pytest.raises(UnsupportedValueError):
        render_value(template, sample_context)


@pytest.mark.parametrize(
    "ref",
    [
        "input.nope",
        "input.customer.nope",
        "input.items.5",
        "input.items.x",
        "input.customer.id.deeper",
        "steps.unknown.response",
        "steps.book-flight.response.nope",
    ],
)
def test_missing_value_errors_name_placeholder(sample_context: RenderContext, ref: str):
    template = f"{{{{{ref}}}}}"
    with pytest.raises(MissingValueError) as exc_info:
        render_value(template, sample_context)
    assert ref in str(exc_info.value)


def test_render_value_nested_structures(sample_context: RenderContext):
    nested = {
        "a": ["{{input.customer.id}}", {"b": "{{saga.id}}"}],
        "n": 1,
        "t": True,
        "z": None,
    }
    orig_copy = copy.deepcopy(nested)
    result = render_value(nested, sample_context)

    assert nested == orig_copy
    assert result == {
        "a": [42, {"b": "sg-1"}],
        "n": 1,
        "t": True,
        "z": None,
    }
    assert type(result["a"][0]) is int

    with pytest.raises(PlaceholderSyntaxError):
        render_value({"bad_{{input.a}}": 1}, sample_context)

    with pytest.raises(UnsupportedValueError):
        render_value({1, 2, 3}, sample_context)


def test_render_url(sample_context: RenderContext):
    r1 = render_url("http://svc.local/orders/{{input.customer.id}}/x", sample_context)
    assert r1 == "http://svc.local/orders/42/x"

    r2 = render_url("http://svc.local/s?q={{input.text}}", sample_context)
    assert r2 == "http://svc.local/s?q=a%20b%2Fc%3Fd%3De%26f%23g"

    r3 = render_url("http://svc.local/u?q={{input.unicode}}", sample_context)
    assert r3 == "http://svc.local/u?q=%C3%A9"

    r4 = render_url("http://svc.local/a%20b/{{saga.id}}", sample_context)
    assert r4 == "http://svc.local/a%20b/sg-1"

    with pytest.raises(DisallowedPlaceholderError):
        render_url("http://{{input.host}}/x", sample_context)

    with pytest.raises(DisallowedPlaceholderError):
        render_url("http://svc:{{input.port}}/x", sample_context)

    with pytest.raises(DisallowedPlaceholderError):
        render_url("http://svc/{{secret.PAYMENT_KEY}}", sample_context)

    with pytest.raises(PlaceholderSyntaxError):
        render_url("/orders/1", sample_context)

    long_url = "http://svc.local/" + ("a" * 2050)
    with pytest.raises(RenderLimitError):
        render_url(long_url, sample_context)


def test_render_headers(sample_context: RenderContext):
    headers = {
        "X-Api-Key": "{{secret.PAYMENT_KEY}}",
        "X-Saga": "{{saga.id}}",
        "X-N": "{{input.customer.id}}",
    }
    rendered, used_secrets = render_headers(headers, sample_context)
    assert rendered == {
        "X-Api-Key": "s3cr3t-value",
        "X-Saga": "sg-1",
        "X-N": "42",
    }
    assert type(rendered["X-N"]) is str
    assert used_secrets == frozenset({"s3cr3t-value"})

    with pytest.raises(UnsafeValueError):
        render_headers({"X-Bad": "x\r\nInjected: 1"}, sample_context)

    with pytest.raises(UnsafeValueError):
        render_headers({"X-Bad": "x\ny"}, sample_context)

    with pytest.raises(UnsafeValueError):
        render_headers({"X-Bad": "x\x00y"}, sample_context)

    with pytest.raises(PlaceholderSyntaxError):
        render_headers({"X-{{input.a}}": "val"}, sample_context)

    with pytest.raises(PlaceholderSyntaxError):
        render_headers({"X Bad": "val"}, sample_context)

    with pytest.raises(MissingValueError) as exc_info:
        render_headers({"X-Key": "{{secret.MISSING_KEY}}"}, sample_context)
    assert "MISSING_KEY" in str(exc_info.value)

    long_hdr = {"X-Long": "a" * 5000}
    with pytest.raises(RenderLimitError):
        render_headers(long_hdr, sample_context)


def test_render_body(sample_context: RenderContext):
    body_tmpl = {
        "id": "{{input.customer.id}}",
        "name": "{{input.customer.name}}",
        "char": "{{input.unicode}}",
    }
    body_bytes = render_body(body_tmpl, sample_context)
    assert body_bytes == b'{"id":42,"name":"Ana","char":"\xc3\xa9"}'
    assert b"\\u00e9" not in body_bytes

    with pytest.raises(DisallowedPlaceholderError):
        render_body({"sec": "{{secret.PAYMENT_KEY}}"}, sample_context)

    large_tmpl = {"big": "a" * 300_000}
    with pytest.raises(RenderLimitError):
        render_body(large_tmpl, sample_context)

    nan_ctx = RenderContext(
        saga_id="sg-1",
        input={"nan": float("nan")},
        step_responses={},
        secrets={},
    )
    with pytest.raises(UnsupportedValueError):
        render_body({"v": "{{input.nan}}"}, nan_ctx)


def test_redact_text():
    text = "Authorization: Bearer s3cr3t-value and tok"
    redacted = redact_text(text, ["s3cr3t-value", "tok"])
    assert redacted == "Authorization: Bearer *** and ***"

    sub_text = "secret: abcdef is here"
    redacted_sub = redact_text(sub_text, ["abcd", "abcdef"])
    assert redacted_sub == "secret: *** is here"

    assert redact_text("clean text", ["", ""]) == "clean text"
    assert redact_text("clean text", []) == "clean text"


def test_no_secret_leak_in_exceptions(sample_context: RenderContext):
    secret_val = "s3cr3t-value"

    # Test MissingValueError
    with pytest.raises(MissingValueError) as exc1:
        render_headers({"X-Key": "{{secret.ABSENT_KEY}}"}, sample_context)
    assert secret_val not in str(exc1.value)
    assert secret_val not in repr(exc1.value)

    # Test DisallowedPlaceholderError
    with pytest.raises(DisallowedPlaceholderError) as exc2:
        render_body({"sec": "{{secret.PAYMENT_KEY}}"}, sample_context)
    assert secret_val not in str(exc2.value)
    assert secret_val not in repr(exc2.value)

    # Test UnsafeValueError (fails after secret is resolved)
    leak_ctx = RenderContext(
        saga_id="sg-1",
        input={},
        step_responses={},
        secrets={"PAYMENT_KEY": "s3cr3t-value\r\nInjected: 1"},
    )
    with pytest.raises(UnsafeValueError) as exc3:
        render_headers({"X-Key": "{{secret.PAYMENT_KEY}}"}, leak_ctx)
    assert secret_val not in str(exc3.value)
    assert secret_val not in repr(exc3.value)

    # Test RenderLimitError
    limit_ctx = RenderContext(
        saga_id="sg-1",
        input={},
        step_responses={},
        secrets={"PAYMENT_KEY": "s3cr3t-value" * 500},
    )
    with pytest.raises(RenderLimitError) as exc4:
        render_headers({"X-Key": "{{secret.PAYMENT_KEY}}"}, limit_ctx)
    assert secret_val not in str(exc4.value)
    assert secret_val not in repr(exc4.value)

    # Test PlaceholderSyntaxError
    with pytest.raises(PlaceholderSyntaxError) as exc5:
        render_headers({"X Bad": "{{secret.PAYMENT_KEY}}"}, sample_context)
    assert secret_val not in str(exc5.value)
    assert secret_val not in repr(exc5.value)

    # Test UnsupportedValueError
    with pytest.raises(UnsupportedValueError) as exc6:
        render_value("x{{input.customer}}", sample_context)
    assert secret_val not in str(exc6.value)
    assert secret_val not in repr(exc6.value)
