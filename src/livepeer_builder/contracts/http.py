"""Bounded HTTP values for opaque jobs; no inference-specific vocabulary."""

import re
from urllib.parse import unquote

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")  # C0, DEL and C1
_ENCODED_SEPARATOR = re.compile(r"%(?:2f|5c)", re.IGNORECASE)  # an escaped "/" or "\"
_SURROGATE = re.compile(r"[\ud800-\udfff]")  # in a str, always a lone one: nothing can encode it
# What a header value may hold (RFC 9110): a tab, a space, visible ASCII and the bytes 0x80 to 0xFF, each
# of them here as the Latin-1 character an HTTP server writes back as that byte.
_HEADER_VALUE = re.compile(r"[\t\x20-\x7e\x80-\xff]+")
OCTET_STREAM = "application/octet-stream"

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


def response_content_type(value: str) -> str:
    """A runner's content type as the engine keeps it (#113, C27): ``value`` when a response header can
    hold it, and ``application/octet-stream`` when it cannot. The rule is one for every transport (#158).
    The SDK transport hands in the header's bytes read as Latin-1, so a control character, which aiohttp
    lets through, is replaced. A supplied transport hands in its own text: a character Latin-1 cannot
    encode or a lone surrogate was a 500 when the answer was sent, after the job ran, and a line break
    went out as written. A value that begins or ends with a space or a tab is replaced too: the server
    cannot write that header, so the job ran and its answer was lost. Whitespace inside a value is kept."""
    held = _HEADER_VALUE.fullmatch(value) and value == value.strip(" \t")
    return value if held else OCTET_STREAM


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

    Refused: a fragment, a backslash, a control character or a lone surrogate anywhere (JSON can write
    one, ``"\\ud800"``, and no request target can encode it), and in the part before the query an empty
    segment, a ``.`` or ``..`` segment, literal or percent-encoded (``%2e``, in any case), or an encoded
    separator (``%2F``, ``%5C``). livepeer/node decodes the path before it forwards it, so ``%2F``
    reaches the runner as a separator: ``/a%2F%2Fb`` arrives as ``/a//b`` and ``/a%2F.%2Fb`` as ``/a/./b``,
    the empty and ``.`` segments refused here as written. (``..%2Fother`` reaches no runner: the node
    answers a decoded ``..`` segment with 400 before it routes.) The node passes ``%5C`` on as it is; it is
    refused because it decodes to the backslash, which is refused here as written. A trailing ``/`` and a
    query string stay allowed, as written. One thing is kept here and not sent: the ``?`` of an empty
    query (``/run?``), which the HTTP client leaves out of the request target.

    The segments of a path with no leading ``/`` are not read here: the job service refuses every such path
    with one answer (400), whatever its segments, and the transport refuses it again.
    """
    if "#" in value or "\\" in value or _CONTROL.search(value) or _SURROGATE.search(value):
        raise ValueError(
            "path must not contain a fragment, a backslash, a control character or a lone surrogate"
        )
    target = value.partition("?")[0]
    if not target.startswith("/"):
        return value
    if _ENCODED_SEPARATOR.search(target):
        raise ValueError("path must not contain an encoded separator (%2F or %5C)")
    segments = target.split("/")[1:]
    if segments and segments[-1] == "":  # a trailing slash
        segments.pop()
    for segment in segments:
        if not segment or unquote(segment) in (".", ".."):
            raise ValueError("path must be normal: no empty, '.' or '..' segment, literal or percent-encoded")
    return value
