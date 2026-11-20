"""ISS-26: async jobs across a restart and a stop, on SQLite and PostgreSQL.

Each test opens an engine on a database, leaves jobs behind as a process that died would, and opens a
second engine on the same database. ``start()`` resolves what was left and then starts the worker's
loop, so these tests run the real loop: they wait for a condition, with a bound, never for a length of
time (docs/design-docs/m4-guides/iss-26-async-jobs.md, "Crash and disconnect").
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from livepeer_builder import BuilderEngine, EngineSupply, generate_secret_key
from livepeer_builder.contracts import (
    ActorContext,
    AllotRequest,
    JobRequest,
    Money,
    PaymentKey,
    Runner,
    RunnerReply,
)
from livepeer_builder.protocols import OnSigned
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FixedClock, ScriptedStreamTransport, ScriptedTransport, make_engine
from livepeer_builder.testing.fakes import report_signed
from tests.store.conftest import make_attempt

ADMIN = ActorContext(actor_id="operator", credential_kind="clearinghouse_admin")
ALICE = ActorContext(actor_id="alice", permissions=frozenset({"jobs.run", "jobs.read", "costs.read"}))
APP = "example/echo"
SECRET_KEY = generate_secret_key()  # one for every engine of a test: the second reads the first's records


def queued(n: int = 1, **fields: object) -> JobRequest:
    return JobRequest.model_validate({"app": APP, "mode": "async", "payload": {"n": n}, **fields})


def echo(_: Runner, request: JobRequest, __: str | None) -> RunnerReply:
    return ScriptedTransport.ok(json.dumps(request.payload).encode())


@asynccontextmanager
async def engine_on(
    database_url: str, clock: FixedClock, transport: ScriptedTransport, **settings: Any
) -> AsyncIterator[BuilderEngine]:
    """An engine on ``database_url``, not started. Leaving the block stops it, as a process ends."""
    supply = settings.pop("supply", EngineSupply())
    supply = EngineSupply(**{**vars(supply), "clock": clock, "transport": transport})
    settings.setdefault("async_poll_s", 0.01)
    async with make_engine(
        database_url=database_url, secret_key=SECRET_KEY, supply=supply, **settings
    ) as engine:
        yield engine


async def allot(engine: BuilderEngine) -> None:
    amount = Money(amount=Decimal(5), currency="USD")
    request = AllotRequest(operation_ref="allot-alice", tenant_id="default", actor_id="alice", amount=amount)
    await engine.payments.allot(ADMIN, request)


async def until(condition: Callable[[], Any], what: str) -> None:
    """Wait for the running engine to get somewhere: at most five seconds, then the test fails."""
    for _ in range(500):
        found = condition()
        if asyncio.iscoroutine(found):
            found = await found
        if found:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting until {what}")


async def claims(engine: BuilderEngine, job_id: object) -> int:
    async with engine.store.engine.connect() as conn:  # type: ignore[attr-defined]
        found = await conn.execute(select(t.jobs.c.claims).where(t.jobs.c.id == str(job_id)))
        return int(found.scalar_one())


async def states(engine: BuilderEngine, *job_ids: Any) -> list[str]:
    found = [await engine.store.get_job(job_id) for job_id in job_ids]
    return [job.state if job else "missing" for job in found]


async def test_a_queued_job_and_one_claimed_with_no_attempt_each_run_once_after_a_restart(
    database_url: str, clock: FixedClock
) -> None:
    before, after = ScriptedStreamTransport(), ScriptedStreamTransport(steps=[echo, echo])
    async with engine_on(database_url, clock, before) as first:
        await allot(first)
        waiting = await first.jobs.submit(ALICE, queued(1))
        clock.advance(1)
        claimed = await first.jobs.submit(ALICE, queued(2))
        # the process dies after a worker claimed the second job and before its first attempt was written
        assert await first.store.claim_job(claimed.id, clock.now()) is not None
    assert before.calls == []

    async with engine_on(database_url, clock, after) as second:
        assert await states(second, waiting.id, claimed.id) == ["queued", "running"]
        await second.start()
        await until(lambda: len(after.calls) == 2 and second.admission.running == 0, "both jobs ran")
        await until(lambda: second.health().queued_jobs == 0, "the queue is empty")
        assert await states(second, waiting.id, claimed.id) == ["succeeded", "succeeded"]
        assert await second.store.read_result(waiting.id, clock.now()) == b'{"n": 1}'
        assert await second.store.read_result(claimed.id, clock.now()) == b'{"n": 2}'
        assert (await claims(second, waiting.id), await claims(second, claimed.id)) == (1, 2)
        ran = await second.store.get_job(claimed.id)
        assert ran is not None and [a.number for a in ran.attempts] == [1]  # dispatched once
    assert len(after.calls) == 2  # and nothing ran again while the engine stopped


async def test_a_job_with_a_paid_attempt_in_flight_is_uncertain_after_a_restart_and_never_run_again(
    database_url: str, clock: FixedClock
) -> None:
    after = ScriptedStreamTransport(steps=[echo])
    async with engine_on(database_url, clock, ScriptedStreamTransport()) as first:
        await allot(first)
        job = await first.jobs.submit(ALICE, queued(operation_ref="order-1"))
        assert await first.store.claim_job(job.id, clock.now()) is not None
        in_flight = make_attempt(job.id, clock, manifest_ids=("m-1",)).model_copy(
            update={"outcome": "in_flight", "ended_at": None, "status_code": None}
        )
        await first.store.start_attempt(in_flight)  # written before the call; then the process dies
        await first.store.record_payment_evidence(in_flight)

    async with engine_on(database_url, clock, after) as second:
        await second.start()
        left = await second.jobs.get(ALICE, job.id)
        assert left.failure is not None
        assert (left.state, left.failure.kind, left.failure.payment_sent) == (
            "uncertain",
            "interrupted",
            True,
        )
        await asyncio.sleep(0.05)  # several turns of the worker's loop: it has nothing to claim
        assert after.calls == [] and (await second.jobs.get(ALICE, job.id)).state == "uncertain"
        assert await claims(second, job.id) == 1
        # the caller's repeat is answered with that job; nothing is queued in its place (D69 (2))
        assert (await second.jobs.submit(ALICE, queued(operation_ref="order-1"))).id == job.id
        assert second.health().queued_jobs == 0
        async with second.store.engine.connect() as conn:  # type: ignore[attr-defined]
            assert (await conn.execute(select(t.job_requests))).first() is None  # its request went with it


async def test_a_job_claimed_three_times_without_an_attempt_fails_interrupted(
    database_url: str, clock: FixedClock
) -> None:
    """A job that brings the engine down before its first attempt would be claimed at every start.
    The third claim is the last: it ends ``failed``, unpaid, and its reference may be queued again."""
    after = ScriptedStreamTransport(steps=[echo])
    async with engine_on(database_url, clock, ScriptedStreamTransport()) as first:
        await allot(first)
        job = await first.jobs.submit(ALICE, queued(operation_ref="order-1"))

    for claim in (1, 2):  # two restarts, each after a claim that wrote no attempt
        async with engine_on(database_url, clock, ScriptedStreamTransport()) as crashing:
            assert await crashing.store.claim_job(job.id, clock.now()) is not None
            assert await claims(crashing, job.id) == claim
            assert await crashing.jobs.recover_interrupted() == 0  # back in the queue: nothing was resolved
            assert await states(crashing, job.id) == ["queued"]

    async with engine_on(database_url, clock, after) as last:
        assert await last.store.claim_job(job.id, clock.now()) is not None  # the third claim
        await last.start()
        dead = await last.jobs.get(ALICE, job.id)
        assert dead.failure is not None
        assert (dead.state, dead.failure.kind, dead.failure.payment_sent, dead.attempts) == (
            "failed",
            "interrupted",
            False,
            (),
        )
        assert after.calls == []
        again = await last.jobs.submit(ALICE, queued(operation_ref="order-1"))  # it never reached a runner
        assert again.id != job.id
        await until(lambda: len(after.calls) == 1 and last.admission.running == 0, "the new job ran")
        assert await states(last, again.id) == ["succeeded"]


class PaysThenWaits(ScriptedStreamTransport):
    """A runner call that is paid for and then takes as long as the test lets it."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def call(
        self, runner: Runner, request: JobRequest, credential: str | None, on_signed: OnSigned | None = None
    ) -> RunnerReply:
        self.calls.append((runner.url, credential))
        await report_signed(on_signed, ("m-1",), True)
        await self.release.wait()
        return ScriptedTransport.ok(b"late", manifest_ids=("m-1",))


