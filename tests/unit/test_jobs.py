"""ISS-02 / ISS-05: immediate jobs, failover, idempotency, write ordering, recovery, admission."""

import asyncio
import hashlib
from decimal import Decimal
from uuid import uuid4

import pytest

from livepeer_builder.contracts import ActorContext, Attempt, Job, JobRequest, Money, ProvisionRequest
from livepeer_builder.errors import (
    JobFailed,
    NoCapacity,
    NotFound,
    NotImplementedInMilestone,
    OperationConflict,
    OperationExists,
    RunnerCallError,
)
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FixedClock, ScriptedTransport, runner
from tests.conftest import EngineFactory

APP = "example/echo"


async def provisioned(make_engine: EngineFactory, admin: ActorContext, *actors: str, **settings: object):  # type: ignore[no-untyped-def]
    engine = await make_engine(**settings)
    for actor_id in actors:
        request = ProvisionRequest(
            application_id="default", actor_id=actor_id, amount=Money(amount=Decimal(5), currency="USD")
        )
        await engine.payments.provision(admin, request)
    return engine


async def test_success_records_attempt_payment_and_result_reference(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await provisioned(make_engine, admin, "alice")
    transport.steps = [ScriptedTransport.ok(b'{"echo": 1}', manifest_ids=("manifest-1",))]
    result = await engine.jobs.run(alice, JobRequest(app=APP, path="/echo", payload={"x": 1}))
    assert result.content == b'{"echo": 1}'
    assert result.job.state == "succeeded"
    assert result.job.result is not None
    assert result.job.result.sha256 == hashlib.sha256(b'{"echo": 1}').hexdigest()
    stored = await engine.jobs.get(alice, result.job.id)
    assert [
        (a.outcome, tuple(s.manifest_id for s in a.sessions), a.payment_sent) for a in stored.attempts
    ] == [("succeeded", ("manifest-1",), True)]
    assert stored.attempts[0].allocation_id is not None
    assert transport.calls[0][1] is not None and transport.calls[0][1].startswith("lpg_")  # actor's own key


async def test_fails_over_only_before_payment(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await provisioned(
        make_engine, admin, "alice", runners=(runner(url="https://r1"), runner(url="https://r2"))
    )
    transport.steps = [RunnerCallError("refused", "503", payment_sent=False), ScriptedTransport.ok()]
    result = await engine.jobs.run(alice, JobRequest(app=APP))
    assert [a.outcome for a in result.job.attempts] == ["refused", "succeeded"]

    transport.steps = [
        RunnerCallError("http", "500", payment_sent=True, manifest_ids=("m-9",), status_code=500)
    ]
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP))
    assert failed.value.job.state == "failed"
    assert failed.value.job.failure is not None and failed.value.job.failure.kind == "runner_error"
    assert len(failed.value.job.attempts) == 1  # bound to the paid runner; no failover


async def test_signer_refusal_for_one_orchestrator_fails_over(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await provisioned(
        make_engine,
        admin,
        "alice",
        runners=(runner(url="https://r1"), runner(url="https://r2"), runner(url="https://r3")),
    )
    transport.steps = [
        RunnerCallError("payment_refused", "400 incomplete params", payment_sent=False, status_code=400),
        ScriptedTransport.ok(),
    ]
    result = await engine.jobs.run(alice, JobRequest(app=APP))
    assert [a.outcome for a in result.job.attempts] == ["payment_refused", "succeeded"]


async def test_signer_refusal_everywhere_fails_as_payment(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await provisioned(
        make_engine, admin, "alice", runners=(runner(url="https://r1"), runner(url="https://r2"))
    )
    transport.steps = [RunnerCallError("payment_refused", "400", payment_sent=False, status_code=400)] * 2
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP))
    assert len(failed.value.job.attempts) == 2
    assert failed.value.job.failure is not None and failed.value.job.failure.kind == "payment"


async def test_caller_side_payment_refusal_does_not_fail_over(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await provisioned(
        make_engine, admin, "alice", runners=(runner(url="https://r1"), runner(url="https://r2"))
    )
    transport.steps = [
        RunnerCallError("payment", "402 allowance exhausted", payment_sent=False, status_code=402)
    ]
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP))
    assert len(failed.value.job.attempts) == 1  # every orchestrator would refuse the same caller


async def test_paid_then_unreachable_is_uncertain_never_failed(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await provisioned(make_engine, admin, "alice")
    transport.steps = [RunnerCallError("timeout", "timed out", payment_sent=True, manifest_ids=("m-2",))]
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP))
    assert failed.value.job.state == "uncertain"


async def test_attempt_is_written_before_the_call(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, store: SqlStore
) -> None:
    engine = await provisioned(make_engine, admin, "alice")
    seen_during_call: list[Job] = []

    class Observing(ScriptedTransport):
        async def call(self, r, request, credential):  # type: ignore[no-untyped-def]
            seen_during_call.extend(await store.jobs_in_state("running"))
            return ScriptedTransport.ok()

    engine.jobs._transport = Observing()
    await engine.jobs.run(alice, JobRequest(app=APP))
    assert len(seen_during_call) == 1
    assert [a.outcome for a in seen_during_call[0].attempts] == ["in_flight"]


