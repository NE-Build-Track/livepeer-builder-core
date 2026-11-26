"""ISS-26: a queued job waits through what may pass, up to the queue's time limit.

A job that nothing was attempted for is not failed because, at the moment a worker reached it, no
runner was listed for its app (a restart whose first discovery fetch failed), every runner it would pay
advertised no room, or its actor's credential could not be read (a provider or secret store that is
away for a moment). It stays ``queued``, and its claim is given back without counting, so the wait
never uses up the three claims that guard against a job that stops the engine. It fails ``no_capacity``
when it has waited ``LPB_ASYNC_QUEUE_TTL_S``.

An app nobody can run now is set aside as an app at its limit is, but not unseen: once a poll interval
the worker claims one of its jobs, which is how a job that need not wait is found. A job that does not
pay runs on a runner that advertises no room, as an immediate one does. And what is an answer still
fails a job, unpaid, while its app waits: an actor whose allocation was revoked ends ``not_provisioned``
whatever the runners say, because a queued job is asked about its actor first. A queue of waiting jobs
costs one claim an app in a poll interval, however long it is and however often the worker is woken.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
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
from tests.unit.test_zero_capacity import (
    APP,
    OPERATOR,
    PRICED,
    ROOM_AND_NONE,
    answer,
    called,
    full_and_priced,
    refusal,
    runner_record,
)

OTHER = "other/app"
POLL_S = 3600.0  # so long that no test passes one by waiting: a test moves the loop's clock
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


async def still_waits(engine: BuilderEngine, job_id: Any, *, looked_at: int) -> None:
    """The job is as it was submitted: queued, nothing recorded. The worker looked at it ``looked_at``
    times, each a claim it gave back, so none of them counts."""
    job = await engine.jobs.get(ALICE, job_id)
    assert (job.state, job.failure, job.attempts, job.started_at) == ("queued", None, (), None)
    assert (await claims(engine, job_id), await given_back(engine, job_id)) == (looked_at, looked_at)
    assert engine.admission.running == 0


async def looked_at_once_and_left_waiting(engine: BuilderEngine, job_id: Any) -> None:
    """The worker claims the job once, to see whether it has to wait, and gives it back. However often
    it steps in the same poll interval, it does not claim it again."""
    assert await engine.jobs.worker.run_once() == 1
    await still_waits(engine, job_id, looked_at=1)
    assert [await engine.jobs.worker.run_once() for _ in range(3)] == [0, 0, 0]
    await still_waits(engine, job_id, looked_at=1)


async def test_a_job_waits_while_no_runner_is_listed_for_its_app(
    loop_time: LoopTime,  # noqa: F811
) -> None:
    """After a restart whose first discovery fetch failed the engine lists no runner, and every queued
    job failed ``no_offering`` the moment it was claimed."""
    transport = ScriptedStreamTransport(steps=[ScriptedTransport.ok()])
    async with engine_reading(NOBODY, ROOMY, transport=transport, async_poll_s=POLL_S) as engine:
        job = await engine.jobs.submit(ALICE, queued())

        await looked_at_once_and_left_waiting(engine, job.id)

        await engine.discovery.refresh()  # the runner is listed now
        loop_time.advance(POLL_S)  # a job that was given back is left alone for a poll interval
        assert await engine.jobs.worker.run_once() == 1
        assert (await engine.jobs.get(ALICE, job.id)).state == "succeeded"
        # the claim it ran under is the one that counts
        assert (await claims(engine, job.id), await given_back(engine, job.id)) == (2, 1)
        assert len(transport.calls) == 1


async def test_a_job_that_pays_waits_while_every_runner_advertises_no_room(
    loop_time: LoopTime,  # noqa: F811
) -> None:
    transport = ScriptedStreamTransport(steps=[ScriptedTransport.ok()])
    full = answer(full_and_priced("a"))
    async with engine_reading(full, ROOMY, transport=transport, async_poll_s=POLL_S) as engine:
        job = await engine.jobs.submit(ALICE, queued())

        await looked_at_once_and_left_waiting(engine, job.id)
        assert transport.calls == []  # a full runner's refusal can cost a ticket: it is not asked

        await engine.discovery.refresh()
        loop_time.advance(POLL_S)
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


async def test_a_queued_job_that_pays_tries_a_priced_runner_advertising_no_room_last_and_pays_it() -> None:
    """As an immediate job does (``test_zero_capacity.py``; #158, Mike, 26 November 2026). One runner
    the job pays has room, so the job does not wait and every runner stays a candidate: the priced
    runner that advertises none is tried after the priced runner with room, with the job's payment key,
    and ahead of a runner with more room that the job signs nothing for."""
    transport = ScriptedStreamTransport(steps=[refusal(), ScriptedTransport.ok()])
    async with engine_reading(answer(*ROOM_AND_NONE), transport=transport) as engine:
        job = await engine.jobs.submit(ALICE, queued())
        assert await engine.jobs.worker.run_once() == 1
        ended = await engine.jobs.get(ALICE, job.id)
    assert (ended.state, called(transport)) == ("succeeded", ["roomy", "zero"])
    assert [attempt.payment_sent for attempt in ended.attempts] == [False, True]
    assert all(key is not None for _, key in transport.calls)  # each call carried the payment key

    every_refusal = ScriptedStreamTransport(steps=[refusal(), refusal(), refusal()])
    async with engine_reading(answer(*ROOM_AND_NONE), transport=every_refusal) as engine:
        job = await engine.jobs.submit(ALICE, queued())
        assert await engine.jobs.worker.run_once() == 1
        ended = await engine.jobs.get(ALICE, job.id)
    assert ended.failure is not None
    assert (ended.state, ended.failure.kind) == ("failed", "refused")
    assert called(every_refusal) == ["roomy", "zero", "unpayable"]


async def test_an_app_nobody_can_run_now_does_not_hold_up_another_apps_job() -> None:
    """One worker, and the oldest job's app has no runner. The worker looks at that job once, which
    takes its one worker for the moment of a claim and its return, and the next step leaves the app
    out, as it leaves out an app at its limit, and runs the other app's job."""
    transport = ScriptedStreamTransport(steps=[ScriptedTransport.ok()])
    listed = answer(runner_record("other", app=OTHER))
    supply = EngineSupply(clock=FixedClock())
    async with engine_reading(listed, transport=transport, supply=supply, async_poll_s=POLL_S) as engine:
        absent = await engine.jobs.submit(ALICE, queued())
        engine.clock.advance(1)  # type: ignore[attr-defined]
        present = await engine.jobs.submit(ALICE, queued(OTHER))

        assert await engine.jobs.worker.run_once() == 1  # the look at the oldest job
        assert await engine.jobs.worker.run_once() == 1  # the other app's job

        assert (await engine.jobs.get(ALICE, present.id)).state == "succeeded"
        await still_waits(engine, absent.id, looked_at=1)


async def test_a_waiting_apps_jobs_cost_one_claim_a_poll_interval_however_often_the_worker_steps(
    loop_time: LoopTime,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Five jobs of an app with no runner, ahead of another app's job, and two workers. Each step is a
    wake of the loop. The first claims one of the five, to see whether it has to wait, and the other
    app's job in the same step; the steps after it, in the same poll interval, claim nothing. A poll
    interval later one job is looked at again, and only one."""
    transport = ScriptedStreamTransport(steps=[ScriptedTransport.ok()])
    listed = answer(runner_record("other", app=OTHER))
    settings: dict[str, Any] = {
        "async_poll_s": POLL_S,
        "async_workers": 2,
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}",  # two jobs write at once
        "supply": EngineSupply(clock=FixedClock()),
    }
    async with engine_reading(listed, transport=transport, **settings) as engine:
        waiting = []
        for _ in range(5):
            waiting.append(await engine.jobs.submit(ALICE, queued()))
            engine.clock.advance(1)  # type: ignore[attr-defined]
        behind = await engine.jobs.submit(ALICE, queued(OTHER))

        async def claimed() -> int:
            return sum([await claims(engine, job.id) for job in waiting])

        assert await engine.jobs.worker.run_once() == 2  # one look, and the other app's job
        assert (await engine.jobs.get(ALICE, behind.id)).state == "succeeded"
        assert await claimed() == 1
        assert [await engine.jobs.worker.run_once() for _ in range(5)] == [0] * 5  # woken five times
        assert await claimed() == 1

        loop_time.advance(POLL_S)
        assert [await engine.jobs.worker.run_once() for _ in range(5)] == [1, 0, 0, 0, 0]
        assert await claimed() == 2
        assert [(await engine.jobs.get(ALICE, job.id)).state for job in waiting] == ["queued"] * 5
        assert sum([await given_back(engine, job.id) for job in waiting]) == 2  # none of them counts


async def test_a_job_that_waited_the_queues_time_limit_fails_no_capacity_whatever_it_waited_for() -> None:
    clock = FixedClock()
    transport = ScriptedStreamTransport()
    supply = EngineSupply(clock=clock)
    async with engine_reading(NOBODY, transport=transport, supply=supply, async_queue_ttl_s=600) as engine:
        job = await engine.jobs.submit(ALICE, queued())
        clock.advance(599)
        assert await engine.jobs.worker.run_once() == 1  # looked at, and given back
        await still_waits(engine, job.id, looked_at=1)
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
    """A credential resolver of the deployment's own. Its credential has no token unless it is given
    one, so a job it resolves for does not pay. Its store can be away (any error but
    ``NotProvisioned``, which is an answer), and an actor's allocation can be revoked."""

    def __init__(self, token: str | None = None) -> None:
        self.away, self.gone, self.asked = False, False, 0
        self.revoked: set[str] = set()
        self._token = token

    async def credential(self, actor: ActorContext) -> PaymentKey:
        if self.gone or actor.actor_id in self.revoked:
            raise NotProvisioned(f"actor {actor.actor_id!r} has no allocation; an admin must allot one")
        if self.away:
            self.asked += 1
            raise OSError("the secret store went away")
        return PaymentKey(token=self._token, allocation_id=None)


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

    waiting.ran(job_id, APP)  # it ran: the next time it is set aside it starts again at one interval
    waiting.put(job_id, poll_s)
    loop_time.advance(1.5)
    assert waiting.due(job_id)
    loop_time.advance(32)  # long due and never set aside again: forgotten
    assert waiting.set_aside() == 0
    waiting.put(job_id, poll_s)
    loop_time.advance(1.5)
    assert waiting.due(job_id)  # one interval again, not two


FULL = answer(full_and_priced("a"))  # every runner priced, and none with room


async def test_a_job_that_does_not_pay_runs_though_every_priced_runner_is_full() -> None:
    """A credential with no token makes an unpaid call, and an unpaid call is tried on a runner that
    advertises no room: its refusal costs nothing, and the advertisement may be old (#114, C17). The
    worker set the whole app aside because a job that paid would have to wait, so this job was never
    claimed and ended ``no_capacity`` at the queue's time limit with nothing attempted."""
    transport = ScriptedStreamTransport(steps=[ScriptedTransport.ok(manifest_ids=()), ScriptedTransport.ok()])
    supply = EngineSupply(credentials=AwayForAWhile(), clock=FixedClock())  # type: ignore[arg-type]
    settings: dict[str, Any] = {"allotted": False, "async_poll_s": POLL_S}
    async with engine_reading(FULL, transport=transport, supply=supply, **settings) as engine:
        first = await engine.jobs.submit(ALICE, queued())
        engine.clock.advance(1)  # type: ignore[attr-defined]
        second = await engine.jobs.submit(ALICE, queued(payload={"x": 2}))

        assert await engine.jobs.worker.run_once() == 1

        ran = await engine.jobs.get(ALICE, first.id)
        assert (ran.state, [(a.outcome, a.payment_sent) for a in ran.attempts]) == (
            "succeeded",
            [("succeeded", False)],
        )
        assert transport.calls == [("https://a.example/app", None)]  # called, with no credential to pay
        # a job that ran did not have to wait: the next one is looked at now, not a poll interval on
        assert await engine.jobs.worker.run_once() == 1
        assert (await engine.jobs.get(ALICE, second.id)).state == "succeeded"


@pytest.mark.parametrize("inventory", [NOBODY, FULL], ids=["no runner listed", "every priced runner full"])
async def test_a_revoked_actors_job_fails_not_provisioned_while_its_app_waits(inventory: bytes) -> None:
    """The allocation was revoked after the submit, and nobody can run the job's app now. The app was
    set aside before any of its jobs was claimed, so the revoked actor's job waited with the rest and
    ended ``no_capacity`` at the queue's time limit. A queued job is asked about its actor first:
    it fails ``not_provisioned``, unpaid, whatever the runners say."""
    credentials = AwayForAWhile(token="lpg_key")
    transport = ScriptedStreamTransport()
    supply = EngineSupply(credentials=credentials)  # type: ignore[arg-type]
    settings: dict[str, Any] = {"allotted": False, "async_poll_s": POLL_S}
    async with engine_reading(inventory, transport=transport, supply=supply, **settings) as engine:
        job = await engine.jobs.submit(ALICE, queued())
        credentials.revoked.add("alice")

        assert await engine.jobs.worker.run_once() == 1

        failed = await engine.jobs.get(ALICE, job.id)
        assert failed.failure is not None
        assert (failed.state, failed.failure.kind, failed.failure.payment_sent) == (
            "failed",
            "not_provisioned",
            False,
        )
        assert (failed.attempts, transport.calls, engine.admission.running) == ((), [], 0)


async def test_every_waiting_job_is_looked_at_in_turn_so_a_revoked_one_behind_others_is_found(
    loop_time: LoopTime,  # noqa: F811
) -> None:
    """Two jobs that have to wait, and behind them the job of an actor whose allocation was revoked.
    One job is looked at each poll interval. A job that has to wait is then left alone for longer each
    time, so the look moves on to the jobs behind it: the third job is reached after a few intervals,
    long before the queue's time limit, and fails ``not_provisioned``."""
    credentials = AwayForAWhile(token="lpg_key")
    transport = ScriptedStreamTransport()
    supply = EngineSupply(credentials=credentials, clock=FixedClock())  # type: ignore[arg-type]
    settings: dict[str, Any] = {"allotted": False, "async_poll_s": POLL_S, "async_queue_ttl_s": 3600}
    bob = ALICE.model_copy(update={"actor_id": "bob"})
    async with engine_reading(FULL, transport=transport, supply=supply, **settings) as engine:
        ahead = []
        for _ in range(2):
            ahead.append(await engine.jobs.submit(ALICE, queued()))
            engine.clock.advance(1)  # type: ignore[attr-defined]
        revoked = await engine.jobs.submit(bob, queued())
        credentials.revoked.add("bob")

        for _ in range(10):  # ten poll intervals at most, where the time limit is 3600 of them
            await engine.jobs.worker.run_once()
            loop_time.advance(POLL_S)

        failed = await engine.jobs.get(bob, revoked.id)
        assert failed.failure is not None
        assert (failed.failure.kind, failed.attempts) == ("not_provisioned", ())
        assert [(await engine.jobs.get(ALICE, job.id)).state for job in ahead] == ["queued", "queued"]
        assert transport.calls == []


async def test_each_waiting_app_is_looked_at_once_a_poll_interval_until_one_of_its_jobs_runs(
    loop_time: LoopTime,  # noqa: F811
) -> None:
    waiting, poll_s = Waiting(), 10.0
    assert waiting.may_look(APP, poll_s) and waiting.may_look(OTHER, poll_s)

    waiting.looked(APP, poll_s)
    loop_time.advance(4)
    waiting.looked(OTHER, poll_s)
    assert not waiting.may_look(APP, poll_s) and not waiting.may_look(OTHER, poll_s)
    loop_time.advance(6)  # ten seconds since the look at the first app, six since the second
    assert waiting.may_look(APP, poll_s) and not waiting.may_look(OTHER, poll_s)

    waiting.ran(uuid4(), OTHER)  # a job of it was dispatched: the next is looked at now
    assert waiting.may_look(OTHER, poll_s)