async def test_stop_waits_for_a_running_job_and_one_still_running_after_that_ends_uncertain(
    database_url: str, clock: FixedClock
) -> None:
    quick, slow = PaysThenWaits(), PaysThenWaits()
    async with engine_on(database_url, clock, quick, async_drain_timeout_s=5) as drained:
        await allot(drained)
        await drained.start()
        finished = await drained.jobs.submit(ALICE, queued(1))
        await until(lambda: len(quick.calls) == 1, "the job is at the runner")
        stopping = asyncio.create_task(drained.stop())
        await asyncio.sleep(0.05)
        assert not stopping.done()  # stop waits for the job
        refused = await drained.jobs.submit(
            ALICE, queued(3)
        )  # queued, and claimed by nobody: the engine stops
        quick.release.set()
        await stopping
    assert len(quick.calls) == 1

    async with engine_on(database_url, clock, slow, async_drain_timeout_s=0.05) as cut:
        assert await states(cut, finished.id, refused.id) == ["succeeded", "queued"]
        assert await cut.store.read_result(finished.id, clock.now()) == b"late"
        await cut.jobs.cancel(ALICE, refused.id)
        await cut.start()
        paid = await cut.jobs.submit(ALICE, queued(2))
        await until(lambda: len(slow.calls) == 1, "the job is at the runner")
        await cut.stop()  # the runner never answers: the drain time passes and the job is cancelled
        async with engine_on(database_url, clock, ScriptedStreamTransport()) as reader:
            left = await reader.jobs.get(ALICE, paid.id)
            assert left.failure is not None
            assert (left.state, left.failure.payment_sent, left.result) == ("uncertain", True, None)
            assert [(a.outcome, a.payment_sent, len(a.sessions)) for a in left.attempts] == [
                ("in_flight", True, 1)
            ]
            assert await reader.jobs.recover_interrupted() == 0  # ended: a restart has nothing to resolve
    assert len(slow.calls) == 1  # never run again