async def test_restart_recovery_marks_maybe_paid_jobs_uncertain(
    make_engine: EngineFactory, store: SqlStore, clock: FixedClock
) -> None:
    engine = await make_engine()
    job = Job(
        id=uuid4(), actor_id="alice", application_id="default", operation_ref=None, mode="immediate", app=APP,
        capability=None, model=None, state="running", attempts=(), failure=None, result=None, usage=None,
        created_at=clock.now(), completed_at=None,
    )  # fmt: skip
    await store.create_job(job, None)
    await store.start_attempt(
        Attempt(
            job_id=job.id, number=1, runner_url="https://r1", orchestrator_url=None, allocation_id=None, sessions=(),
            payment_sent=False, outcome="in_flight", status_code=None, started_at=clock.now(), ended_at=None,
        )
    )  # fmt: skip
    assert await engine.jobs.recover_interrupted() == 1
    recovered = await store.get_job(job.id)
    assert recovered is not None and recovered.state == "uncertain"
    assert recovered.failure is not None and recovered.failure.kind == "interrupted"


async def test_operation_ref_dispatches_at_most_once(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await provisioned(make_engine, admin, "alice")
    transport.steps = [ScriptedTransport.ok()]
    first = await engine.jobs.run(alice, JobRequest(app=APP, payload={"a": 1}, operation_ref="order-1"))
    with pytest.raises(OperationExists) as again:
        await engine.jobs.run(
            alice, JobRequest(app=APP, payload={"a": 1}, operation_ref="order-1", timeout_s=9)
        )
    assert again.value.job.id == first.job.id
    with pytest.raises(OperationConflict):
        await engine.jobs.run(alice, JobRequest(app=APP, payload={"a": 2}, operation_ref="order-1"))
    assert len(transport.calls) == 1


async def test_unprovisioned_actor_fails_before_any_payment(
    make_engine: EngineFactory, bob: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await make_engine()
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(bob, JobRequest(app=APP))
    assert failed.value.job.failure is not None and failed.value.job.failure.kind == "not_provisioned"
    assert transport.calls == []  # no silent fallback to a shared key


async def test_no_offering_and_no_capacity(make_engine: EngineFactory, alice: ActorContext) -> None:
    engine = await make_engine(runners=(runner(capacity_available=0),))
    with pytest.raises(JobFailed) as missing:
        await engine.jobs.run(alice, JobRequest(app="nothing/here"))
    assert missing.value.job.failure is not None and missing.value.job.failure.kind == "no_offering"
    with pytest.raises(JobFailed) as busy:
        await engine.jobs.run(alice, JobRequest(app=APP))
    assert busy.value.job.failure is not None and busy.value.job.failure.kind == "no_capacity"


async def test_admission_refuses_before_dispatch(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext
) -> None:
    engine = await provisioned(make_engine, admin, "alice", max_concurrent_jobs=1)
    release, entered = asyncio.Event(), asyncio.Event()

    class Slow(ScriptedTransport):
        async def call(self, r, request, credential):  # type: ignore[no-untyped-def]
            entered.set()  # the first job now holds the only admission slot
            await release.wait()
            return ScriptedTransport.ok()

    engine.jobs._transport = Slow()
    first = asyncio.create_task(engine.jobs.run(alice, JobRequest(app=APP)))
    await entered.wait()  # wait for the condition, not a fixed time
    with pytest.raises(NoCapacity):
        await engine.jobs.run(alice, JobRequest(app=APP))
    release.set()
    assert (await first).job.state == "succeeded"


async def test_reserved_execution_modes_fail_loudly(make_engine: EngineFactory, alice: ActorContext) -> None:
    engine = await make_engine()
    with pytest.raises(NotImplementedInMilestone, match="M4"):
        await engine.jobs.run(alice, JobRequest(app=APP, mode="async"))
    with pytest.raises(NotImplementedInMilestone):
        await engine.jobs.result(alice, uuid4())


async def test_other_actors_jobs_are_invisible(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    bob: ActorContext,
    transport: ScriptedTransport,
) -> None:
    engine = await provisioned(make_engine, admin, "alice")
    transport.steps = [ScriptedTransport.ok()]
    result = await engine.jobs.run(alice, JobRequest(app=APP))
    with pytest.raises(NotFound):
        await engine.jobs.get(bob, result.job.id)
    assert await engine.jobs.list_jobs(bob) == []
    assert (await engine.jobs.get(admin, result.job.id)).id == result.job.id


async def test_usage_is_reported_once_by_the_owner(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await provisioned(make_engine, admin, "alice")
    transport.steps = [ScriptedTransport.ok()]
    result = await engine.jobs.run(alice, JobRequest(app=APP))
    updated = await engine.jobs.report_usage(alice, result.job.id, {"tokens_out": 42})
    assert updated.usage is not None and updated.usage.source == "app_reported"
    stored = await engine.jobs.get(alice, result.job.id)
    assert stored.usage is not None and stored.usage.quantities == {"tokens_out": 42}
    with pytest.raises(Exception, match="already reported"):
        await engine.jobs.report_usage(alice, result.job.id, {"tokens_out": 1})
