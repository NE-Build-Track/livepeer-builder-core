"""Native HTTP (#55) through the engine's own runner call (D53): raw bytes, application headers and
opt-in HTTP errors, run against a loopback runner and signer. No SDK fork is involved: the SDK stays
pinned to upstream, and the engine sends the runner request itself."""

import gzip
from collections.abc import AsyncIterator

import pytest

from livepeer_builder.adapters.runner_call import SdkRunnerStream
from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.contracts import JobRequest
from livepeer_builder.contracts.jobs import RunnerReply
from livepeer_builder.errors import RunnerCallError
from tests.unit.livepeer_net import Net, Reply, start

APP = "example/echo"


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        yield value


async def _signed(manifest_id: str) -> None:
    return None


async def call(net: Net, request: JobRequest) -> RunnerReply:
    return await SdkRunnerTransport(net.url).call(net.runner(), request, "lpg_k", on_signed=_signed)


async def open_(net: Net, request: JobRequest) -> SdkRunnerStream:
    return await SdkRunnerTransport(net.url).open(net.runner(), request, "lpg_k", on_signed=_signed)


async def read(stream: SdkRunnerStream) -> bytes:
    try:
        return b"".join([chunk async for chunk in stream])
    finally:
        await stream.aclose()


@pytest.mark.parametrize("body", [b"", b"\xff\x00", b' {"new":true} '])
async def test_raw_bytes_reach_the_runner_with_their_content_type_on_every_request(
    net: Net, body: bytes
) -> None:
    request = JobRequest(app=APP, path="/run", raw_body=body, content_type="application/x-custom")
    reply = await call(net, request)
    assert reply.status_code == 200 and reply.payment_sent
    assert net.raw == [body, body]  # the unpaid request, then the paid retry: the same bytes
    assert [headers.get("Content-Type") for headers in net.headers] == ["application/x-custom"] * 2


async def test_application_headers_reach_the_runner_beside_the_transport_s_own(net: Net) -> None:
    headers = (("anthropic-version", "2023-06-01"), ("accept", "text/event-stream"))
    await call(net, JobRequest(app=APP, path="/run", payload={"a": 1}, headers=headers))
    unpaid, paid = net.headers
    for seen in (unpaid, paid):
        assert seen.get("anthropic-version") == "2023-06-01"
        assert seen.getall("Accept") == ["text/event-stream"]  # the caller's choice replaces "*/*"
    assert paid.get("Livepeer-Payment") and paid.get("Livepeer-Payer-Address")  # still the transport's


async def test_an_http_error_comes_back_unchanged_after_payment_when_asked(net: Net) -> None:
    net.replies = [Reply(status=400, body=b'{"error":"bad"}', header_pairs=(("X-Trace", "a"),))]
    stream = await open_(net, JobRequest(app=APP, path="/run", payload={"a": 1}, return_http_errors=True))
    assert stream.status_code == 400 and ("X-Trace", "a") in stream.headers
    assert stream.payment_sent and "sign m-1" in net.log  # the 402 was still paid
    assert await read(stream) == b'{"error":"bad"}'


async def test_without_the_opt_in_an_http_error_still_fails_the_call(net: Net) -> None:
    net.replies = [Reply(status=400, body=b"bad")]
    with pytest.raises(RunnerCallError) as failed:
        await open_(net, JobRequest(app=APP, path="/run", payload={"a": 1}))
    assert failed.value.status_code == 400


async def test_a_stream_keeps_repeated_response_headers(net: Net) -> None:
    trace = (("X-Trace", "one"), ("X-Trace", "two"))
    net.replies = [Reply(content_type="text/event-stream", chunks=(b"data: a\n\n",), header_pairs=trace)]
    stream = await open_(net, JobRequest(app=APP, path="/run", payload={}))
    assert [value for name, value in stream.headers if name == "X-Trace"] == ["one", "two"]
    assert await read(stream) == b"data: a\n\n"


async def test_a_returned_error_is_not_decompressed(net: Net) -> None:
    """The application owns the content encoding of what it asked to receive unchanged."""
    compressed = gzip.compress(b'{"error":"bad"}')
    net.replies = [Reply(status=500, body=compressed, header_pairs=(("Content-Encoding", "gzip"),))]
    stream = await open_(net, JobRequest(app=APP, path="/run", payload={}, return_http_errors=True))
    assert net.headers[-1].get("Accept-Encoding") == "identity"
    assert await read(stream) == compressed


async def test_a_signer_refusal_stays_an_error_when_http_errors_are_returned(net: Net) -> None:
    """Payment is control flow, never a response for the caller (#55)."""
    net.sign = [403]
    with pytest.raises(RunnerCallError) as failed:
        await open_(net, JobRequest(app=APP, path="/run", payload={"a": 1}, return_http_errors=True))
    assert failed.value.kind == "payment" and not failed.value.payment_sent
    assert not any(entry.startswith("request ") for entry in net.log)


@pytest.mark.parametrize("method", ["PUT", "DELETE", "GET"])
async def test_a_paid_request_is_never_sent_twice_on_a_dropped_connection(net: Net, method: str) -> None:
    """aiohttp resends an idempotent request once when a reused connection drops. A paid request
    carries a payment, so the engine never lets it go out a second time."""
    net.replies = [Reply(drop_before_headers=True)]
    with pytest.raises(RunnerCallError) as failed:
        await call(net, JobRequest(app=APP, path="/run", method=method, payload={"a": 1}))
    assert failed.value.payment_sent
    assert [entry for entry in net.log if entry.startswith("request ")] == ["request pay-m-1"]
