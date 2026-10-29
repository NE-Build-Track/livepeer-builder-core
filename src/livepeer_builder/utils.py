"""Shared helpers with no package dependencies. Any layer may import this module."""

import asyncio
import hashlib
import json
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


def spawn_shielded[T](work: Coroutine[Any, Any, T]) -> "asyncio.Task[T]":
    """Start ``work`` as a task no cancellation reaches, and keep it referenced until it ends."""
    task = asyncio.ensure_future(work)
    _SHIELDED.add(task)
    task.add_done_callback(_SHIELDED.discard)
    return task


def loads_json_numbers(raw: str | bytes | bytearray) -> Any:
    """``json.loads`` with a fractional number as :class:`Decimal`, not a float.

    livepeer-node's ``price_usd`` has more digits than a float keeps. Integers stay ``int``.
    """
    return json.loads(raw, parse_float=Decimal)


def dumps_json_numbers(value: object) -> str:
    """Compact ``json.dumps`` that writes a :class:`Decimal` as the same JSON number.

    ``str`` keeps the digits as sent; fixed-point would pad ``1E-999999`` to a megabyte."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite decimal")
        return str(value)
    if isinstance(value, dict):
        return "{" + ",".join(f"{_dumps(str(k))}:{dumps_json_numbers(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list | tuple):
        return "[" + ",".join(dumps_json_numbers(item) for item in value) + "]"
    return _dumps(value)


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
