"""One deadline for a job that waits for its answer: ``timeout_s`` is the whole job, from its start.

``jobs.run`` notes when the job's time runs out as it starts, and the paid call gives each attempt only
what is left. A job that fails over is therefore not given ``timeout_s`` again for every runner it
tries (with three attempts and 300 s that was up to 15 minutes), and once the time is up no further
runner is called or paid: the job ends ``timeout``. What decides a failover is unchanged: a paid
attempt that timed out still ends the job ``uncertain``. An async job follows the same rule from its
claim (``test_async_deadline.py``).

No test here waits: ``LoopTime`` moves the event loop's clock, which the deadline is read from.
"""

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from livepeer_builder import EngineSupply
from livepeer_builder.contracts import ActorContext, Attempt, JobRequest, PaymentKey, Runner, RunnerReply
from livepeer_builder.errors import JobFailed, RunnerCallError
from livepeer_builder.protocols import OnSigned
from livepeer_builder.testing import ScriptedTransport, runner
from tests.conftest import EngineFactory, allot

APP = "example/echo"
TWO_ORCHESTRATORS = (
    runner(APP, "https://runner-1.example", orchestrator_url="https://orch-1.example"),
    runner(APP, "https://runner-2.example", orchestrator_url="https://orch-2.example"),
)
Step = Callable[[Runner, JobRequest, str | None], RunnerReply]


class LoopTime:
    """The event loop's clock, which a test moves ahead without waiting. A job's deadline is a time on
    this clock, so moving it is how a test lets a job's time pass."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loop = asyncio.get_running_loop()
        real, self._ahead = loop.time, 0.0
        monkeypatch.setattr(loop, "time", lambda: real() + self._ahead)

    def advance(self, seconds: float) -> None:
        self._ahead += seconds


@pytest.fixture
async def loop_time(monkeypatch: pytest.MonkeyPatch) -> LoopTime:
    return LoopTime(monkeypatch)


def refusal(paid: bool = False) -> RunnerCallError:
    manifests = ("manifest-refused",) if paid else ()
    return RunnerCallError(
        "refused", "HTTP 503 from the runner", payment_sent=paid, manifest_ids=manifests, status_code=503
    )


def refuses_after(loop_time: LoopTime, seconds: float, *, paid: bool = False) -> Step:
    """A runner that takes ``seconds`` of the job's time and then refuses, so the job fails over."""

    def step(_: Runner, __: JobRequest, ___: str | None) -> RunnerReply:
        loop_time.advance(seconds)
        raise refusal(paid)

    return step


def timeouts_given(transport: ScriptedTransport, monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The ``timeout_s`` of the request each runner call was made with, in order."""
    seen: list[float] = []
    call = transport.call

    async def records(
        to: Runner, request: JobRequest, credential: str | None, on_signed: OnSigned | None = None
    ) -> RunnerReply:
        seen.append(request.timeout_s)
        return await call(to, request, credential, on_signed)

    monkeypatch.setattr(transport, "call", records)
    return seen


async def test_the_attempts_of_an_immediate_job_share_one_timeout(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    loop_time: LoopTime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first runner refuses when 25 of the job's 30 seconds are gone, and the job fails over. The
    second runner is given the 5 seconds that are left, not 30 again."""
    engine = await make_engine(TWO_ORCHESTRATORS)
    await allot(engine, alice)
    seen = timeouts_given(transport, monkeypatch)
    transport.steps = [refuses_after(loop_time, 25), ScriptedTransport.ok()]

    result = await engine.jobs.run(alice, JobRequest(app=APP, timeout_s=30, max_attempts=2))

    assert result.job.state == "succeeded"
    first, second = seen
    assert 25 < first <= 30  # the whole of it, less what the engine itself took before the call
    assert 0 < second <= 5


async def test_an_immediate_job_whose_time_is_up_makes_no_further_paid_call(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport, loop_time: LoopTime
) -> None:
    """The first runner was paid and refused when the job's time was up. Failing over would pay a
    second runner for work the caller no longer waits for: the job ends ``timeout``. It is ``failed``,
    not ``uncertain``: the attempt it made was refused, so no work is left in doubt."""
    engine = await make_engine(TWO_ORCHESTRATORS)
    await allot(engine, alice)
    transport.steps = [refuses_after(loop_time, 31, paid=True), ScriptedTransport.ok()]

    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP, timeout_s=30, max_attempts=2))

    job = failed.value.job
    assert job.failure is not None
    assert (job.state, job.failure.kind, job.failure.payment_sent) == ("failed", "timeout", True)
    assert "timeout_s ran out before another runner was called" in job.failure.message
    assert [(a.outcome, a.payment_sent) for a in job.attempts] == [("refused", True)]
    assert len(transport.calls) == 1  # the second runner was never called
    assert await engine.jobs.get(alice, job.id) == job
    assert engine.admission.running == 0


