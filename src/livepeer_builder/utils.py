"""Shared helpers with no package dependencies. Any layer may import this module."""

import asyncio
import hashlib
import json
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

_SHIELDED: set["asyncio.Task[Any]"] = set()  # strong references, so a shielded task is never collected

BODY_LIMIT = 4096  # how much of a runner's, a signer's or the payment provider's error body an error keeps


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


class CursorTrail:
    """Whether a list read page by page names a cursor it was already at, in constant memory.

    Each page names the next cursor. A page that names the cursor it was asked with would be asked for
    again without end, and so would a provider whose cursors go round (A, B, A). Remembering every
    cursor would grow with the list. This keeps two: the cursor the page was asked with, so a cursor
    that does not advance is found at once, and one earlier cursor, the mark, which moves to the
    current cursor after 1, 2, 4, 8... pages (Brent's cycle detection), so a longer cycle is found after
    at most a few turns of it. A list that only advances is never refused."""

    def __init__(self, start: str | None, kept: str | None = None) -> None:
        """A trail from ``start``, the cursor the first page is asked with. A reader that goes on where
        an earlier one stopped (a resumed search) passes the cursor it goes on from and what that
        one's ``keep()`` returned, and is on the same trail: one that began again with every reader
        would never see a cursor come back when each reads a page or two. With nothing usable kept,
        the trail starts here."""
        self._asked = start or ""
        self._mark, self._steps, self._span = self._asked, 0, 1
        try:
            mark, steps, span = json.loads(kept or "")
        except (ValueError, TypeError, RecursionError):
            return
        if isinstance(mark, str) and type(steps) is int and type(span) is int and 0 <= steps < span:
            self._mark, self._steps, self._span = mark, steps, span

    def keep(self) -> str:
        """The trail as text, to record beside the cursor a later reader goes on from. That cursor is
        the one the next page is asked with, so it is not kept twice."""
        return json.dumps([self._mark, self._steps, self._span])

    def repeats(self, cursor: str) -> bool:
        """Whether ``cursor``, which a page just named as the next one, is the cursor that page was asked
        with or the mark: asking with it would read pages already read. Call it once for each next
        cursor, in order."""
        if cursor in (self._asked, self._mark):
            return True
        self._asked = cursor
        self._steps += 1
        if self._steps == self._span:
            self._mark, self._steps, self._span = cursor, 0, self._span * 2
        return False
