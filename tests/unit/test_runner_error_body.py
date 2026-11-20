"""#184 review: what the engine reads of a runner's error answer and of its payment challenge.

A runner's answer of 300 or above is not the caller's result. Of an error the engine keeps the first
``BODY_LIMIT`` characters, and a 402 is parsed as a payment challenge. Both were read whole, whatever
their length, so a runner could make a call hold any number of bytes. These tests run the SDK transport
against a loopback runner and signer: the read is bounded, what is kept of an error is what was kept
before, and a challenge of any length a real one has is still paid.
"""

import json
from collections.abc import AsyncIterator

import aiohttp
import pytest
from hypothesis import given
from hypothesis import strategies as st

from livepeer_builder.adapters.reply_body import CHALLENGE_LIMIT, bytes_holding
from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.contracts import JobRequest, RunnerReply
from livepeer_builder.errors import RunnerCallError
from livepeer_builder.utils import BODY_LIMIT
from tests.unit.livepeer_net import Net, Reply, start

APP = "example/echo"
FACE = "\N{GRINNING FACE}".encode()  # four bytes of UTF-8, one character


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        yield value


async def call(net: Net, **fields: object) -> RunnerReply:
    request = JobRequest.model_validate({"app": APP, "path": "/run", "payload": {"a": 1}, **fields})
    return await SdkRunnerTransport(net.url).call(net.runner(), request, "lpg_k")


def challenge(net: Net, length: int) -> bytes:
    """A payment challenge this runner's orchestrator could send, padded with spaces to ``length``."""
    body = {"payment_params": "params", "manifest_id": "m-1", "payment_url": f"{net.url}/pay/m-1"}
    return json.dumps(body).encode().ljust(length)


async def test_an_error_answer_is_read_only_as_far_as_the_engine_keeps_it(net: Net) -> None:
    """An error of 400 KiB that arrives over four seconds, for a job with one second: read whole, the
    read ran into the job's timeout. The engine keeps 4096 characters of it and stops reading once it
    holds them, so the job fails with the runner's own answer."""
    net.replies = [Reply(status=500, content_type="text/plain", chunks=(b"x" * 1024,) * 400)]
    with pytest.raises(RunnerCallError) as failed:
        await call(net, timeout_s=1.0)
    error = failed.value
    assert (error.kind, error.status_code, error.body) == ("http", 500, "x" * BODY_LIMIT)
    assert (error.payment_sent, error.manifest_ids) == (True, ("m-1",))


@pytest.mark.parametrize(
    "body",
    [
        b"no capacity available for runner",
        b"",
        b"x" * BODY_LIMIT,
        b"x" * (BODY_LIMIT + 1),
        b"x" * 100_000,
        FACE * 5000,  # the first 4096 characters are the first 16 384 bytes
        b"a" + FACE * 5000,  # ... and here a character lies across byte 16 384
        "\N{LATIN SMALL LETTER E WITH ACUTE}".encode() * 9000,
        b"\xff" * 20_000,  # not UTF-8: each byte is kept as one replacement character
        b"\xf0\x90\x80" * 7000,  # the start of a character, never its end
        b"x" * 16_383 + FACE,
    ],
    ids=lambda body: f"{len(body)}-bytes-{body[:4].hex()}",
)
async def test_what_is_kept_of_an_error_answer_is_what_was_kept_when_it_was_read_whole(
    net: Net, body: bytes
) -> None:
    """An immediate job's caller receives the same ``failure.body`` as before, for a body of any length
    and any bytes: the first ``BODY_LIMIT`` characters of the whole body, decoded as it always was."""
    net.replies = [Reply(status=422, body=body, content_type="text/plain")]
    with pytest.raises(RunnerCallError) as failed:
        await call(net)
    assert failed.value.body == (body.decode(errors="replace")[:BODY_LIMIT] or None)
    assert (failed.value.kind, failed.value.status_code) == ("http", 422)


async def test_no_answer_of_300_or_above_is_read_whole(net: Net, monkeypatch: pytest.MonkeyPatch) -> None:
    """``ClientResponse.read()`` holds the whole body, whatever its length. It is never called for the
    402 challenge or for an error answer."""
    whole: list[int] = []
    read = aiohttp.ClientResponse.read

    async def noted(response: aiohttp.ClientResponse) -> bytes:
        whole.append(response.status)
        return await read(response)

    monkeypatch.setattr(aiohttp.ClientResponse, "read", noted)
    net.replies = [Reply(status=500, body=b"trace", content_type="text/plain")]
    with pytest.raises(RunnerCallError) as failed:
        await call(net)
    assert (failed.value.status_code, failed.value.body, failed.value.payment_sent) == (500, "trace", True)
    assert [status for status in whole if status >= 300] == []


async def test_a_payment_challenge_longer_than_the_engine_reads_is_refused_unsigned(net: Net) -> None:
    """A challenge is parsed, so it is read to its end, up to ``CHALLENGE_LIMIT``. A longer body is no
    challenge the engine pays: the attempt fails as a malformed one does, with nothing signed."""
    net.challenge_body = challenge(net, CHALLENGE_LIMIT + 1)
    with pytest.raises(RunnerCallError) as refused:
        await call(net)
    error = refused.value
    assert (error.kind, error.status_code, error.payment_sent) == ("http", 402, False)
    assert str(CHALLENGE_LIMIT) in str(error)
    assert not any(entry.startswith("sign m") for entry in net.log)


async def test_a_payment_challenge_as_long_as_the_engine_reads_is_paid(net: Net) -> None:
    """Sixty-four KiB is many times a real challenge (about one KiB from livepeer/node). Up to the limit
    the body is read whole and parsed as before."""
    net.challenge_body = challenge(net, CHALLENGE_LIMIT)
    reply = await call(net)
    assert (reply.status_code, reply.payment_sent, reply.manifest_ids) == (200, True, ("m-1",))
    assert "sign m-1" in net.log


# Bytes that often start, continue or break a UTF-8 character, so a cut often lands inside one.
utf8_like = st.lists(
    st.one_of(
        st.sampled_from([b"a", b"\x80", b"\xbf", b"\xc3", b"\xe2", b"\xed", b"\xf0", b"\xf4", b"\xff"]),
        st.characters().map(lambda character: character.encode("utf-8", "surrogatepass")),
        st.binary(max_size=3),
    ),
    max_size=40,
).map(b"".join)


@given(body=utf8_like, characters=st.integers(min_value=0, max_value=12))
def test_the_bytes_read_of_an_error_hold_every_character_that_is_kept(body: bytes, characters: int) -> None:
    """The rule the bound rests on, for every body: the first ``n`` characters of a body read whole are
    the first ``n`` characters of its first ``bytes_holding(n)`` bytes."""
    start = body[: bytes_holding(characters)]
    assert start.decode(errors="replace")[:characters] == body.decode(errors="replace")[:characters]