class TakesItsTime:
    """A credential resolver whose lookup takes some of the job's time before any runner is called."""

    def __init__(self, loop_time: LoopTime, seconds: float) -> None:
        self._loop_time, self._seconds = loop_time, seconds

    async def credential(self, actor: ActorContext) -> PaymentKey:
        del actor
        self._loop_time.advance(self._seconds)
        return PaymentKey(token=None, allocation_id=None)


async def test_an_immediate_jobs_time_counts_from_its_start_not_from_its_first_attempt(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    loop_time: LoopTime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the engine does before the first runner call is part of the job: the first attempt is given
    ``timeout_s`` less the time already gone."""
    supply = EngineSupply(credentials=TakesItsTime(loop_time, 10))  # type: ignore[arg-type]
    engine = await make_engine(supply=supply)
    seen = timeouts_given(transport, monkeypatch)
    transport.steps = [ScriptedTransport.ok()]

    await engine.jobs.run(alice, JobRequest(app=APP, timeout_s=30))

    (first,) = seen
    assert 0 < first <= 20


async def test_a_paid_attempt_that_timed_out_still_ends_the_job_uncertain_and_is_not_failed_over(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport
) -> None:
    """Not changed by the one deadline: the runner may have run the work, so nobody else is paid."""
    engine = await make_engine(TWO_ORCHESTRATORS)
    await allot(engine, alice)
    timed_out = RunnerCallError("timeout", "no answer in time", payment_sent=True, manifest_ids=("m-1",))
    transport.steps = [timed_out, ScriptedTransport.ok()]

    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP, timeout_s=30, max_attempts=2))

    assert failed.value.job.state == "uncertain"
    assert len(transport.calls) == 1


def the_attempt_write_takes(
    engine: Any, monkeypatch: pytest.MonkeyPatch, loop_time: LoopTime, seconds: float, *, on: int = 1
) -> None:
    """The store writes attempt number ``on`` and only answers when ``seconds`` of the job's time have
    gone: a slow database, once, between the engine's look at the clock and its call to the runner."""
    start_attempt, slow_writes = engine.store.start_attempt, [seconds]

    async def slow(attempt: Attempt, **fence: Any) -> Any:
        written = await start_attempt(attempt, **fence)
        if attempt.number == on and slow_writes:
            loop_time.advance(slow_writes.pop())
        return written

    monkeypatch.setattr(engine.store, "start_attempt", slow)


async def test_no_runner_is_called_when_the_time_ran_out_while_the_attempt_was_being_written(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    loop_time: LoopTime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The time left was measured, and then the write of the attempt row took all of it. The runner
    was still called, after the deadline, with what had been left before the write. Now the time is
    measured again once the row is written: the runner is not called, the row says so, and the job
    ends ``timeout``, unpaid. Nothing reached a runner, so the same reference may run again."""
    engine = await make_engine()
    await allot(engine, alice)
    the_attempt_write_takes(engine, monkeypatch, loop_time, 31)
    transport.steps = [ScriptedTransport.ok(), ScriptedTransport.ok()]
    request = JobRequest(app=APP, timeout_s=30, operation_ref="order-1")

    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, request)

    job = failed.value.job
    assert job.failure is not None
    assert (job.state, job.failure.kind, job.failure.payment_sent) == ("failed", "timeout", False)
    assert job.failure.message == "timeout_s ran out before a runner was called"
    assert transport.calls == []  # no runner was called after the deadline
    (attempt,) = job.attempts  # the row that was written before the call that was never made
    assert (attempt.outcome, attempt.payment_sent, attempt.status_code) == ("refused", False, None)
    assert (attempt.sessions, attempt.timings, attempt.ended_at) == ((), None, engine.clock.now())
    assert await engine.jobs.get(alice, job.id) == job
    assert (await engine.costs.for_job(alice, job.id)).status == "none"
    assert engine.admission.running == 0

    again = await engine.jobs.run(alice, request)  # it never reached a runner: the reference runs again
    assert (again.job.state, again.job.id != job.id, len(transport.calls)) == ("succeeded", True, 1)


async def test_a_failover_whose_attempt_write_used_up_the_time_calls_no_second_runner(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    loop_time: LoopTime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await make_engine(TWO_ORCHESTRATORS)
    await allot(engine, alice)
    the_attempt_write_takes(engine, monkeypatch, loop_time, 31, on=2)
    transport.steps = [refusal(paid=True), ScriptedTransport.ok()]

    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP, timeout_s=30, max_attempts=2))

    job = failed.value.job
    assert job.failure is not None
    assert (job.state, job.failure.kind, job.failure.payment_sent) == ("failed", "timeout", True)
    assert job.failure.message == "timeout_s ran out before another runner was called"
    assert len(transport.calls) == 1  # the runner that refused, and no other
    assert [(a.outcome, a.payment_sent, a.status_code) for a in job.attempts] == [
        ("refused", True, 503),
        ("refused", False, None),  # written, and never called
    ]
