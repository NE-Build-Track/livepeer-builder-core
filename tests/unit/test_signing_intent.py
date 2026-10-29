"""D53, D54: the manifest is stored as ``unknown`` before the signer is asked, then ``paid`` or ``unpaid``.

``on_signed`` makes it ``paid``. An expired challenge and a signer refusal make it ``unpaid``, and usage
for an unpaid manifest is never attributed. A lost reply and a cancel leave it ``unknown``, so a crash
during signing still has it. The engine, the Live Runner transport and the SDK's signer client
are real; only the network is local (tests/unit/livepeer_net.py). The job passes the recorder as
``on_signed``; nothing passes a second callback.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from decimal import Decimal
from pathlib import Path

import pytest

from livepeer_builder import EngineSupply
from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.contracts import ActorContext, Attempt, Job, JobRequest, Money, ProvisionRequest
from livepeer_builder.errors import JobFailed
from livepeer_builder.runtime import BuilderEngine
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FakeUsageSource
from livepeer_builder.utils import _SHIELDED
from tests.conftest import EngineFactory
from tests.unit.livepeer_net import Net, Reply, start
from tests.unit.test_costs import row


def states(attempt: Attempt) -> Sequence[tuple[str, str]]:
    return [(session.manifest_id, session.payment_state) for session in attempt.sessions]


async def settled(store: SqlStore) -> Job:
    """The job once it ended and every shielded write finished: the close runs in a task (D34)."""
    ended: list[Job] = []
    for _ in range(100):
        ended = [job for state in ("failed", "uncertain") for job in await store.jobs_in_state(state)]
        if ended and not _SHIELDED:
            break
        await asyncio.sleep(0.01)
    ended = [job for state in ("failed", "uncertain") for job in await store.jobs_in_state(state)]
    assert ended, "the job never ended"
    return ended[0]


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqlStore]:
    """A file database, not the shared in-memory connection: these tests overlap evidence writes, and
    only separate connections keep one write's rollback from undoing another's, as in production."""
    opened = await SqlStore.connect(f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}")
    yield opened
    await opened.close()


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        yield value


async def paid_engine(
    make_engine: EngineFactory,
    admin: ActorContext,
    net: Net,
    *,
    retries: int = 3,
    signer_url: str | None = None,
) -> tuple[BuilderEngine, str]:
    """Two runners on two orchestrators; the signer is ``net`` unless ``signer_url`` names another."""
    runners = (net.runner(name="a1"), net.runner(name="b1", orchestrator_url="https://orch-b.example"))
    transport = SdkRunnerTransport(
        signer_url or net.url,
        max_payment_challenge_retries=retries,
    )
    engine = await make_engine(
        runners=runners,
        supply=EngineSupply(
            transport=transport,
        ),
    )
    request = ProvisionRequest(
        operation_ref="provision:default:alice",
        application_id="default",
        actor_id="alice",
        amount=Money(amount=Decimal(5), currency="USD"),
        grant_id="g",
    )
    actor = await engine.payments.provision(admin, request)
    return engine, actor.allocation_id


@pytest.mark.parametrize("step", [503, "drop", "malformed"])
async def test_a_signer_that_gives_no_answer_keeps_the_manifest_and_an_uncertain_job(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    net: Net,
    usage: FakeUsageSource,
    step: int | str,
) -> None:
    engine, allocation = await paid_engine(make_engine, admin, net)
    net.sign = [step]
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app="example/echo"))
    job = failed.value.job
    assert (
        job.state == "uncertain" and len(job.attempts) == 1
    )  # the signer may have charged: no one else is paid
    stored = await engine.jobs.get(alice, job.id)
    attempt = stored.attempts[0]
    assert attempt.outcome == "payment_unknown"
    assert states(attempt) == [("m-1", "unknown")] and not attempt.payment_sent
    assert (await engine.costs.for_job(alice, job.id)).status == "pending"  # until usage says

    usage.rows = [row("u1", "s1", "0.002", usd="0.2", allocation=allocation, manifest="m-1")]
    await engine.costs.sync_once()
    cost = await engine.costs.for_job(alice, job.id)
    assert cost.status == "observed" and cost.fees.fee_eth == Decimal("0.002")
    assert cost.attempts[0].cost_class == "uncertain" and cost.billable.fee_eth == 0
    confirmed = await engine.jobs.get(alice, job.id)
    assert states(confirmed.attempts[0]) == [("m-1", "paid")]  # unique usage confirms the payment


async def test_a_refused_manifest_is_stored_unpaid_and_never_billed(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    net: Net,
    usage: FakeUsageSource,
) -> None:
    engine, allocation = await paid_engine(make_engine, admin, net)
    net.sign = [481]  # this orchestrator's price is above the ceiling: another orchestrator may succeed
    result = await engine.jobs.run(alice, JobRequest(app="example/echo"))
    job = await engine.jobs.get(alice, result.job.id)
    assert [(a.outcome, a.payment_sent) for a in job.attempts] == [
        ("payment_refused", False),
        ("succeeded", True),
    ]
    assert states(job.attempts[0]) == [("m-1", "unpaid")]  # stored before signing, refused
    assert states(job.attempts[1]) == [("m-2", "paid")]
    waiting = await engine.costs.for_job(alice, job.id)
    assert waiting.status == "pending"  # the signed session has no usage yet
    assert [a.cost_class for a in waiting.attempts] == ["pending"]  # the refused attempt costs nothing
    assert waiting.billable.fee_eth == 0

    usage.rows = [row("u1", "s1", "0.002", usd="0.2", allocation=allocation, manifest="m-1")]
    await engine.costs.sync_once()
    matched = await engine.costs.for_job(alice, job.id)
    assert matched.status == "pending"
    assert [a.cost_class for a in matched.attempts] == ["pending"]
    assert matched.fees.fee_eth == 0 and matched.billable.fee_eth == 0
    unattributed = await engine.store.unattributed_usage(allocation)
    assert [item.computed_fee_eth for item in unattributed] == [Decimal("0.002")]


async def test_a_cancel_during_signing_keeps_the_manifest_unknown(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, net: Net, store: SqlStore
) -> None:
    net.sign = [30.0]  # the signer accepts the request and does not answer
    engine, _ = await paid_engine(make_engine, admin, net)
    task = asyncio.create_task(engine.jobs.run(alice, JobRequest(app="example/echo")))
    for _ in range(200):
        if "sign m-1" in net.log:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("the signer was not asked")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(2):
            await task
    [job] = await store.jobs_in_state("uncertain")
    assert states(job.attempts[0]) == [("m-1", "unknown")]
    assert not job.attempts[0].payment_sent


async def test_an_expired_manifest_is_stored_unpaid_and_never_billed(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    net: Net,
    usage: FakeUsageSource,
) -> None:
    engine, allocation = await paid_engine(make_engine, admin, net)
    net.sign = [480]  # params expired: the same attempt asks for a fresh challenge and pays that
    result = await engine.jobs.run(alice, JobRequest(app="example/echo"))
    job = await engine.jobs.get(alice, result.job.id)
    assert [(a.outcome, a.payment_sent) for a in job.attempts] == [("succeeded", True)]
    assert states(job.attempts[0]) == [("m-1", "unpaid"), ("m-2", "paid")]
    usage.rows = [
        row("u1", "s1", "0.001", usd="0.1", allocation=allocation, manifest="m-1"),
        row("u2", "s2", "0.004", usd="0.4", allocation=allocation, manifest="m-2"),
    ]
    await engine.costs.sync_once()
    cost = await engine.costs.for_job(alice, result.job.id)
    assert [(attempt.cost_class, attempt.fees.fee_eth) for attempt in cost.attempts] == [
        ("delivered", Decimal("0.004"))
    ]
    assert cost.fees.fee_eth == Decimal("0.004") and cost.billable.fee_eth == Decimal("0.004")
    unattributed = await engine.store.unattributed_usage(allocation)
    assert [item.computed_fee_eth for item in unattributed] == [Decimal("0.001")]


async def test_a_signed_session_without_usage_keeps_the_job_pending(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    net: Net,
    usage: FakeUsageSource,
) -> None:
    engine, allocation = await paid_engine(make_engine, admin, net)
    net.sign = [480]
    result = await engine.jobs.run(alice, JobRequest(app="example/echo"))
    usage.rows = [row("u1", "s1", "0.001", usd="0.1", allocation=allocation, manifest="m-1")]
    await engine.costs.sync_once()
    waiting = await engine.costs.for_job(alice, result.job.id)
    assert waiting.status == "pending"  # the signed payment has not shown up: not observed at 0
    assert [(attempt.cost_class, attempt.fees.fee_eth) for attempt in waiting.attempts] == [
        ("pending", Decimal(0))
    ]
    assert waiting.billable.fee_eth == 0

    usage.rows.append(row("u2", "s2", "0.004", usd="0.4", allocation=allocation, manifest="m-2"))
    await engine.costs.sync_once()
    cost = await engine.costs.for_job(alice, result.job.id)
    assert [(attempt.cost_class, attempt.fees.fee_eth) for attempt in cost.attempts] == [
        ("delivered", Decimal("0.004"))
    ]
    assert cost.status == "observed"
    assert cost.fees.fee_eth == Decimal("0.004") and cost.billable.fee_eth == Decimal("0.004")


async def test_a_refreshed_stream_stores_the_expired_manifest_unpaid(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, net: Net
) -> None:
    engine, _allocation = await paid_engine(make_engine, admin, net)
    net.sign = [480]
    net.replies = [Reply(content_type="text/event-stream", chunks=(b"data: a\n\n",))]
    streaming = engine.jobs.streaming
    assert streaming is not None
    stream = await streaming.stream(alice, JobRequest(app="example/echo"))
    assert b"".join([chunk async for chunk in stream]) == b"data: a\n\n"
    assert stream.job.state == "succeeded"
    assert states(stream.job.attempts[0]) == [("m-1", "unpaid"), ("m-2", "paid")]


async def test_an_exhausted_refresh_fails_over_to_the_other_orchestrator(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, net: Net
) -> None:
    engine, _allocation = await paid_engine(make_engine, admin, net, retries=1)
    net.sign = [480, 480]  # one in-loop refresh, then the budget is spent
    result = await engine.jobs.run(alice, JobRequest(app="example/echo"))
    job = await engine.jobs.get(alice, result.job.id)
    assert [(a.outcome, a.payment_sent) for a in job.attempts] == [
        ("payment_refused", False),
        ("succeeded", True),
    ]
    assert states(job.attempts[0]) == [("m-1", "unpaid"), ("m-2", "unpaid")]
    assert states(job.attempts[1]) == [("m-3", "paid")]


async def test_the_manifest_is_stored_before_the_signer_is_asked(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, net: Net, store: SqlStore
) -> None:
    """While the signer is still answering, the database already holds the manifest (D54): a crash now
    leaves an ``unknown`` intent for cost sync, not a charge with nothing to match it."""
    net.sign = [30.0]  # the signer accepts the request and does not answer
    engine, _ = await paid_engine(make_engine, admin, net)
    task = asyncio.create_task(engine.jobs.run(alice, JobRequest(app="example/echo")))
    try:
        for _ in range(200):
            if "sign m-1" in net.log:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("the signer was not asked")
        [job] = await store.jobs_in_state("running")  # what a crash right now would leave
        assert states(job.attempts[0]) == [("m-1", "unknown")]
        assert not job.attempts[0].payment_sent
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(2):
                await task


async def test_a_failed_intent_write_signs_nothing(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    net: Net,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed (D54): no manifest on record, no signing. The attempt is unpaid, so the job is
    ``failed``, not ``uncertain``, and no other orchestrator is tried."""
    engine, _ = await paid_engine(make_engine, admin, net)

    async def database_down(attempt: Attempt) -> None:
        raise RuntimeError("database is down")

    monkeypatch.setattr(store, "record_payment_evidence", database_down)
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app="example/echo"))
    job = failed.value.job
    assert job.state == "failed"
    assert [(a.outcome, a.payment_sent) for a in job.attempts] == [("payment", False)]
    assert not any(entry.startswith("sign ") for entry in net.log)
    stored = await engine.jobs.get(alice, job.id)
    assert states(stored.attempts[0]) == []  # no intent was stored, so none is kept
    assert (await engine.costs.for_job(alice, job.id)).status == "none"  # never pending forever


