"""Shared helpers with no package dependencies. Any layer may import this module."""

import asyncio
import hashlib
import json
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

_SHIELDED: set["asyncio.Task[Any]"] = set()  # strong references, so a shielded task is never collected

BODY_LIMIT = 4096  # how much of a runner's or signer's response body an error keeps


def token_hash(token: str) -> str:
    """SHA-256 of an engine-issued token. Only the hash is ever stored."""
    return hashlib.sha256(token.encode()).hexdigest()


def json_body(content: bytes, content_type: str) -> dict[str, Any] | None:
    """The body as a JSON object, when its content type says JSON and it is one; else ``None``. What a
    runner's reply carries as ``data``, worked out again for a stored result (ISS-26)."""
    mime = content_type.split(";", 1)[0].strip().lower()
    if not mime.endswith(("/json", "+json")):
        return None
    try:
        data = json.loads(content)
    except (ValueError, RecursionError):  # not JSON, not text, or nested past what the decoder reads
        return None
    return data if isinstance(data, dict) else None


def utc(value: datetime) -> datetime:
    """Normalize to timezone-aware UTC (SQLite returns naive datetimes). A time with no offset is UTC.

    A datetime is naive when it names no offset, whatever ``tzinfo`` it carries: one whose ``utcoffset()``
    is ``None`` is read as UTC too. ``astimezone`` reads such a time in the host's own zone, so a bound of
    a cost summary moved by the host's offset (#187 review)."""
    return value.replace(tzinfo=UTC) if value.utcoffset() is None else value.astimezone(UTC)


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
