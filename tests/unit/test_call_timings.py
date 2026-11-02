"""A runner call says where its time went before the runner answered (#89): the unpaid request until
its 402, the signer, and the request the runner answered. Run against a loopback runner and signer."""

import logging
from collections.abc import AsyncIterator

import pytest

from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.contracts import JobRequest
from livepeer_builder.errors import RunnerCallError
from tests.unit.livepeer_net import Net, Reply, start

APP = "example/echo"
SIGNER_SECONDS = 0.05
RUNNER_SECONDS = 0.08
SIGNER_MS = 50
RUNNER_MS = 80


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        yield value


async def _signed(manifest_id: str) -> None:
    return None


def _request(**changes: object) -> JobRequest:
    return JobRequest(app=APP, path="/run", payload={"x": 1}, **changes)  # type: ignore[arg-type]


async def test_a_paid_call_times_each_step_separately(net: Net) -> None:
    net.sign = [SIGNER_SECONDS]
    net.replies = [Reply(hang_s=RUNNER_SECONDS)]
    reply = await SdkRunnerTransport(net.url).call(net.runner(), _request(), "lpg_k", on_signed=_signed)
    assert reply.timings is not None
    assert reply.timings.challenge_ms is not None
    assert reply.timings.signing_ms is not None and reply.timings.signing_ms >= SIGNER_MS
    assert reply.timings.response_ms is not None and reply.timings.response_ms >= RUNNER_MS
    assert reply.timings.challenge_ms < SIGNER_MS, "the signer's wait isn't the challenge's"


async def test_a_stream_knows_its_timings_once_it_opens(net: Net) -> None:
    net.sign = [SIGNER_SECONDS]
    net.replies = [Reply(chunks=(b"a", b"b"))]
    stream = await SdkRunnerTransport(net.url).open(net.runner(), _request(), "lpg_k", on_signed=_signed)
    try:
        assert stream.timings.signing_ms is not None and stream.timings.signing_ms >= SIGNER_MS
        assert stream.timings.challenge_ms is not None
        assert stream.timings.response_ms is not None
    finally:
        await stream.aclose()


async def test_an_unpaid_call_has_only_a_response_time(net: Net) -> None:
    net.paid = False
    reply = await SdkRunnerTransport(net.url).call(net.runner(), _request(), None)
    assert reply.timings is not None
    assert reply.timings.challenge_ms is None
    assert reply.timings.signing_ms is None
    assert reply.timings.response_ms is not None


async def test_a_refused_signature_keeps_what_was_measured(net: Net) -> None:
    net.sign = [403]
    with pytest.raises(RunnerCallError) as raised:
        await SdkRunnerTransport(net.url).call(net.runner(), _request(), "lpg_k", on_signed=_signed)
    timings = raised.value.timings
    assert timings is not None
    assert timings.challenge_ms is not None
    assert timings.signing_ms is not None
    assert timings.response_ms is None, "no paid request went out"


async def test_a_runner_error_after_payment_keeps_its_timings(net: Net) -> None:
    net.replies = [Reply(status=500, body=b"boom")]
    with pytest.raises(RunnerCallError) as raised:
        await SdkRunnerTransport(net.url).call(net.runner(), _request(), "lpg_k", on_signed=_signed)
    timings = raised.value.timings
    assert timings is not None
    assert timings.challenge_ms is not None
    assert timings.signing_ms is not None
    assert timings.response_ms is not None


async def test_a_call_that_keeps_being_challenged_adds_up_each_step(net: Net) -> None:
    net.challenge_paid = True
    net.sign = [SIGNER_SECONDS, SIGNER_SECONDS]
    with pytest.raises(RunnerCallError) as raised:
        await SdkRunnerTransport(net.url, max_payment_challenge_retries=0).call(
            net.runner(), _request(), "lpg_k", on_signed=_signed
        )
    timings = raised.value.timings
    assert timings is not None
    assert timings.signing_ms is not None and timings.signing_ms >= SIGNER_MS
    assert timings.response_ms is None, "only 402s came back"


async def test_each_call_logs_its_timings(net: Net, caplog: pytest.LogCaptureFixture) -> None:
    net.sign = [SIGNER_SECONDS]
    with caplog.at_level(logging.INFO, logger="livepeer_builder.adapters.runner_call"):
        await SdkRunnerTransport(net.url).call(net.runner(), _request(), "lpg_k", on_signed=_signed)
    events = [record for record in caplog.records if getattr(record, "event", "") == "runner.call_timings"]
    assert len(events) == 1
    assert events[0].signing_ms >= SIGNER_MS  # type: ignore[attr-defined]
    assert events[0].response_ms is not None  # type: ignore[attr-defined]
    assert events[0].challenge_ms is not None  # type: ignore[attr-defined]
