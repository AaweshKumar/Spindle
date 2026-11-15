# Component: Spindle Architecture
# File: placeholders.py
# Description: Pure helpers for parsing, validating, and rendering template placeholders.

from __future__ import annotations

import json
import math
import re
import urllib.parse
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

NAMESPACES = frozenset({"input", "steps", "saga", "secret"})
MAX_BODY_BYTES = 262_144
MAX_URL_CHARS = 2_048
MAX_HEADER_VALUE_CHARS = 4_096

SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
URL_ABS_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*://)([^/?#]*)(.*)$", re.DOTALL)


class PlaceholderError(ValueError):
    """Base exception for all placeholder errors."""


class PlaceholderSyntaxError(PlaceholderError):
    """Raised when placeholder grammar is violated."""


class DisallowedPlaceholderError(PlaceholderError):
    """Raised when a placeholder is not permitted in a context."""


class MissingValueError(PlaceholderError):
    """Raised when a referenced placeholder value is absent."""


class UnsupportedValueError(PlaceholderError):
    """Raised when a value type is unsupported."""


class UnsafeValueError(PlaceholderError):
    """Raised when a rendered value poses a security hazard."""


class RenderLimitError(PlaceholderError):
    """Raised when rendered content exceeds size limits."""


@dataclass(frozen=True)
class Placeholder:
    """Parsed representation of a single placeholder inside a string."""

    raw: str
    namespace: str
    path: tuple[str, ...]
    start: int
    end: int


@dataclass(frozen=True)
class RenderContext:
    """Context holding resolved values and secrets for rendering placeholders."""

    saga_id: str
    input: Mapping[str, Any]
    step_responses: Mapping[str, Any]
    secrets: Mapping[str, str]


def _parse_reference(raw: str) -> tuple[str, tuple[str, ...]]:
    # Validate segment structure and reference grammar without escape syntax.
    if not raw:
        raise PlaceholderSyntaxError("empty placeholder")

    parts = raw.split(".")
    if any(p == "" for p in parts):
        raise PlaceholderSyntaxError(f"empty segment in placeholder: '{raw}'")

    for part in parts:
        if not SEGMENT_RE.match(part):
            raise PlaceholderSyntaxError(f"invalid characters in segment: '{part}'")

    ns = parts[0]
    if ns not in NAMESPACES:
        raise PlaceholderSyntaxError(f"unknown placeholder namespace: '{ns}'")

    if ns == "input":
        if len(parts) < 2:
            raise PlaceholderSyntaxError("input placeholder requires at least one segment")
        return ns, tuple(parts[1:])

    if ns == "saga":
        if raw != "saga.id":
            raise PlaceholderSyntaxError("saga namespace only supports 'saga.id'")
        return ns, ("id",)

    if ns == "secret":
        if len(parts) != 2:
            raise PlaceholderSyntaxError("secret placeholder must have exactly one segment")
        name = parts[1]
        if not SECRET_NAME_RE.match(name):
            raise PlaceholderSyntaxError(
                f"invalid secret name '{name}', must match ^[A-Z][A-Z0-9_]*$"
            )
        return ns, (name,)

    if ns == "steps":
        if len(parts) < 3 or parts[2] != "response":
            raise PlaceholderSyntaxError(
                "steps placeholder must follow 'steps.<step_id>.response[.<seg>]'"
            )
        return ns, tuple(parts[1:])

    raise PlaceholderSyntaxError(f"unhandled namespace '{ns}'")


def find_placeholders(text: str) -> list[Placeholder]:
    """Find and parse all placeholders in a string, preserving appearance order."""
    placeholders: list[Placeholder] = []
    pos = 0
    n = len(text)

    while pos < n:
        start = text.find("{{", pos)
        if start == -1:
            break
        end = text.find("}}", start + 2)
        if end == -1:
            raise PlaceholderSyntaxError("unterminated placeholder")

        inner = text[start + 2 : end]
        raw = inner.strip()
        ns, path = _parse_reference(raw)
        placeholders.append(
            Placeholder(
                raw=raw,
                namespace=ns,
                path=path,
                start=start,
                end=end + 2,
            )
        )
        pos = end + 2

    return placeholders


