"""Shared helpers with no package dependencies. Any layer may import this module."""

import asyncio
import hashlib
import json
import re
from collections.abc import Coroutine
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

_SHIELDED: set["asyncio.Task[Any]"] = set()  # strong references, so a shielded task is never collected

BODY_LIMIT = 4096  # how much of a runner's or signer's response body an error keeps
# The price units the engine will pay for, and the Live Runner payment type each is signed as.
PAYMENT_TYPES_BY_UNIT = {"hour": "live", "seconds": "live", "fixed": "fixed"}


def token_hash(token: str) -> str:
    """SHA-256 of an engine-issued token. Only the hash is ever stored."""
    return hashlib.sha256(token.encode()).hexdigest()


def utc(value: datetime) -> datetime:
    """Normalize to timezone-aware UTC (SQLite returns naive datetimes)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def run_shielded[T](work: Coroutine[Any, Any, T]) -> T:
    """Run ``work`` as its own task that the caller's cancellation cannot stop.

    The caller still sees its own ``CancelledError``, but ``work`` runs to the end. That holds under
    plain asyncio and under anyio cancel scopes (Starlette, FastAPI), which cancel every await of the
    host task again until it leaves the scope.
    """
    return await asyncio.shield(spawn_shielded(work))


# A private-use mark so a Decimal survives json.dumps and can be written back as a JSON number.
# json.dumps escapes U+E000, which is how the substitution below finds it. A runner field that
# already contains this prefix would be rewritten; discovery inventories do not.
_DECIMAL_MARK = "\ue000lpb-decimal:"
_DECIMAL_MARK_RE = re.compile(r'"\\u[eE]000lpb-decimal:(-?(?:0|[1-9]\d*)(?:\.\d+)?)"')


def loads_json_numbers(raw: str | bytes | bytearray) -> Any:
    """Parse JSON, keeping a non-integer number as :class:`Decimal`.

    ``json.loads`` turns a JSON number with a fraction or an exponent into a float, which keeps
    about 15 digits. livepeer-node sends ``price_usd`` that way, and the advertised figure is
    longer than a float. ``parse_float=Decimal`` keeps the digits the node wrote. A JSON integer,
    including a wei ``price``, stays ``int``.

    ``livepeer_gateway.http.request_json`` does not do this yet
    (https://github.com/livepeer/livepeer-python-gateway/issues/88). Callers that read discovery
    use this until that release.
    """
    return json.loads(raw, parse_float=Decimal)


def dumps_json_numbers(value: object) -> str:
    """``json.dumps`` that writes a :class:`Decimal` as a JSON number, not a float and not a string.

    The discovery service re-serves an orchestrator's inventory. Encoding a ``Decimal`` with the
    usual ``json.dumps`` raises, and encoding it via ``float`` would drop the digits
    :func:`loads_json_numbers` just kept (livepeer/livepeer-python-gateway#88).
    """
    return _DECIMAL_MARK_RE.sub(r"\1", json.dumps(_mark_decimals(value)))


def _mark_decimals(value: object) -> object:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite decimal")
        return _DECIMAL_MARK + format(value, "f")
    if isinstance(value, dict):
        return {key: _mark_decimals(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_mark_decimals(item) for item in value]
    return value


def spawn_shielded[T](work: Coroutine[Any, Any, T]) -> "asyncio.Task[T]":
    """Start ``work`` as a task no cancellation reaches, and keep it referenced until it ends."""
    task = asyncio.ensure_future(work)
    _SHIELDED.add(task)
    task.add_done_callback(_SHIELDED.discard)
    return task
