"""ISS-26, #184 review: ``LPB_ASYNC_RESULT_MAX_BYTES`` bounds what the worker holds of a runner's reply.

The limit was applied after the transport had read the whole reply into memory, so a runner could make
the engine hold any number of bytes for a job nobody waits for. A transport that is a
``BoundedRunnerTransport`` reads the reply in pieces for an async job: it counts and hashes all of it and
keeps at most the limit. The first tests run the SDK transport against a loopback runner and signer; the
rest run the worker over a scripted transport that has ``call_bounded``. An immediate job is read as it
always was.
"""

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import pytest

from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.contracts import ActorContext, JobRequest, ResultRef, Runner, RunnerReply
from livepeer_builder.errors import JobFailed, RunnerCallError
from livepeer_builder.protocols import BoundedRunnerTransport, OnSigned
from livepeer_builder.testing import ScriptedStreamTransport, ScriptedTransport
from livepeer_builder.testing.fakes import report_signed
from tests.conftest import EngineFactory, allot
from tests.unit.livepeer_net import Net, Reply, start
from tests.unit.test_async_jobs import APP, queued

PIECES = (b"a" * 40, b"b" * 40, b"c" * 20)  # a reply of 100 bytes, sent in three pieces
WHOLE = b"".join(PIECES)


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        yield value


async def bounded(net: Net, max_bytes: int) -> RunnerReply:
    request = JobRequest(app=APP, path="/run", payload={"a": 1})
    return await SdkRunnerTransport(net.url).call_bounded(net.runner(), request, "lpg_k", max_bytes=max_bytes)


# -- the SDK transport -------------------------------------------------------------------------------------


def test_the_sdk_transport_is_a_bounded_transport_and_the_scripted_one_is_not() -> None:
    """The port is optional, as ``open`` is (D33): a transport without it is called as before."""
    assert isinstance(SdkRunnerTransport(None), BoundedRunnerTransport)
    assert not isinstance(ScriptedStreamTransport(), BoundedRunnerTransport)


@pytest.mark.parametrize("max_bytes", [100, 4096])
async def test_a_bounded_call_keeps_a_reply_within_the_bound_whole(net: Net, max_bytes: int) -> None:
    net.replies = [Reply(content_type="image/png", chunks=PIECES)]
    reply = await bounded(net, max_bytes)
    assert (reply.status_code, reply.content, reply.content_type) == (200, WHOLE, "image/png")
    assert (reply.size_bytes, reply.sha256) == (100, hashlib.sha256(WHOLE).hexdigest())
    assert reply.payment_sent and reply.manifest_ids == ("m-1",)
    assert reply.timings is not None


@pytest.mark.parametrize("max_bytes", [99, 40, 1, 0])
async def test_a_bounded_call_keeps_nothing_of_a_longer_reply_and_measures_all_of_it(
    net: Net, max_bytes: int
) -> None:
    net.replies = [Reply(content_type="image/png", chunks=PIECES)]
    reply = await bounded(net, max_bytes)
    assert reply.content == b""  # over the bound: none of it is kept
    assert (reply.size_bytes, reply.sha256) == (100, hashlib.sha256(WHOLE).hexdigest())  # of all of it
    assert (reply.status_code, reply.content_type, reply.payment_sent) == (200, "image/png", True)


