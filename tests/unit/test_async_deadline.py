"""ISS-26: an async job has one deadline, set when it is claimed.

``timeout_s`` counts from the claim, once: each attempt is called with the time that is left, and none
is started once it has run out, so a job that fails over is never given ``timeout_s`` again for every
runner it tries. It is the rule of a job that waits for its answer (``test_job_deadline.py``), counted
from the claim, since a queued job has not started. The worker also stops a dispatch whose transport
ignores its timeout, so no runner call holds a worker and an admission slot for ever.

No test here waits for a job's time to pass: ``LoopTime`` moves the clock the deadline is read from.
"""

import asyncio

import pytest

from livepeer_builder import EngineSupply
from livepeer_builder.contracts import ActorContext
from livepeer_builder.errors import JobFailed
from livepeer_builder.testing import ScriptedTransport
from tests.conftest import EngineFactory, allot
from tests.store.test_async_restart import PaysThenWaits
from tests.unit.test_async_jobs import calls, queued
from tests.unit.test_job_deadline import (  # noqa: F401 - loop_time: a fixture
    TWO_ORCHESTRATORS,
    LoopTime,
    loop_time,
    refuses_after,
    the_attempt_write_takes,
    timeouts_given,
)


async def test_the_attempts_of_an_async_job_share_one_timeout(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    loop_time: LoopTime,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first runner refuses when 25 of the job's 30 seconds are gone, and the job fails over. The
    second runner is given the 5 seconds that are left, not 30 again. The time the job waited in the
    queue is not part of it: an hour passes before the claim, and the first runner still gets it all."""
    engine = await make_engine(TWO_ORCHESTRATORS)
    await allot(engine, alice)
    seen = timeouts_given(transport, monkeypatch)
    transport.steps = [refuses_after(loop_time, 25), ScriptedTransport.ok()]
    job = await engine.jobs.submit(alice, queued(timeout_s=30, max_attempts=2))
    loop_time.advance(3600)  # queued: its time has not begun

    assert await engine.jobs.worker.run_once() == 1

    assert (await engine.jobs.get(alice, job.id)).state == "succeeded"
    first, second = seen
    assert 25 < first <= 30
    assert 0 < second <= 5


async def test_no_runner_is_called_or_paid_once_the_time_has_run_out(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    loop_time: LoopTime,  # noqa: F811
) -> None:
    """A paid attempt that fails over when the job's time is up is the last one. The job ends
    ``timeout``, ``failed``: the attempt it made was refused, so nothing is left in doubt."""
    engine = await make_engine(TWO_ORCHESTRATORS)
    await allot(engine, alice)
    transport.steps = [refuses_after(loop_time, 31, paid=True), ScriptedTransport.ok()]
    job = await engine.jobs.submit(alice, queued(timeout_s=30, max_attempts=2))

    assert await engine.jobs.worker.run_once() == 1

    ended = await engine.jobs.get(alice, job.id)
    assert ended.failure is not None
    assert (ended.state, ended.failure.kind, ended.failure.payment_sent) == ("failed", "timeout", True)
    assert "timeout_s ran out before another runner was called" in ended.failure.message
    assert [(a.outcome, a.payment_sent) for a in ended.attempts] == [("refused", True)]
    assert len(transport.calls) == 1  # the second runner was never called
    assert engine.admission.running == 0


async def test_a_job_whose_time_ran_out_before_its_first_attempt_calls_no_runner(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport
) -> None:
    """The paid call, told a deadline that has passed: no attempt is written and nobody is called."""
    engine = await make_engine()
    await allot(engine, alice)
    job = await engine.jobs.submit(alice, queued())
    claimed = await engine.store.claim_job(job.id, engine.clock.now())
    assert claimed is not None
    running, request, actor, number = claimed
    past = asyncio.get_running_loop().time() - 1

    with pytest.raises(JobFailed) as failed:
        await engine.jobs._dispatcher.dispatch(running, actor, request, [], deadline=past, claim=number)

    failure = failed.value.job.failure
    assert failure is not None and (failure.kind, failure.payment_sent) == ("timeout", False)
    assert "timeout_s ran out before a runner was called" in failure.message
    stored = await engine.jobs.get(alice, job.id)
    assert (stored.state, stored.attempts, transport.calls) == ("failed", (), [])


async def test_a_transport_that_ignores_its_timeout_does_not_hold_a_worker_for_ever(
    make_engine: EngineFactory,
    alice: ActorContext,
    loop_time: LoopTime,  # noqa: F811
) -> None:
    """A supplied transport may not keep to ``timeout_s``. The worker gives it 5 seconds more than the
    job's time and then stops the call: the job ends as one the engine stopped, ``uncertain`` because
    it was paid, and its worker and its slot are free again."""
    never_answers = PaysThenWaits()
    engine = await make_engine(supply=EngineSupply(transport=never_answers))
    await allot(engine, alice)
    job = await engine.jobs.submit(alice, queued(timeout_s=30))
    step = asyncio.create_task(engine.jobs.worker.run_once())
    await calls(never_answers, 1)  # at the runner, and paid

    loop_time.advance(30 + 5 + 1)  # the job's time, the worker's patience, and a moment
    assert await asyncio.wait_for(step, timeout=10) == 1

    ended = await engine.jobs.get(alice, job.id)
    assert ended.failure is not None
    assert (ended.state, ended.failure.kind, ended.failure.payment_sent) == (
        "uncertain",
        "runner_error",
        True,
    )
    assert ended.failure.message == "engine error: TimeoutError"
    assert [(a.outcome, a.payment_sent) for a in ended.attempts] == [("in_flight", True)]
    assert (len(never_answers.calls), engine.admission.running) == (1, 0)


async def test_an_async_job_calls_no_runner_when_its_attempt_write_used_up_its_time(
    make_engine: EngineFactory,
    alice: ActorContext,
    transport: ScriptedTransport,
    loop_time: LoopTime,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For an async job the worker's backstop would otherwise cancel a call that began after the
    deadline, when its payment may already have gone out. No call begins: the job ends ``timeout``,
    ``failed``, with nothing paid, and its attempt row says the runner was never called."""
    engine = await make_engine()
    await allot(engine, alice)
    the_attempt_write_takes(engine, monkeypatch, loop_time, 31)
    transport.steps = [ScriptedTransport.ok()]
    job = await engine.jobs.submit(alice, queued(timeout_s=30))

    assert await engine.jobs.worker.run_once() == 1

    ended = await engine.jobs.get(alice, job.id)
    assert ended.failure is not None
    assert (ended.state, ended.failure.kind, ended.failure.payment_sent) == ("failed", "timeout", False)
    assert [(a.outcome, a.payment_sent, a.status_code) for a in ended.attempts] == [("refused", False, None)]
    assert (transport.calls, engine.admission.running) == ([], 0)
