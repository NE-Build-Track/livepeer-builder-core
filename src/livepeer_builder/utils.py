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


class _UnreadableNumber:
    """A JSON number :class:`Decimal` cannot build. Not a number to any reader of it."""

    def __repr__(self) -> str:
        return "UNREADABLE_NUMBER"


UNREADABLE_NUMBER = _UnreadableNumber()


def loads_json_numbers(raw: str | bytes | bytearray) -> Any:
    """``json.loads`` with a fractional number as :class:`Decimal`, not a float.

    livepeer-node's ``price_usd`` has more digits than a float keeps. Integers stay ``int``. A number
    ``Decimal`` cannot build (an exponent past ``10**18``), or an integer past the runtime digit limit, is
    ``UNREADABLE_NUMBER``, so only the field that holds it is unreadable, not the document: livepeer-node's
    discovery cache passes the token through as received. ``NaN`` and ``Infinity`` are not JSON and raise
    ``ValueError``, as an unparseable body does.
    """
    return json.loads(raw, parse_float=_lenient_number, parse_int=_lenient_int, parse_constant=_not_json)


def _lenient_number(token: str) -> Decimal | _UnreadableNumber:
    try:  # a JSON number token is never NaN or Infinity: those are constants (parse_constant)
        return Decimal(token)
    except InvalidOperation:  # an exponent past 10**18: Decimal cannot build it
        return UNREADABLE_NUMBER


def _lenient_int(token: str) -> int | _UnreadableNumber:
    """An integer token, or ``UNREADABLE_NUMBER`` past the runtime digit limit
    (``sys.get_int_max_str_digits``, 4300 by default), where ``int`` raises and would fail every runner."""
    try:
        return int(token)
    except ValueError:
        return UNREADABLE_NUMBER


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