async def test_a_cancel_during_the_intent_write_stores_the_manifest_unpaid(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    net: Net,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write cannot be cancelled half way, so the intent may land. The signer was never asked, so
    the manifest is then stored ``unpaid``: the job is ``failed`` with no cost, never ``pending``
    forever (#64 review)."""
    engine, _ = await paid_engine(make_engine, admin, net)
    writing, release = asyncio.Event(), asyncio.Event()
    write = store.record_payment_evidence

    async def slow_first_write(attempt: Attempt) -> None:
        if not writing.is_set():
            writing.set()
            await release.wait()
        await write(attempt)

    monkeypatch.setattr(store, "record_payment_evidence", slow_first_write)
    task = asyncio.create_task(engine.jobs.run(alice, JobRequest(app="example/echo")))
    async with asyncio.timeout(2):
        await writing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(2):
            await task
    for _ in range(100):  # the close ends the job before the intent write lands, which overwrites it
        if not await store.jobs_in_state("running"):
            break
        await asyncio.sleep(0.01)
    release.set()
    job = await settled(store)
    assert job.state == "failed"
    assert states(job.attempts[0]) == [("m-1", "unpaid")] and not job.attempts[0].payment_sent
    assert not any(entry.startswith("sign ") for entry in net.log)
    assert (await engine.costs.for_job(alice, job.id)).status == "none"


async def test_a_first_byte_timeout_during_the_intent_write_stores_the_manifest_unpaid(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    net: Net,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The open deadline cancels the call while the intent is being stored, before the signer is asked.
    As for a cancel, the manifest is ``unpaid`` and the cost ``none`` (#64 review)."""
    engine, _ = await paid_engine(make_engine, admin, net)
    writing = asyncio.Event()
    write = store.record_payment_evidence

    async def slow_first_write(attempt: Attempt) -> None:
        if not writing.is_set():
            writing.set()
            await asyncio.sleep(0.3)  # past the first-byte deadline
        await write(attempt)

    monkeypatch.setattr(store, "record_payment_evidence", slow_first_write)
    streaming = engine.jobs.streaming
    assert streaming is not None
    with pytest.raises(JobFailed):
        await streaming.stream(alice, JobRequest(app="example/echo", first_byte_timeout_s=0.1))
    job = await settled(store)
    assert job.state == "failed"
    assert [(a.outcome, a.payment_sent) for a in job.attempts] == [("timeout", False)]
    assert states(job.attempts[0]) == [("m-1", "unpaid")]
    assert not any(entry.startswith("sign ") for entry in net.log)
    assert (await engine.costs.for_job(alice, job.id)).status == "none"


async def test_a_cancel_while_storing_a_refusal_leaves_the_job_failed(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    net: Net,
    store: SqlStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The signer refused, and the caller went away while ``unpaid`` was being stored. Nothing was paid,
    so the job is ``failed``, not ``uncertain``, and the signer error is still logged (#64 review)."""
    engine, _ = await paid_engine(make_engine, admin, net)
    net.sign = [403]  # the signer refuses this payer
    writing, release = asyncio.Event(), asyncio.Event()
    write = store.record_payment_evidence

    async def slow_refusal(attempt: Attempt) -> None:
        if any(s.payment_state == "unpaid" for s in attempt.sessions) and not writing.is_set():
            writing.set()
            await release.wait()
        await write(attempt)

    monkeypatch.setattr(store, "record_payment_evidence", slow_refusal)
    caplog.set_level(logging.WARNING, logger="livepeer_builder.adapters.runner_call")
    task = asyncio.create_task(engine.jobs.run(alice, JobRequest(app="example/echo")))
    async with asyncio.timeout(2):
        await writing.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(2):
            await task
    for _ in range(100):  # the close runs in a task the caller cannot cancel (D34)
        if not await store.jobs_in_state("running"):
            break
        await asyncio.sleep(0.01)
    assert not await store.jobs_in_state("uncertain")
    [job] = await store.jobs_in_state("failed")
    assert states(job.attempts[0]) == [("m-1", "unpaid")] and not job.attempts[0].payment_sent
    [logged] = [record for record in caplog.records if record.message == "signer error"]
    assert logged.manifest_id == "m-1"
