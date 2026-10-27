"""Bounded HTTP values for opaque jobs; no inference-specific vocabulary."""

import re

# Transport/payment credentials and framing are owned by the transport, not a job payload.
_RESERVED = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "host",
        "connection",
        "keep-alive",
        "proxy-connection",
        "te",
        "trailer",
        "upgrade",
        "transfer-encoding",
        "content-length",
        "content-encoding",
        "content-type",
        "expect",
        "accept-encoding",
    }
)


def request_headers(value: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    if len(value) > 32:
        raise ValueError("Too many runner headers")
    seen: set[str] = set()
    result = []
    size = 0
    for name, content in value:
        name = name.lower()
        if (
            not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", name)
            or name in _RESERVED
            or name.startswith("livepeer-")
            or name in seen
        ):
            raise ValueError("Reserved, duplicate or invalid runner header")
        if any(ord(c) < 32 or ord(c) > 126 for c in content):
            raise ValueError("Invalid runner header value")
        size += len(name) + len(content)
        if size > 16384:
            raise ValueError("Runner headers exceed limit")
        seen.add(name)
        result.append((name, content))
    return tuple(sorted(result))
