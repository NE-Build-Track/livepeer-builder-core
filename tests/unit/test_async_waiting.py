"""ISS-26: a queued job waits through what may pass, up to the queue's time limit.

A job that nothing was attempted for is not failed because, at the moment a worker reached it, no
runner was listed for its app (a restart whose first discovery fetch failed), every runner it would pay
advertised no room, or its actor's credential could not be read (a provider or secret store that is
away for a moment). It stays ``queued``: an app nobody can run now is left out as an app at its limit
is, and a job claimed before that was known is given back without its claim counting, so the wait
never uses up the three claims that guard against a job that stops the engine. It fails ``no_capacity``
when it has waited ``LPB_ASYNC_QUEUE_TTL_S``. An actor with no allocation and a refused payment still
fail the job at once, unpaid (``test_async_repeats.py``, ``test_async_paying.py``).
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.contracts import ActorContext, AllotRequest, JobRequest, Money, PaymentKey
from livepeer_builder.errors import NotProvisioned
from livepeer_builder.services.async_held import Waiting
from livepeer_builder.testing import FixedClock, ScriptedStreamTransport, ScriptedTransport
from tests.store.test_async_restart import claims, given_back
from tests.unit.discovery_wire import ALICE, wire_engine
from tests.unit.test_job_deadline import LoopTime, loop_time  # noqa: F401 - loop_time: a fixture
from tests.unit.test_zero_capacity import APP, OPERATOR, PRICED, answer, full_and_priced, runner_record

OTHER = "other/app"
NOBODY = answer()  # a discovery answer that lists no runner
ROOMY = answer(runner_record("a", capacity_available=1, price_info=PRICED))


def queued(app: str = APP, **fields: object) -> JobRequest:
    return JobRequest.model_validate({"app": app, "mode": "async", "payload": {"x": 1}, **fields})


@asynccontextmanager
async def engine_reading(
    *answers: bytes,
    transport: ScriptedTransport,
    supply: EngineSupply | None = None,
    allotted: bool = True,
    **settings: Any,
) -> AsyncIterator[BuilderEngine]:
    """An engine whose discovery read the first of ``answers`` and reads the next at each refresh, with
    alice allotted (not when the test supplies who resolves credentials)."""
    parts = EngineSupply(**{**(vars(supply) if supply is not None else {}), "transport": transport})
    async with wire_engine(*answers, supply=parts, **settings) as engine:
        if allotted:
            amount = Money(amount=Decimal(5), currency="USD")
            request = AllotRequest(
                operation_ref="allot-alice", tenant_id="default", actor_id="alice", amount=amount
            )
            await engine.payments.allot(OPERATOR, request)
        yield engine


async def waits_unclaimed(engine: BuilderEngine, job_id: Any) -> None:
    """One step of the worker leaves the job as it was: queued, never claimed, nothing recorded."""
    assert await engine.jobs.worker.run_once() == 0
    job = await engine.jobs.get(ALICE, job_id)
    assert (job.state, job.failure, job.attempts, job.started_at) == ("queued", None, (), None)
    assert await claims(engine, job_id) == 0


async def test_a_job_waits_while_no_runner_is_listed_for_its_app() -> None:
    """After a restart whose first discovery fetch failed the engine lists no runner, and every queued
    job failed ``no_offering`` the moment it was claimed."""
    transport = ScriptedStreamTransport(steps=[ScriptedTransport.ok()])
    async with engine_reading(NOBODY, ROOMY, transport=transport) as engine:
        job = await engine.jobs.submit(ALICE, queued())

        await waits_unclaimed(engine, job.id)
        await waits_unclaimed(engine, job.id)  # as often as it takes

        await engine.discovery.refresh()  # the runner is listed now
        assert await engine.jobs.worker.run_once() == 1
        assert (await engine.jobs.get(ALICE, job.id)).state == "succeeded"
        assert (len(transport.calls), await claims(engine, job.id)) == (1, 1)


async def test_a_job_that_pays_waits_while_every_runner_advertises_no_room() -> None:
    transport = ScriptedStreamTransport(steps=[ScriptedTransport.ok()])
    async with engine_reading(answer(full_and_priced("a")), ROOMY, transport=transport) as engine:
        job = await engine.jobs.submit(ALICE, queued())

        await waits_unclaimed(engine, job.id)
        assert transport.calls == []  # a full runner's refusal can cost a ticket: it is not asked

        await engine.discovery.refresh()
        assert await engine.jobs.worker.run_once() == 1
        assert (await engine.jobs.get(ALICE, job.id)).state == "succeeded"


async def test_a_runner_that_would_sign_nothing_is_still_tried_at_zero_capacity() -> None:
    """Not changed (#114, C17): advertised capacity can lag, and a runner an attempt signs nothing for
    is tried anyway. Only a job that would pay for the refusal waits."""
    transport = ScriptedStreamTransport(steps=[ScriptedTransport.ok()])
    async with engine_reading(
        answer(runner_record("a", capacity_available=0)), transport=transport
    ) as engine:
        job = await engine.jobs.submit(ALICE, queued())
        assert await engine.jobs.worker.run_once() == 1
        assert (await engine.jobs.get(ALICE, job.id)).state == "succeeded"


async def test_an_app_nobody_can_run_now_does_not_hold_up_another_apps_job() -> None:
    """One worker, and the oldest job's app has no runner. The worker leaves it out, as it leaves out an
    app at its limit, and runs the other app's job."""
    transport = ScriptedStreamTransport(steps=[ScriptedTransport.ok()])
    listed = answer(runner_record("other", app=OTHER))
    async with engine_reading(listed, transport=transport, supply=EngineSupply(clock=FixedClock())) as engine:
        absent = await engine.jobs.submit(ALICE, queued())
        engine.clock.advance(1)  # type: ignore[attr-defined]
        present = await engine.jobs.submit(ALICE, queued(OTHER))

        assert await engine.jobs.worker.run_once() == 1

        assert [(await engine.jobs.get(ALICE, job.id)).state for job in (absent, present)] == [
            "queued",
            "succeeded",
        ]
        assert await claims(engine, absent.id) == 0


async def test_a_job_that_waited_the_queues_time_limit_fails_no_capacity_whatever_it_waited_for() -> None:
    clock = FixedClock()
    transport = ScriptedStreamTransport()
    supply = EngineSupply(clock=clock)
    async with engine_reading(NOBODY, transport=transport, supply=supply, async_queue_ttl_s=600) as engine:
        job = await engine.jobs.submit(ALICE, queued())
        clock.advance(599)
        await waits_unclaimed(engine, job.id)
        clock.advance(2)

        assert await engine.jobs.worker.run_once() == 0

        ended = await engine.jobs.get(ALICE, job.id)
        assert ended.failure is not None
        assert (ended.state, ended.failure.kind, ended.failure.payment_sent) == (
            "failed",
            "no_capacity",
            False,
        )
        assert ended.failure.message == "the job was not dispatched within the queue's time limit (600 s)"
        assert (ended.attempts, transport.calls) == ((), [])
        reader = ALICE.model_copy(update={"permissions": ALICE.permissions | {"costs.read"}})
        assert (await engine.costs.for_job(reader, job.id)).status == "none"


class AwayForAWhile:
    """A credential resolver whose store is away: any error but ``NotProvisioned``, which is an answer."""

    def __init__(self) -> None:
        self.away, self.gone, self.asked = False, False, 0

    async def credential(self, actor: ActorContext) -> PaymentKey:
        del actor
        if self.gone:
            raise NotProvisioned("actor 'alice' has no allocation; an admin must allot one")
        if self.away:
            self.asked += 1
            raise OSError("the secret store went away")
        return PaymentKey(token=None, allocation_id=None)


async def step_until(engine: BuilderEngine, done: Any, what: str) -> None:
    """Step the worker until ``done()``. A job that was given back is not claimed again before a poll
    interval has passed, so this takes a few milliseconds; ten seconds and the test fails."""
    deadline = asyncio.get_running_loop().time() + 10
    while not done():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting until {what}")
        await engine.jobs.worker.run_once()
        await asyncio.sleep(0.001)


async def test_a_job_whose_credential_cannot_be_read_is_given_back_and_its_claim_does_not_count() -> None:
    """The lookup failed with something other than "no allocation": the job was ``failed``,
    ``runner_error``, with no attempt. It is given back instead, more often than a job may be claimed,
    and runs once the lookup answers."""
    credentials = AwayForAWhile()
    transport = ScriptedStreamTransport(steps=[ScriptedTransport.ok()])
    supply = EngineSupply(credentials=credentials)  # type: ignore[arg-type]
    settings: dict[str, Any] = {"allotted": False, "async_poll_s": 0.001}
    async with engine_reading(ROOMY, transport=transport, supply=supply, **settings) as engine:
        job = await engine.jobs.submit(ALICE, queued())
        credentials.away = True

        assert await engine.jobs.worker.run_once() == 1  # claimed, and given back
        waiting = await engine.jobs.get(ALICE, job.id)
        assert (waiting.state, waiting.failure, waiting.attempts) == ("queued", None, ())
        assert waiting.started_at is None
        assert (await claims(engine, job.id), await given_back(engine, job.id)) == (1, 1)
        await step_until(engine, lambda: credentials.asked >= 5, "the job was given back five times")
        assert (await engine.jobs.get(ALICE, job.id)).state == "queued"
        waited = await given_back(engine, job.id)
        assert waited >= 5 and await claims(engine, job.id) == waited  # every claim given back: none counted
        assert engine.admission.running == 0

        credentials.away = False
        await step_until(engine, lambda: bool(transport.calls), "the job ran")
        assert (await engine.jobs.get(ALICE, job.id)).state == "succeeded"
        # the claim it ran under is the one claim that counts, and its number is past every one given back
        assert await claims(engine, job.id) == await given_back(engine, job.id) + 1 > waited


async def test_a_job_given_back_is_not_claimed_again_before_the_poll_interval() -> None:
    """So a job that cannot be dispatched costs a claim now and then, and the loop does not spin."""
    credentials = AwayForAWhile()
    transport = ScriptedStreamTransport()
    supply = EngineSupply(credentials=credentials)  # type: ignore[arg-type]
    settings: dict[str, Any] = {"allotted": False, "async_poll_s": 3600}
    async with engine_reading(ROOMY, transport=transport, supply=supply, **settings) as engine:
        job = await engine.jobs.submit(ALICE, queued())
        credentials.away = True
        assert await engine.jobs.worker.run_once() == 1

        assert [await engine.jobs.worker.run_once() for _ in range(3)] == [0, 0, 0]

        assert credentials.asked == 1
        assert (await engine.jobs.get(ALICE, job.id)).state == "queued"
        assert (await claims(engine, job.id), await given_back(engine, job.id)) == (1, 1)
        assert engine.admission.running == 0


async def test_an_actor_with_no_allocation_still_fails_the_job_at_once() -> None:
    credentials = AwayForAWhile()
    transport = ScriptedStreamTransport()
    supply = EngineSupply(credentials=credentials)  # type: ignore[arg-type]
    async with engine_reading(ROOMY, transport=transport, supply=supply, allotted=False) as engine:
        job = await engine.jobs.submit(ALICE, queued())
        credentials.gone = True  # revoked while the job waited

        assert await engine.jobs.worker.run_once() == 1

        failed = await engine.jobs.get(ALICE, job.id)
        assert failed.failure is not None
        assert (failed.state, failed.failure.kind, failed.attempts) == ("failed", "not_provisioned", ())
        assert transport.calls == []


async def test_a_runner_that_went_away_after_the_claim_gives_the_job_back() -> None:
    """The runner was listed when the worker looked and gone when the job was dispatched. Nothing was
    attempted: the job is given back, its claim uncounted, and waits as if it had never been claimed."""
    transport = ScriptedStreamTransport()
    async with engine_reading(ROOMY, NOBODY, transport=transport) as engine:
        job = await engine.jobs.submit(ALICE, queued())
        claim = engine.store.claim_job

        async def the_list_is_read_again(*args: Any, **kwargs: Any) -> Any:
            claimed = await claim(*args, **kwargs)
            await engine.discovery.refresh()  # between the worker's look and the dispatch
            return claimed

        engine.store.claim_job = the_list_is_read_again  # type: ignore[method-assign]
        assert await engine.jobs.worker.run_once() == 1

        waiting = await engine.jobs.get(ALICE, job.id)
        assert (waiting.state, waiting.failure, waiting.attempts) == ("queued", None, ())
        assert (await claims(engine, job.id), await given_back(engine, job.id)) == (1, 1)
        assert (engine.admission.running, transport.calls) == (0, [])


async def test_a_job_given_back_under_a_claim_it_lost_is_left_to_whoever_holds_it() -> None:
    """Giving a claim back is a write like any other: it is made only under the claim that holds the
    job. Here the job was taken from its worker while the credential lookup failed."""
    credentials = AwayForAWhile()
    transport = ScriptedStreamTransport()
    supply = EngineSupply(credentials=credentials)  # type: ignore[arg-type]
    async with engine_reading(ROOMY, transport=transport, supply=supply, allotted=False) as engine:
        job = await engine.jobs.submit(ALICE, queued())
        lookup, now = credentials.credential, engine.clock.now()

        async def taken_meanwhile(actor: ActorContext) -> PaymentKey:
            assert await engine.store.requeue_unstarted(job.id, 3)
            assert await engine.store.claim_job(job.id, now) is not None  # another writer's, number 2
            return await lookup(actor)

        credentials.away = True
        credentials.credential = taken_meanwhile  # type: ignore[method-assign]
        assert await engine.jobs.worker.run_once() == 1

        left = await engine.jobs.get(ALICE, job.id)
        assert (left.state, left.attempts, await claims(engine, job.id)) == ("running", (), 2)
        assert engine.admission.running == 0


async def test_an_immediate_job_whose_credential_cannot_be_read_fails_as_before() -> None:
    """Waiting is a queued job's: a caller that waits for its answer is told at once."""
    credentials = AwayForAWhile()
    credentials.away = True
    transport = ScriptedStreamTransport()
    supply = EngineSupply(credentials=credentials)  # type: ignore[arg-type]
    async with engine_reading(ROOMY, transport=transport, supply=supply, allotted=False) as engine:
        with pytest.raises(OSError, match="the secret store went away"):
            await engine.jobs.run(ALICE, JobRequest(app=APP))

        (job,) = await engine.jobs.list_jobs(ALICE)
        assert job.failure is not None
        assert (job.state, job.failure.kind, job.attempts) == ("failed", "runner_error", ())
        assert transport.calls == []


async def test_a_job_that_keeps_being_set_aside_waits_longer_each_time_and_is_forgotten_when_stale(
    loop_time: LoopTime,  # noqa: F811
) -> None:
    """One poll interval, then two, four, up to 32: a cause that lasts costs a claim now and then. A
    job that was not set aside again for long is forgotten, so what the worker remembers stays small."""
    waiting, job_id, poll_s = Waiting(), uuid4(), 1.0
    assert waiting.due(job_id) and waiting.set_aside() == 0

    for expected in (1, 2, 4, 8, 16, 32, 32):
        waiting.put(job_id, poll_s)
        loop_time.advance(expected - 0.5)
        assert not waiting.due(job_id) and waiting.set_aside() == 1
        loop_time.advance(1)
        assert waiting.due(job_id) and waiting.set_aside() == 0

    waiting.forget(job_id)  # it ran: the next time it is set aside it starts again at one interval
    waiting.put(job_id, poll_s)
    loop_time.advance(1.5)
    assert waiting.due(job_id)
    loop_time.advance(32)  # long due and never set aside again: forgotten
    assert waiting.set_aside() == 0
    waiting.put(job_id, poll_s)
    loop_time.advance(1.5)
    assert waiting.due(job_id)  # one interval again, not two