class WaitsForItsAllocation:
    """A credential resolver that holds a claimed job before its first attempt is written."""

    def __init__(self) -> None:
        self.asked, self.release = asyncio.Event(), asyncio.Event()
        self.hold = True

    async def credential(self, actor: ActorContext) -> PaymentKey:
        del actor
        if self.hold:
            self.asked.set()
            await self.release.wait()
        return PaymentKey(token=None, allocation_id=None)


async def test_a_job_stopped_before_its_first_attempt_goes_back_to_the_queue(
    database_url: str, clock: FixedClock
) -> None:
    """Nothing went out for it, so stop puts it back, as a restart would. The next engine runs it."""
    credentials = WaitsForItsAllocation()
    before, after = ScriptedStreamTransport(), ScriptedStreamTransport(steps=[echo])
    supply = EngineSupply(credentials=credentials)  # type: ignore[arg-type]
    async with engine_on(database_url, clock, before, supply=supply, async_drain_timeout_s=0.05) as first:
        credentials.hold = False
        job = await first.jobs.submit(ALICE, queued())
        credentials.hold = True
        await first.start()
        await credentials.asked.wait()  # claimed, and held before any attempt
        assert await states(first, job.id) == ["running"]
        await first.stop()
    assert before.calls == []

    credentials.hold = False
    async with engine_on(database_url, clock, after, supply=supply) as second:
        assert await states(second, job.id) == ["queued"]
        assert await claims(second, job.id) == 1
        await second.start()
        await until(lambda: len(after.calls) == 1 and second.admission.running == 0, "the job ran")
        assert await states(second, job.id) == ["succeeded"]
        assert await claims(second, job.id) == 2


@pytest.mark.parametrize("running", [False, True])
async def test_the_worker_loop_reads_the_queue_only_while_jobs_wait(
    database_url: str, clock: FixedClock, running: bool
) -> None:
    """An engine nobody submits to adds no query: ``start()`` counts the queue once, before the loop
    runs, and the loop then waits for a submit. With a job waiting behind a full engine, it looks again
    at every poll."""
    transport = PaysThenWaits()
    async with engine_on(database_url, clock, transport, max_concurrent_jobs=1) as engine:
        await allot(engine)
        looks = 0
        counted = engine.store.queued_stats

        async def counting() -> tuple[int, Any]:
            nonlocal looks
            looks += 1
            return await counted()

        engine.store.queued_stats = counting  # type: ignore[method-assign]
        await engine.start()
        await until(lambda: looks == 1, "the loop looked once")
        await asyncio.sleep(0.1)  # ten polls
        assert looks == 1
        if running:
            await engine.jobs.submit(ALICE, queued(1))
            await until(lambda: len(transport.calls) == 1, "the first job is at the runner")
            await engine.jobs.submit(ALICE, queued(2))  # waits: the engine's one slot is taken
            seen = looks
            await until(lambda: looks >= seen + 3, "the loop keeps looking while a job waits")
            assert await engine.store.queued_stats() == (1, clock.now())
            transport.release.set()
            await until(lambda: len(transport.calls) == 2 and engine.admission.running == 0, "both ran")
