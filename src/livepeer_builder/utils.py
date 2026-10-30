"""Shared helpers with no package dependencies. Any layer may import this module."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterable, Coroutine
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

_SHIELDED: set["asyncio.Task[Any]"] = set()  # strong references, so a shielded task is never collected

BODY_LIMIT = 4096  # how much of a runner's or signer's response body an error keeps
# The most a /discovery inventory may be: livepeer/node's discovery reads no more
# (discovery/shared/inventory.go, MaxBody)
INVENTORY_MAX_BYTES = 1 << 20
# The ceilings livepeer-node's signer accepts (signer/service.go, checkMaxPrice): wei, per second for a live
# payment or per request for a fixed one. Each unit the engine pays, and the payment type it is signed as.
PAYMENT_CURRENCY = "wei"
PAYMENT_TYPES_BY_UNIT = {"seconds": "live", "fixed": "fixed"}


def is_payable(currency: str, unit: str) -> bool:
    """The signer takes a ceiling in this currency and unit; it refuses any other (HTTP 400)."""
    return currency.strip().lower() == PAYMENT_CURRENCY and unit.strip().lower() in PAYMENT_TYPES_BY_UNIT


def signable(amount: Decimal, currency: str, unit: str) -> bool:
    """The signer takes a ceiling for this price: a positive one (a free price needs none, and a 0.0 ceiling
    is a 400), in a currency and unit it takes. The engine signs for nothing else."""
    return amount > 0 and is_payable(currency, unit)


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

    livepeer-node's ``price_usd`` has more digits than a float keeps. Integers stay ``int``. ``NaN`` and
    ``Infinity`` are not JSON and raise ``ValueError``, as an unparseable body does. So does an exponent
    ``Decimal`` cannot construct (past ``10**18``).
    """
    return json.loads(raw, parse_float=decimal_json_number, parse_constant=_not_json)


def decimal_json_number(token: str) -> Decimal:
    """A JSON number token as :class:`Decimal`.

    An exponent past ``10**18`` makes ``Decimal`` raise ``InvalidOperation``. Discovery must refuse that
    inventory: the token would otherwise be re-served, and every engine refresh would fail on it.
    """
    try:
        amount = Decimal(token)
    except InvalidOperation as exc:
        raise ValueError(f"{token} is not a JSON number") from exc
    if not amount.is_finite():
        raise ValueError(f"{token} is not a JSON number")
    return amount


def _not_json(token: str) -> Any:
    raise ValueError(f"{token} is not a JSON number")


async def read_capped(chunks: AsyncIterable[bytes], limit: int) -> bytes:
    """A streamed body, refused (``ValueError``) as soon as it is longer than ``limit`` bytes."""
    body = bytearray()
    async for chunk in chunks:
        body += chunk
        if len(body) > limit:
            raise ValueError(f"inventory larger than {limit} bytes")
    return bytes(body)
