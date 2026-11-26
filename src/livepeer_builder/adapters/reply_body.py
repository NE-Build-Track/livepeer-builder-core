"""Reading a runner's reply: all at once for a caller that waits, or in pieces with a bound (ISS-26).

An immediate job's caller gets the whole reply, so it is read whole, as it always was. An async job's
reply is stored only up to ``LPB_ASYNC_RESULT_MAX_BYTES``, and nobody waits for it: read whole, a runner
could make the worker hold any number of bytes. ``read_reply`` with a bound reads the body as it arrives,
counts and hashes every piece, and keeps the pieces only while the reply is within the bound.

An answer that is not the caller's result (an error, a payment challenge) is never anybody's to receive
whole: ``answer_text`` reads the part of it the engine uses and stops there.
"""

import hashlib
import json
from collections.abc import AsyncIterable
from typing import Any, NamedTuple, Protocol

from livepeer_builder.utils import BODY_LIMIT

# The most the engine reads of a 402 before it parses it (#184 review). livepeer/node's challenge is about
# one KiB: an OrchestratorInfo with one set of ticket parameters in base64, a manifest id and two URLs.
# 64 KiB leaves room for many times that, and a longer body is refused before anything is signed.
CHALLENGE_LIMIT = 64 * 1024


class _Pieces(Protocol):
    def iter_any(self) -> AsyncIterable[bytes]: ...


class Readable(Protocol):
    """What this module needs of an HTTP response: aiohttp's ``ClientResponse`` is one, and aiohttp
    itself stays in the modules that own it (ARCHITECTURE.md#third-party-boundaries)."""

    @property
    def content(self) -> _Pieces: ...

    async def read(self) -> bytes: ...


class Answer(Readable, Protocol):
    """A response whose status is known and whose connection can be given back or closed."""

    @property
    def status(self) -> int: ...

    def release(self) -> object: ...

    def close(self) -> None: ...


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


async def answer_text(response: Answer) -> tuple[str, bool]:
    """The text of an answer that is not the caller's result, and whether it is all of it (#184 review).

    A 402 is parsed, so it is read to its end, up to ``CHALLENGE_LIMIT``. Of any other answer an error
    keeps the first ``BODY_LIMIT`` characters, and the bytes that hold them are read: what is kept is
    what was kept when the body was read whole. A longer body is read no further, so a runner cannot
    make a call hold more than that, and the connection it is still arriving on is closed."""
    limit = CHALLENGE_LIMIT if response.status == 402 else bytes_holding(BODY_LIMIT)
    start, whole = await read_start(response, limit)
    if whole:
        response.release()
    else:
        response.close()
    return start.decode(errors="replace"), whole


async def read_start(response: Readable, max_bytes: int) -> tuple[bytes, bool]:
    """The first ``max_bytes`` of the body, and whether that is all of it. The read stops once the body
    is known to be longer: at most ``max_bytes`` and the piece being read are held, and the rest is left
    unread, so the caller closes the response and its connection is not used again."""
    kept: list[bytes] = []
    size = 0
    async for piece in response.content.iter_any():
        kept.append(piece)
        size += len(piece)
        if size > max_bytes:
            return b"".join(kept)[:max_bytes], False
    return b"".join(kept), True


def bytes_holding(characters: int) -> int:
    """How many bytes from the start of a body hold its first ``characters`` characters, whatever the
    bytes are. UTF-8 gives a character four bytes at most, and decoding with ``errors="replace"`` makes
    one character of every run of one to three bytes it cannot read. So no character takes more than
    four bytes, and ``body[: bytes_holding(n)].decode(errors="replace")[:n]`` is
    ``body.decode(errors="replace")[:n]`` for every body: a character the cut divides comes after the
    first ``n``."""
    return 4 * characters


def json_object(body: bytes, content_type: str) -> dict[str, Any] | None:
    mime = content_type.split(";", 1)[0].strip().lower()
    if not (mime.endswith("/json") or mime.endswith("+json")):
        return None
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
