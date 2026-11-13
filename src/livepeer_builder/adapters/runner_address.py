"""Where a runner call goes: a runner URL's origin, and the address a job's path makes of it (#113, C3).

A runner URL names one app on an orchestrator (``https://orch:8936/apps/app-1/app``), and the engine
appends the job's path. aiohttp reads a ``str`` address with yarl, which decodes ``%2e`` and removes dot
segments, so ``/../app-2/app`` reached another app on the same orchestrator. ``JobRequest.path`` refuses
such a path; this is the second check. The address is percent-encoded only where a request target cannot
hold it as written (a space, a non-ASCII character), nothing in it is decoded or removed, aiohttp sends it
as built (``as_built``), and it must still begin with the runner's: the same scheme and authority, and a
path at or under the runner's own.
"""

import re
from urllib.parse import quote, urlsplit

import yarl

from livepeer_builder.errors import RunnerCallError

# Kept as written: RFC 3986's pchar, "/" and "?", and the "%" of an escape. Anything else is encoded.
_KEPT = "!$&'()*+,;=:@/?%"
_LONE_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")


def runner_address(runner_url: str, path: str) -> str:
    """``runner_url`` with ``path`` appended, encoded but never re-normalized. Before any request, a
    ``RunnerCallError`` (``http``, unpaid) when the address would no longer begin with the runner's."""
    base = runner_url.rstrip("/")
    address = base + quote(_LONE_PERCENT.sub("%25", path), safe=_KEPT)
    sent, runner = as_built(address), as_built(base)
    under = runner.raw_path.rstrip("/")
    same_host = (sent.scheme, sent.raw_authority) == (runner.scheme, runner.raw_authority)
    if not same_host or not (sent.raw_path == under or sent.raw_path.startswith(under + "/")):
        raise RunnerCallError("http", f"the path {path!r} leaves the runner {runner_url}", payment_sent=False)
    return address


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
