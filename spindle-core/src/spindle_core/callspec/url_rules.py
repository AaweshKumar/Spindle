# Component: Spindle Architecture
# File: url_rules.py
# Description: Security and validation rules for step URL templates.

from __future__ import annotations

import ipaddress
import re
import urllib.parse
from typing import Any

from .placeholders import MAX_URL_CHARS, PlaceholderSyntaxError, find_placeholders

METADATA_IPS = frozenset({
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("169.254.170.2"),
    ipaddress.ip_address("fd00:ec2::254"),
})
METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata"})
NUMERIC_HOST_RE = re.compile(r"^(0x[0-9a-fA-F]+|[0-9]+)(\.(0x[0-9a-fA-F]+|[0-9]+))*$")


def parse_allowed_hosts(value: str | None) -> frozenset[str] | None:
    """Parse comma-separated allowed hosts, returning None if unrestricted."""
    if value is None:
        return None
    hosts = {h.strip().lower() for h in value.split(",") if h.strip()}
    return frozenset(hosts) if hosts else None


def check_url_template(url: Any, *, allowed_hosts: frozenset[str] | None = None) -> list[str]:
    """Validate a URL template against security rules, collecting all detected problems."""
    # Rule 1: Type, emptiness, and length checks
    if not isinstance(url, str):
        return ["URL must be a string"]
    if not url:
        return ["URL must not be empty"]
    if len(url) > MAX_URL_CHARS:
        return [f"URL is longer than maximum length of {MAX_URL_CHARS} characters"]

    # Rule 2: Placeholder syntax check and skeleton building
    try:
        phs = find_placeholders(url)
    except PlaceholderSyntaxError as exc:
        return [str(exc)]

    # Build skeleton replacing each placeholder span with 'p'
    skeleton = url
    for ph in sorted(phs, key=lambda p: p.start, reverse=True):
        skeleton = skeleton[: ph.start] + "p" + skeleton[ph.end :]

    problems: list[str] = []

    # Rule 3: Whitespace and control character validation on skeleton
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in skeleton):
        problems.append("URL must not contain whitespace or control characters")

    # Rule 4: Scheme validation
    scheme_idx = skeleton.find("://")
    if scheme_idx == -1:
        problems.append("URL must be absolute and have scheme http or https")
    else:
        scheme = skeleton[:scheme_idx].lower()
        if scheme not in {"http", "https"}:
            problems.append(f"URL scheme '{scheme}' is invalid; must be http or https")

    # Rule 5: Authority in original URL must not have placeholders
    orig_scheme_idx = url.find("://")
    orig_auth = ""
    if orig_scheme_idx != -1:
        auth_start = orig_scheme_idx + 3
        auth_end = len(url)
        for delim in ("/", "?", "#"):
            pos = url.find(delim, auth_start)
            if pos != -1 and pos < auth_end:
                auth_end = pos
        orig_auth = url[auth_start:auth_end]
        if "{{" in orig_auth:
            problems.append("placeholders are not allowed in the scheme, host or port")

    # Parse authority and host on skeleton
    skel_auth = ""
    if scheme_idx != -1:
        s_auth_start = scheme_idx + 3
        s_auth_end = len(skeleton)
        for delim in ("/", "?", "#"):
            pos = skeleton.find(delim, s_auth_start)
            if pos != -1 and pos < s_auth_end:
                s_auth_end = pos
        skel_auth = skeleton[s_auth_start:s_auth_end]

    # Rule 6: Userinfo check
    if "@" in skel_auth or "@" in orig_auth:
        problems.append("URL must not contain credentials (user:password@host)")

    # Rule 7: Host and port validation
    host = ""
    try:
        parsed = urllib.parse.urlsplit(skeleton)
    except ValueError:
        problems.append("URL has malformed authority or host")
        parsed = None

    if parsed is not None:
        try:
            port = parsed.port
            if port is not None and not (1 <= port <= 65535):
                problems.append("invalid port: out of range 1..65535")
        except ValueError:
            problems.append("invalid port: non-numeric or malformed")

        if not parsed.hostname:
            problems.append("URL must include a host")
        else:
            host = parsed.hostname.lower().rstrip(".")

    # Rule 8: IP address security checks
    is_ip = False
    if host:
        try:
            ip = ipaddress.ip_address(host)
            is_ip = True
            eff_ip = ip.ipv4_mapped if getattr(ip, "ipv4_mapped", None) else ip
            if eff_ip.is_link_local or eff_ip in METADATA_IPS:
                problems.append(f"host is a link-local or metadata IP address: {eff_ip}")
            elif eff_ip.is_unspecified:
                problems.append(f"host is an unspecified address: {eff_ip}")
        except ValueError:
            pass

    # Rule 9: Ambiguous numeric host representations
    if host and not is_ip:
        if NUMERIC_HOST_RE.match(host):
            problems.append(f"ambiguous numeric host: '{host}'")

    # Rule 10: Metadata hostnames
    if host in METADATA_HOSTS:
        problems.append(f"host cannot target cloud metadata services: '{host}'")

    # Rule 11: Allowed hosts constraint
    if allowed_hosts is not None and host:
        if host not in allowed_hosts:
            problems.append(f"host '{host}' is not in the allowed hosts")

    # Rule 12: Secret placeholders in URL
    for ph in phs:
        if ph.namespace == "secret":
            problems.append("secret placeholders are only allowed in header values")

    return problems