async def test_a_bounded_call_never_asks_for_the_whole_reply_at_once(
    net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ClientResponse.read()`` holds the whole body, whatever its length: the bounded call reads the
    body piece by piece and never calls it for the runner's answer."""
    read = aiohttp.ClientResponse.read
    whole: list[str] = []

    async def noted(response: aiohttp.ClientResponse) -> bytes:
        whole.append(response.url.path)
        return await read(response)

    monkeypatch.setattr(aiohttp.ClientResponse, "read", noted)
    net.replies = [Reply(chunks=PIECES), Reply(chunks=PIECES)]
    assert (await bounded(net, 10)).size_bytes == 100
    assert "/app/run" in whole  # the 402 challenge of that path: an error's body, read as before
    answers = [entry for entry in net.log if entry.startswith("request ")]
    assert len(answers) == 1 and len([path for path in whole if path == "/app/run"]) == 1

    request = JobRequest(app=APP, path="/run", payload={"a": 1})
    reply = await SdkRunnerTransport(net.url).call(net.runner(), request, "lpg_k")
    assert len([path for path in whole if path == "/app/run"]) == 3  # an immediate call: read at once
    assert (reply.content, reply.size_bytes, reply.sha256) == (WHOLE, None, None)  # and as it always was


async def test_a_bounded_call_does_not_parse_what_it_keeps(net: Net) -> None:
    """``data`` is for a caller that waits. A stored result's is worked out when it is read back."""
    net.replies = [Reply(body=b'{"ok": true}')]
    reply = await bounded(net, 4096)
    assert (reply.content, reply.data, reply.size_bytes) == (b'{"ok": true}', None, 12)


async def test_a_reply_cut_off_part_way_fails_a_bounded_call_as_it_fails_any_call(net: Net) -> None:
    """The failure of the read is the same failure, with the payment kept, however the reply is read."""
    net.replies = [Reply(chunks=PIECES, drop_after_chunks=True), Reply(chunks=PIECES, drop_after_chunks=True)]
    request = JobRequest(app=APP, path="/run", payload={"a": 1})
    with pytest.raises(RunnerCallError) as at_once:
        await SdkRunnerTransport(net.url).call(net.runner(), request, "lpg_k")
    with pytest.raises(RunnerCallError) as in_pieces:
        await bounded(net, 10)
    assert (in_pieces.value.payment_sent, in_pieces.value.manifest_ids) == (True, ("m-2",))
    assert (in_pieces.value.kind, in_pieces.value.status_code) == (
        at_once.value.kind,
        at_once.value.status_code,
    )
    assert at_once.value.payment_sent


async def test_the_jobs_timeout_bounds_a_reply_read_in_pieces(net: Net) -> None:
    """``timeout_s`` is the whole call, the read of the reply included, as it is for ``call``: a runner
    that sends its reply a little at a time does not hold a worker past it."""
    net.replies = [Reply(chunks=(b"x",) * 400)]  # one piece each 10 ms: four seconds of reply
    request = JobRequest(app=APP, path="/run", payload={"a": 1}, timeout_s=1.0)
    with pytest.raises(RunnerCallError) as failed:
        await SdkRunnerTransport(net.url).call_bounded(net.runner(), request, "lpg_k", max_bytes=10)
    assert (failed.value.kind, failed.value.payment_sent) == ("timeout", True)


# -- the worker -------------------------------------------------------------------------------------------


@dataclass
class MeasuresReplies(ScriptedTransport):
    """A scripted transport with ``call_bounded``: it answers as the port says, with what the script
    holds as the runner's whole reply, and notes the bound it was given."""

    bounds: list[int] = field(default_factory=list)

    async def call_bounded(
        self,
        runner: Runner,
        request: JobRequest,
        credential: str | None,
        on_signed: OnSigned | None = None,
        *,
        max_bytes: int,
    ) -> RunnerReply:
        self.bounds.append(max_bytes)
        self.calls.append((runner.url, credential))
        step = self.steps.pop(0)
        if isinstance(step, RunnerCallError):
            await report_signed(on_signed, step.manifest_ids, step.payment_sent)
            raise step
        assert isinstance(step, RunnerReply)
        await report_signed(on_signed, step.manifest_ids, step.payment_sent)
        whole = step.content
        measured: dict[str, Any] = {
            "content": whole if len(whole) <= max_bytes else b"",
            "data": None,
            "size_bytes": len(whole),
            "sha256": hashlib.sha256(whole).hexdigest(),
        }
        return step.model_copy(update=measured)


@pytest.fixture
def transport() -> MeasuresReplies:
    return MeasuresReplies()


async def test_the_worker_asks_a_bounded_transport_to_keep_no_more_than_the_limit(
    make_engine: EngineFactory, alice: ActorContext, transport: MeasuresReplies
) -> None:
    engine = await make_engine(async_result_max_bytes=64)
    await allot(engine, alice)
    big = bytes(range(65))
    transport.steps = [ScriptedTransport.ok(big), ScriptedTransport.ok(bytes(64))]

    over = await engine.jobs.submit(alice, queued())
    assert await engine.jobs.worker.run_once() == 1
    assert transport.bounds == [64]  # LPB_ASYNC_RESULT_MAX_BYTES, given to the transport before it reads
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.result(alice, over.id)
    job = failed.value.job
    assert job.failure is not None
    assert (job.state, job.failure.kind, job.failure.payment_sent) == ("failed", "result_not_stored", True)
    assert "65 bytes" in job.failure.message and "64" in job.failure.message
    assert job.result == ResultRef(  # the true size and hash of a reply the engine never held whole
        storage="stored",
        content_type="application/json",
        size_bytes=65,
        sha256=hashlib.sha256(big).hexdigest(),
    )
    assert [(a.outcome, a.payment_sent) for a in job.attempts] == [("succeeded", True)]
    assert await engine.store.read_result(over.id, engine.clock.now()) is None

    within = await engine.jobs.submit(alice, queued())
    assert await engine.jobs.worker.run_once() == 1
    kept = await engine.jobs.result(alice, within.id)
    assert kept.content == bytes(64)  # at the limit: stored whole
    assert kept.job.result is not None
    assert (kept.job.result.size_bytes, kept.job.result.sha256) == (64, hashlib.sha256(bytes(64)).hexdigest())
    assert transport.bounds == [64, 64]


async def test_an_async_job_on_a_bounded_transport_fails_over_as_any_job_does(
    make_engine: EngineFactory, alice: ActorContext, transport: MeasuresReplies
) -> None:
    engine = await make_engine(async_result_max_bytes=64)
    await allot(engine, alice)
    transport.steps = [RunnerCallError("refused", "busy", payment_sent=False)]

    job = await engine.jobs.submit(alice, queued())
    assert await engine.jobs.worker.run_once() == 1
    ended = await engine.jobs.get(alice, job.id)
    assert ended.failure is not None
    assert (ended.state, ended.failure.kind, ended.failure.payment_sent) == ("failed", "refused", False)
    assert transport.bounds == [64]


async def test_an_immediate_job_is_called_as_before_on_a_bounded_transport(
    make_engine: EngineFactory, alice: ActorContext, transport: MeasuresReplies
) -> None:
    """The bound is the async worker's. An immediate job's caller waits for the whole reply."""
    engine = await make_engine(async_result_max_bytes=64)
    await allot(engine, alice)
    big = bytes(range(65))
    transport.steps = [ScriptedTransport.ok(big)]

    result = await engine.jobs.run(alice, JobRequest(app=APP))

    assert transport.bounds == []  # call, never call_bounded
    assert result.content == big and result.job.result is not None
    assert (result.job.result.storage, result.job.result.size_bytes) == ("inline", 65)
