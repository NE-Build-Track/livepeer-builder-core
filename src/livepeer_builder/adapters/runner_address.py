"""Where a runner call goes: a runner URL's origin, and the address a job's path makes of it (#113, C3).

A runner URL names one app on an orchestrator (``https://orch:8936/apps/app-1/app``), and the engine
appends the job's path. aiohttp reads a ``str`` address with yarl, which decodes ``%2e`` and removes dot
segments, so ``/../app-2/app`` reached another app on the same orchestrator. ``JobRequest.path`` refuses
such a path; this is the second check. The address is percent-encoded only where a request target cannot
hold it as written (a space, a non-ASCII character), nothing in it is decoded or removed, aiohttp sends it
as built (``as_built``), and it must still begin with the runner's: the same scheme and authority, and a
path at or under the runner's own.

Sent as built, the address gets no encoding from aiohttp, so the runner URL's own path is encoded here
by the same rule as the job's, and a host that is not ASCII by its IDNA name. Only the default discovery
source checks its runner URLs: one that cannot be read is an unpaid ``RunnerCallError``, like any other.
"""

import re
from urllib.parse import quote, urlsplit

import yarl

from livepeer_builder.errors import RunnerCallError

# Kept as written: RFC 3986's pchar, "/" and "?", and the "%" of an escape. Anything else is encoded.
_KEPT = "!$&'()*+,;=:@/?%"
_LONE_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_UNSENDABLE = re.compile(r"[\s\x00-\x1f\x7f-\x9f]")  # whitespace and control characters


def runner_address(runner_url: str, path: str) -> str:
    """``runner_url`` with ``path`` appended, encoded but never re-normalized. Before any request, a
    ``RunnerCallError`` (``http``, unpaid) when the runner URL cannot be read, or when the address would
    no longer begin with the runner's."""
    try:
        if origin(runner_url) is None:  # the rule a paid request has always applied, before yarl reads it
            raise ValueError(runner_url)
        base = _runner_base(runner_url.rstrip("/"))
        runner = _read(base)
    except (ValueError, UnicodeError) as error:
        raise RunnerCallError("http", f"invalid runner URL {runner_url!r}", payment_sent=False) from error
    address = base + _as_sent(path)
    leaves = RunnerCallError("http", f"the path {path!r} leaves the runner {runner_url}", payment_sent=False)
    try:
        sent = _read(address)
    except (ValueError, UnicodeError) as error:  # the path changed the authority into one yarl refuses
        raise leaves from error
    under = runner.raw_path.rstrip("/")
    same_host = (sent.scheme, sent.raw_authority) == (runner.scheme, runner.raw_authority)
    if not same_host or not (sent.raw_path == under or sent.raw_path.startswith(under + "/")):
        raise leaves
    return address


def _read(address: str) -> yarl.URL:
    """``as_built``, read as aiohttp will read it. What yarl cannot parse, or cannot decode the host of,
    is a ``ValueError`` here and not an error in the middle of a request."""
    try:
        url = as_built(address)
        _ = url.host
    except IndexError as error:  # yarl's own, for an authority that ends in "@"
        raise ValueError(address) from error
    return url


def _as_sent(text: str) -> str:
    """Percent-encoded where a request target cannot hold it as written. An escape it already holds is
    kept, never decoded; a ``%`` that begins none becomes ``%25``."""
    return quote(_LONE_PERCENT.sub("%25", text), safe=_KEPT)


def _runner_base(runner_url: str) -> str:
    """The runner URL as it is sent: its path by the same rule as the job's, so the prefix check compares
    two encoded paths. The scheme and authority stay as written, except a host that is not ASCII, which
    yarl encodes by its IDNA name (aiohttp did, before the address was sent as built)."""
    scheme, separator, rest = runner_url.partition("://")
    authority, slash, path = rest.partition("/")
    if not authority or _UNSENDABLE.search(authority):  # nothing to dial, or nothing a request can name
        raise ValueError(runner_url)
    if not authority.isascii():
        authority = yarl.URL(scheme + separator + authority).raw_authority
    return scheme + separator + authority + _as_sent(slash + path)


def as_built(address: str) -> yarl.URL:
    """The URL aiohttp sends exactly as written: yarl re-normalizes a ``str`` it is handed."""
    return yarl.URL(address, encoded=True)


def origin(url: str) -> str | None:
    """``scheme://host:port``, or ``None`` when the URL or its port is not one aiohttp would request.

    An empty port is the scheme default. A stated port counts only in ``1..65535``: ``0`` is not ``443``.
    """
    try:
        parts = urlsplit(url)
        scheme, host, port = parts.scheme.lower(), parts.hostname, parts.port
    except (ValueError, UnicodeError):
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    if scheme not in ("http", "https") or not host or not 1 <= port <= 65535:
        return None
    return f"{scheme}://{host.lower()}:{port}"
