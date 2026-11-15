import pytest

from spindle_core.callspec.url_rules import check_url_template, parse_allowed_hosts


@pytest.mark.parametrize(
    "url",
    [
        "http://svc/x",
        "https://svc.example.com:8443/a/b?c=d",
        "http://10.1.2.3:9000/x",
        "http://127.0.0.1:8000/",
        "http://[::1]:8000/x",
        "http://mock-services:9000/orders/{{saga.id}}",
        "http://svc/s?q={{ input.text }}",
    ],
)
def test_acceptable_urls(url: str):
    assert check_url_template(url) == []


@pytest.mark.parametrize(
    ("url", "keyword"),
    [
        ("", "empty"),
        (5, "URL must be a string"),
        ("a" * 2100, "longer"),
        ("ftp://svc/x", "http"),
        ("//svc/x", "scheme"),
        ("svc/x", "absolute"),
        ("http:///x", "host"),
        ("http://user:pw@svc/x", "credentials"),
        ("http://svc:0/x", "port"),
        ("http://svc:70000/x", "port"),
        ("http://169.254.169.254/latest", "link-local"),
        ("http://[fe80::1]/x", "link-local"),
        ("http://0.0.0.0/x", "unspecified"),
        ("http://[::]/x", "unspecified"),
        ("http://[::ffff:169.254.169.254]/x", "link-local"),
        ("http://2852039166/x", "ambiguous"),
        ("http://0xA9FEA9FE/x", "ambiguous"),
        ("http://0251.0376.0251.0376/x", "ambiguous"),
        ("http://metadata.google.internal/x", "metadata"),
        ("http://metadata.google.internal./x", "metadata"),
        ("http://svc/a b", "whitespace"),
        ("http://svc/{{input.a", "unterminated"),
        ("http://{{input.h}}/x", "placeholders are not allowed"),
        ("http://svc:{{input.p}}/x", "placeholders are not allowed"),
        ("http://svc/{{secret.K}}", "secret"),
    ],
)
def test_rejected_urls(url, keyword: str):
    problems = check_url_template(url)
    assert len(problems) >= 1
    assert any(
        keyword.lower() in p.lower() for p in problems
    ), f"Expected '{keyword}' in {problems}"


def test_allowed_hosts_enforcement():
    allowed = frozenset({"svc.example.com"})
    assert check_url_template("http://svc.example.com/x", allowed_hosts=allowed) == []
    assert check_url_template("http://SVC.example.com./x", allowed_hosts=allowed) == []

    problems = check_url_template("http://other.com/x", allowed_hosts=allowed)
    assert len(problems) == 1
    assert "not in the allowed hosts" in problems[0]

    assert check_url_template("http://other.com/x", allowed_hosts=None) == []


def test_parse_allowed_hosts():
    parsed = parse_allowed_hosts("a.com, B.com ,,")
    assert parsed == frozenset({"a.com", "b.com"})

    assert parse_allowed_hosts(None) is None
    assert parse_allowed_hosts("") is None
    assert parse_allowed_hosts("   ") is None


def test_multiple_problems_reported():
    url = "http://user:pw@169.254.169.254/x"
    problems = check_url_template(url)
    assert len(problems) >= 2
    assert any("credentials" in p for p in problems)
    assert any("link-local" in p or "metadata" in p for p in problems)


@pytest.mark.parametrize(
    "odd_input",
    [
        None,
        123,
        b"http://x",
        "http://[bad",
        "http://svc:abc/",
    ],
)
def test_check_url_template_never_raises(odd_input):
    problems = check_url_template(odd_input)
    assert isinstance(problems, list)
    assert len(problems) >= 1
