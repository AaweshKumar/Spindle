import pytest

from spindle_core.callspec.placeholders import (
    Placeholder,
    PlaceholderSyntaxError,
    check_references,
    find_placeholders,
    iter_placeholders,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "{{input.a}}",
            [Placeholder(raw="input.a", namespace="input", path=("a",), start=0, end=11)],
        ),
        (
            "{{ input.a.b.0 }}",
            [
                Placeholder(
                    raw="input.a.b.0",
                    namespace="input",
                    path=("a", "b", "0"),
                    start=0,
                    end=17,
                )
            ],
        ),
        (
            "{{saga.id}}",
            [Placeholder(raw="saga.id", namespace="saga", path=("id",), start=0, end=11)],
        ),
        (
            "{{secret.PAYMENT_KEY}}",
            [
                Placeholder(
                    raw="secret.PAYMENT_KEY",
                    namespace="secret",
                    path=("PAYMENT_KEY",),
                    start=0,
                    end=22,
                )
            ],
        ),
        (
            "{{secret.K2}}",
            [Placeholder(raw="secret.K2", namespace="secret", path=("K2",), start=0, end=13)],
        ),
        (
            "{{steps.book-flight.response}}",
            [
                Placeholder(
                    raw="steps.book-flight.response",
                    namespace="steps",
                    path=("book-flight", "response"),
                    start=0,
                    end=30,
                )
            ],
        ),
        (
            "{{steps.book-flight.response.items.0.id}}",
            [
                Placeholder(
                    raw="steps.book-flight.response.items.0.id",
                    namespace="steps",
                    path=("book-flight", "response", "items", "0", "id"),
                    start=0,
                    end=41,
                )
            ],
        ),
        (
            "x{{input.a}}y{{input.b}}z",
            [
                Placeholder(raw="input.a", namespace="input", path=("a",), start=1, end=12),
                Placeholder(raw="input.b", namespace="input", path=("b",), start=13, end=24),
            ],
        ),
    ],
)
def test_valid_placeholders_parse(text: str, expected: list[Placeholder]):
    assert find_placeholders(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "plain text",
        "literal }} lone closer",
        "just }} braces }} here",
    ],
)
def test_no_placeholders_or_lone_closers(text: str):
    assert find_placeholders(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "{{input.a",
        "{{",
        "{{}}",
        "{{ }}",
        "{{input}}",
        "{{input.}}",
        "{{input..a}}",
        "{{.input.a}}",
        "{{unknown.a}}",
        "{{saga.name}}",
        "{{saga}}",
        "{{secret.lower}}",
        "{{secret.A.B}}",
        "{{secret.1A}}",
        "{{steps.x}}",
        "{{steps.x.output}}",
        "{{steps.x.output.id}}",
        "{{input.a b}}",
        "{{input.é}}",
        "{{{input.a}}}",
        "{{ input.a }} {{",
    ],
)
def test_invalid_placeholders_raise(text: str):
    with pytest.raises(PlaceholderSyntaxError):
        find_placeholders(text)


def test_find_placeholders_spans_and_immutability():
    text = "prefix-{{input.a}}-middle-{{saga.id}}-suffix"
    orig = str(text)
    phs = find_placeholders(text)
    assert text == orig
    assert len(phs) == 2
    for ph in phs:
        assert text[ph.start : ph.end].startswith("{{")
        assert text[ph.start : ph.end].endswith("}}")


def test_iter_placeholders_locations():
    data = {
        "items": [
            {"id": "{{input.first}}"},
            {"name": "{{input.second}}"},
        ],
        "scalar": 123,
        "flag": True,
        "none": None,
    }
    results = list(iter_placeholders(data, "$"))
    assert len(results) == 2
    assert results[0][0] == "$.items[0].id"
    assert results[0][1].raw == "input.first"
    assert results[1][0] == "$.items[1].name"
    assert results[1][1].raw == "input.second"


def test_iter_placeholders_rejects_dict_key():
    data = {"bad_{{input.a}}_key": "val"}
    with pytest.raises(PlaceholderSyntaxError) as exc_info:
        list(iter_placeholders(data))
    assert "placeholders in object keys are not supported" in str(exc_info.value)


def test_iter_placeholders_non_containers():
    assert list(iter_placeholders(123)) == []
    assert list(iter_placeholders(True)) == []
    assert list(iter_placeholders(None)) == []


def test_check_references_clean():
    data = {"a": "{{input.a}}", "b": ["{{saga.id}}"]}
    assert check_references(data, allow_secrets=False) == []


def test_check_references_reports_problems():
    data = {
        "ok": "{{input.valid}}",
        "bad_syntax": "{{input..bad}}",
        "secret_field": "{{secret.API_KEY}}",
        "nested": [{"broken": "{{unterminated"}],
    }
    problems_no_sec = check_references(data, allow_secrets=False)
    assert len(problems_no_sec) == 3
    assert any("$.bad_syntax" in p for p in problems_no_sec)
    assert any(
        "$.secret_field" in p and "secret placeholders are only allowed" in p
        for p in problems_no_sec
    )
    assert any("$.nested[0].broken" in p for p in problems_no_sec)

    problems_allow_sec = check_references(data, allow_secrets=True)
    assert len(problems_allow_sec) == 2
    assert not any("secret placeholders" in p for p in problems_allow_sec)


def test_check_references_never_raises_on_bad_key():
    data = {"bad_{{k}}": 123}
    problems = check_references(data, allow_secrets=False)
    assert len(problems) == 1
    assert "placeholders in object keys are not supported" in problems[0]
