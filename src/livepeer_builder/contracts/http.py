"""Bounded HTTP values for opaque jobs; no inference-specific vocabulary."""

import re
from urllib.parse import unquote

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")  # C0, DEL and C1
_ENCODED_SEPARATOR = re.compile(r"%(?:2f|5c)", re.IGNORECASE)  # an escaped "/" or "\"

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


def request_path(value: str) -> str:
    """A job's path, already normal (#113, C3): it is appended to a runner URL that names one app on an
    orchestrator, and a client or server that normalizes it would reach another app.

    Refused: a fragment, a backslash or a control character anywhere, and in the part before the query an
    empty segment, a ``.`` or ``..`` segment, literal or percent-encoded (``%2e``, in any case), or an
    encoded separator (``%2F``, ``%5C``): livepeer/node unescapes the path before it forwards it, so
    ``..%2Fother`` would reach the runner as ``../other``. A trailing ``/`` and a query string stay allowed,
    as written.
    """
    if "#" in value or "\\" in value or _CONTROL.search(value):
        raise ValueError("path must not contain a fragment, a backslash or a control character")
    target = value.partition("?")[0]
    if _ENCODED_SEPARATOR.search(target):
        raise ValueError("path must not contain an encoded separator (%2F or %5C)")
    segments = target.split("/")[1:] if target.startswith("/") else target.split("/")
    if segments and segments[-1] == "":  # a trailing slash, or no path at all
        segments.pop()
    for segment in segments:
        if not segment or unquote(segment) in (".", ".."):
            raise ValueError("path must be normal: no empty, '.' or '..' segment, literal or percent-encoded")
    return value
