"""Reading a runner's reply: all at once for a caller that waits, or in pieces with a bound (ISS-26).

An immediate job's caller gets the whole reply, so it is read whole, as it always was. An async job's
reply is stored only up to ``LPB_ASYNC_RESULT_MAX_BYTES``, and nobody waits for it: read whole, a runner
could make the worker hold any number of bytes. ``read_reply`` with a bound reads the body as it arrives,
counts and hashes every piece, and keeps the pieces only while the reply is within the bound.
"""

import hashlib
from collections.abc import AsyncIterable
from typing import NamedTuple, Protocol


class _Pieces(Protocol):
    def iter_any(self) -> AsyncIterable[bytes]: ...


class Readable(Protocol):
    """What this module needs of an HTTP response: aiohttp's ``ClientResponse`` is one, and aiohttp
    itself stays in the modules that own it (ARCHITECTURE.md#third-party-boundaries)."""

    @property
    def content(self) -> _Pieces: ...

    async def read(self) -> bytes: ...


class ReplyBody(NamedTuple):
    """A reply as it was read. ``size_bytes`` and ``sha256`` are ``None`` for a reply read whole and not
    measured here. Otherwise they are of the whole reply, and ``content`` is empty when it was over the
    bound."""

    content: bytes
    size_bytes: int | None = None
    sha256: str | None = None


async def read_reply(response: Readable, max_bytes: int | None) -> ReplyBody:
    """The body of ``response``. With ``max_bytes``, at most that much of it is held at a time, beside
    the piece being read: a reply that grows past the bound is read to its end and none of it is kept."""
    if max_bytes is None:
        return ReplyBody(await response.read())
    digest, size = hashlib.sha256(), 0
    kept: list[bytes] = []
    async for piece in response.content.iter_any():
        size += len(piece)
        digest.update(piece)
        if size <= max_bytes:
            kept.append(piece)
        else:
            kept.clear()  # over the bound: what was kept goes now, not when the reply ends
    return ReplyBody(b"".join(kept), size, digest.hexdigest())