def iter_placeholders(value: Any, where: str = "$") -> Iterator[tuple[str, Placeholder]]:
    """Walk dict values, list items, and strings, yielding location and placeholders."""
    if isinstance(value, str):
        for ph in find_placeholders(value):
            yield where, ph
    elif isinstance(value, dict):
        for k in value:
            if isinstance(k, str) and "{{" in k:
                raise PlaceholderSyntaxError("placeholders in object keys are not supported")
        for k, v in value.items():
            yield from iter_placeholders(v, f"{where}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            yield from iter_placeholders(item, f"{where}[{i}]")


def check_references(value: Any, *, allow_secrets: bool, where: str = "$") -> list[str]:
    """Inspect all placeholders in a data structure and collect human-readable issues."""
    problems: list[str] = []

    def _walk(val: Any, loc: str) -> None:
        if isinstance(val, str):
            try:
                phs = find_placeholders(val)
            except PlaceholderSyntaxError as exc:
                problems.append(f"{loc}: {exc}")
                return
            for ph in phs:
                if not allow_secrets and ph.namespace == "secret":
                    problems.append(f"{loc}: secret placeholders are only allowed in header values")
        elif isinstance(val, dict):
            for k in val:
                if isinstance(k, str) and "{{" in k:
                    problems.append(f"{loc}: placeholders in object keys are not supported")
            for k, v in val.items():
                _walk(v, f"{loc}.{k}")
        elif isinstance(val, (list, tuple)):
            for i, item in enumerate(val):
                _walk(item, f"{loc}[{i}]")

    _walk(value, where)
    return problems


def _resolve_placeholder(
    ph: Placeholder, ctx: RenderContext, *, allow_secrets: bool = False
) -> Any:
    # Resolve against RenderContext with strict secret protection and path navigation.
    if ph.namespace == "secret":
        if not allow_secrets:
            raise DisallowedPlaceholderError(
                "secret placeholders are only allowed in header values"
            )
        sec_name = ph.path[0]
        if sec_name not in ctx.secrets:
            raise MissingValueError(f"secret '{sec_name}' is missing")
        return str(ctx.secrets[sec_name])

    if ph.namespace == "saga":
        return ctx.saga_id

    if ph.namespace == "input":
        curr: Any = ctx.input
        for seg in ph.path:
            if isinstance(curr, Mapping):
                if seg not in curr:
                    raise MissingValueError(
                        f"missing segment '{seg}' for placeholder '{{{{{ph.raw}}}}}'"
                    )
                curr = curr[seg]
            elif isinstance(curr, (list, tuple)):
                if not seg.isdigit():
                    raise MissingValueError(
                        f"invalid list index '{seg}' for placeholder '{{{{{ph.raw}}}}}'"
                    )
                idx = int(seg)
                if idx < 0 or idx >= len(curr):
                    raise MissingValueError(
                        f"index {idx} out of range for placeholder '{{{{{ph.raw}}}}}'"
                    )
                curr = curr[idx]
            else:
                raise MissingValueError(
                    f"cannot descend into scalar with segment '{seg}' "
                    f"for placeholder '{{{{{ph.raw}}}}}'"
                )
        return curr

    if ph.namespace == "steps":
        step_id = ph.path[0]
        if step_id not in ctx.step_responses:
            raise MissingValueError(
                f"unknown step '{step_id}' for placeholder '{{{{{ph.raw}}}}}'"
            )
        curr = ctx.step_responses[step_id]
        if curr is None:
            if len(ph.path) == 2:
                return None
            failed_seg = ph.path[2]
            raise MissingValueError(
                f"cannot traverse '{failed_seg}' on null response "
                f"for placeholder '{{{{{ph.raw}}}}}'"
            )
        for seg in ph.path[2:]:
            if isinstance(curr, Mapping):
                if seg not in curr:
                    raise MissingValueError(
                        f"missing segment '{seg}' for placeholder '{{{{{ph.raw}}}}}'"
                    )
                curr = curr[seg]
            elif isinstance(curr, (list, tuple)):
                if not seg.isdigit():
                    raise MissingValueError(
                        f"invalid list index '{seg}' for placeholder '{{{{{ph.raw}}}}}'"
                    )
                idx = int(seg)
                if idx < 0 or idx >= len(curr):
                    raise MissingValueError(
                        f"index {idx} out of range for placeholder '{{{{{ph.raw}}}}}'"
                    )
                curr = curr[idx]
            else:
                raise MissingValueError(
                    f"cannot descend into scalar with segment '{seg}' "
                    f"for placeholder '{{{{{ph.raw}}}}}'"
                )
        return curr

    raise UnsupportedValueError(f"unhandled namespace '{ph.namespace}'")


def _stringify_for_text(val: Any, ph: Placeholder) -> str:
    # Convert resolved value to string representation for text interpolation.
    if isinstance(val, str):
        return val
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            raise UnsupportedValueError("non-finite float cannot be converted to text")
        return json.dumps(val)
    if val is None:
        raise UnsupportedValueError(f"value for '{{{{{ph.raw}}}}}' is null")
    if isinstance(val, (dict, list, tuple)):
        raise UnsupportedValueError(
            "cannot interpolate an object or array into text; "
            "use the placeholder alone as the whole value"
        )
    raise UnsupportedValueError(f"unsupported value type: {type(val).__name__}")


def _render_str(text: str, ctx: RenderContext, *, allow_secrets: bool = False) -> Any:
    placeholders = find_placeholders(text)
    if not placeholders:
        return text

    if not allow_secrets:
        for ph in placeholders:
            if ph.namespace == "secret":
                raise DisallowedPlaceholderError(
                    "secret placeholders are only allowed in header values"
                )

    # Exactly one placeholder spanning entire string retains original JSON type.
    if len(placeholders) == 1 and placeholders[0].start == 0 and placeholders[0].end == len(text):
        return _resolve_placeholder(placeholders[0], ctx, allow_secrets=allow_secrets)

    chunks: list[str] = []
    last_pos = 0
    for ph in placeholders:
        chunks.append(text[last_pos : ph.start])
        val = _resolve_placeholder(ph, ctx, allow_secrets=allow_secrets)
        chunks.append(_stringify_for_text(val, ph))
        last_pos = ph.end
    chunks.append(text[last_pos:])
    return "".join(chunks)


def render_value(value: Any, ctx: RenderContext, *, allow_secrets: bool = False) -> Any:
    """Render placeholders across arbitrary nested JSON structures."""
    if isinstance(value, str):
        return _render_str(value, ctx, allow_secrets=allow_secrets)
    if isinstance(value, dict):
        for k in value:
            if isinstance(k, str) and "{{" in k:
                raise PlaceholderSyntaxError("placeholders in object keys are not supported")
        return {k: render_value(v, ctx, allow_secrets=allow_secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [render_value(v, ctx, allow_secrets=allow_secrets) for v in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    raise UnsupportedValueError(f"unsupported value type: {type(value).__name__}")


def render_url(template: str, ctx: RenderContext) -> str:
    """Render an absolute URL template, quote-encoding substituted values."""
    m = URL_ABS_RE.match(template)
    if not m:
        raise PlaceholderSyntaxError("URL must be absolute")

    scheme = m.group(1)
    authority = m.group(2)
    remainder = m.group(3)

    if "{{" in scheme or "{{" in authority:
        raise DisallowedPlaceholderError("placeholders are not allowed in the scheme, host or port")

    all_phs = find_placeholders(template)
    for ph in all_phs:
        if ph.namespace == "secret":
            raise DisallowedPlaceholderError(
                "secret placeholders are only allowed in header values"
            )
        if ph.start < len(scheme) + len(authority):
            raise DisallowedPlaceholderError(
                "placeholders are not allowed in the scheme, host or port"
            )

    rem_phs = find_placeholders(remainder)
    chunks: list[str] = []
    last_pos = 0
    for ph in rem_phs:
        chunks.append(remainder[last_pos : ph.start])
        val = _resolve_placeholder(ph, ctx, allow_secrets=True)
        sval = _stringify_for_text(val, ph)
        chunks.append(urllib.parse.quote(sval, safe=""))
        last_pos = ph.end
    chunks.append(remainder[last_pos:])
    rendered_rem = "".join(chunks)

    rendered_url = f"{scheme}{authority}{rendered_rem}"
    if len(rendered_url) > MAX_URL_CHARS:
        raise RenderLimitError(f"rendered URL exceeds MAX_URL_CHARS ({MAX_URL_CHARS})")
    return rendered_url


def render_headers(
    headers: Mapping[str, str], ctx: RenderContext
) -> tuple[dict[str, str], frozenset[str]]:
    """Render header values as text, safely tracking resolved secrets."""
    rendered: dict[str, str] = {}
    used_secrets: set[str] = set()

    for name, template in headers.items():
        if "{{" in name or not HEADER_NAME_RE.match(name):
            raise PlaceholderSyntaxError(f"invalid header name '{name}'")

        phs = find_placeholders(template)
        for ph in phs:
            if ph.namespace == "secret":
                sec_name = ph.path[0]
                if sec_name not in ctx.secrets:
                    raise MissingValueError(f"secret '{sec_name}' is missing")
                used_secrets.add(ctx.secrets[sec_name])

        chunks: list[str] = []
        last_pos = 0
        for ph in phs:
            chunks.append(template[last_pos : ph.start])
            val = _resolve_placeholder(ph, ctx, allow_secrets=True)
            chunks.append(_stringify_for_text(val, ph))
            last_pos = ph.end
        chunks.append(template[last_pos:])
        val_str = "".join(chunks)

        if "\r" in val_str or "\n" in val_str or "\x00" in val_str:
            raise UnsafeValueError("header value contains control characters")
        if len(val_str) > MAX_HEADER_VALUE_CHARS:
            raise RenderLimitError(
                f"header '{name}' value exceeds MAX_HEADER_VALUE_CHARS ({MAX_HEADER_VALUE_CHARS})"
            )
        rendered[name] = val_str

    return rendered, frozenset(used_secrets)


def render_body(template: Any, ctx: RenderContext) -> bytes:
    """Render request body and serialize to compact, compliant JSON bytes."""
    rendered = render_value(template, ctx, allow_secrets=False)
    try:
        data = json.dumps(
            rendered,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise UnsupportedValueError(f"cannot serialize rendered body to JSON: {exc}") from exc

    if len(data) > MAX_BODY_BYTES:
        raise RenderLimitError(f"rendered body exceeds MAX_BODY_BYTES ({MAX_BODY_BYTES})")
    return data


def redact_text(text: str, secret_values: Iterable[str]) -> str:
    """Replace non-empty secret occurrences with '***', longest first."""
    sorted_secrets = sorted({s for s in secret_values if s}, key=len, reverse=True)
    for sec in sorted_secrets:
        text = text.replace(sec, "***")
    return text
