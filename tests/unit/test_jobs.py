"""ISS-02 / ISS-05: immediate jobs, failover, idempotency, write ordering, recovery, admission."""

import asyncio
import hashlib
from collections.abc import Sequence
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from livepeer_builder import EngineSupply
from livepeer_builder.contracts import (
    ActorContext,
    Attempt,
    Job,
    JobRequest,
    Money,
    ProvisionRequest,
    Runner,
)
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
            application_id="default",
            actor_id=actor_id,
            amount=Money(amount=Decimal(5), currency="USD"),
            grant_id="grant_1",
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


async def test_fails_over_and_never_pays_the_same_orchestrator_twice(
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
    assert len(failed.value.job.attempts) == 1  # r2 shares the orchestrator that took the payment


ORCH_A, ORCH_B = "https://orch-a.example", "https://orch-b.example"


def two_orchestrators() -> tuple[Runner, ...]:
    return (
        runner(url="https://a1", orchestrator_url=ORCH_A),
        runner(url="https://a2", orchestrator_url=ORCH_A),
        runner(url="https://b1", orchestrator_url=ORCH_B),
    )


async def test_a_paid_failure_that_did_not_run_fails_over_to_another_orchestrator(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport
) -> None:
    """A paid 404 is the orchestrator, before the app ran. The other statuses are in test_attempt_verdict."""
    engine = await provisioned(make_engine, admin, "alice", runners=two_orchestrators())
    transport.steps = [
        RunnerCallError("http", "refused", payment_sent=True, manifest_ids=("m-1",), status_code=404),
        ScriptedTransport.ok(manifest_ids=("m-2",)),
    ]
    result = await engine.jobs.run(alice, JobRequest(app=APP, max_attempts=2))
    assert result.job.state == "succeeded"
    assert [url for url, _ in transport.calls] == ["https://a1", "https://b1"]  # a2: same orchestrator
    stored = await engine.jobs.get(alice, result.job.id)
    assert [(a.outcome, a.payment_sent, a.orchestrator_url) for a in stored.attempts] == [
        ("http", True, ORCH_A),
        ("succeeded", True, ORCH_B),
    ]
    assert [[s.manifest_id for s in a.sessions] for a in stored.attempts] == [["m-1"], ["m-2"]]


async def test_a_paid_request_error_does_not_fail_over(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await provisioned(make_engine, admin, "alice", runners=two_orchestrators())
    transport.steps = [
        RunnerCallError("http", "bad", payment_sent=True, manifest_ids=("m-1",), status_code=400)
    ]
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP))
    assert (
        failed.value.job.state == "failed" and len(failed.value.job.attempts) == 1
    )  # it would fail everywhere
    assert failed.value.job.failure is not None and failed.value.job.failure.kind == "runner_rejected"


async def test_paid_failures_everywhere_fail_with_the_last_runner_s_words(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await provisioned(make_engine, admin, "alice", runners=two_orchestrators())
    transport.steps = [
        RunnerCallError("refused", "busy", payment_sent=True, manifest_ids=("m-1",), status_code=503),
        RunnerCallError(
            "http",
            "gone",
            payment_sent=True,
            manifest_ids=("m-2",),
            status_code=404,
            body="runner session not found",
        ),
    ]
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP))
    job = failed.value.job
    assert job.state == "failed" and len(job.attempts) == 2  # neither failure ran the work: not uncertain
    assert job.failure is not None
    # an orchestrator's 404 is a refusal before the work ran: `refused` (503, retry later), not a 404
    assert (job.failure.kind, job.failure.status_code, job.failure.body) == (
        "refused",
        404,
        "runner session not found",
    )
    assert job.failure.payment_sent and job.failure.message == "all 2 runners failed; last: gone"


@pytest.mark.parametrize(("status", "state"), [(500, "failed"), (502, "uncertain")])
async def test_a_paid_5xx_never_runs_the_work_again(
    make_engine: EngineFactory,
    admin: ActorContext,
    alice: ActorContext,
    transport: ScriptedTransport,
    status: int,
    state: str,
) -> None:
    """A 500 is the app's own error after it ran. A 502 may come while it still runs. 504 is in the table."""
    engine = await provisioned(make_engine, admin, "alice", runners=two_orchestrators())
    transport.steps = [
        RunnerCallError("http", "5xx", payment_sent=True, manifest_ids=("m-1",), status_code=status)
    ]
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP))
    assert failed.value.job.state == state and len(failed.value.job.attempts) == 1


async def test_the_job_failure_says_paid_when_an_earlier_attempt_paid(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await provisioned(make_engine, admin, "alice", runners=two_orchestrators())
    transport.steps = [
        RunnerCallError("refused", "busy", payment_sent=True, manifest_ids=("m-1",), status_code=503),
        RunnerCallError("http", "bad request", payment_sent=False, status_code=400),
    ]
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP))
    job = failed.value.job
    assert job.state == "failed" and [a.payment_sent for a in job.attempts] == [True, False]
    assert job.failure is not None and job.failure.payment_sent  # the allocation paid orchestrator A


async def test_an_unknown_payment_never_pays_another_orchestrator(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await provisioned(make_engine, admin, "alice", runners=two_orchestrators())
    transport.steps = [
        RunnerCallError("unreachable", "signer reply lost", payment_sent=False, payment_unknown=True)
    ]
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP))
    job = failed.value.job
    assert job.state == "uncertain" and len(job.attempts) == 1  # the signer may have charged
    assert job.failure is not None and not job.failure.payment_sent
    assert job.failure.kind == "unreachable" and job.attempts[0].outcome == "payment_unknown"  # stored


async def test_a_selection_policy_that_selects_nobody_fails_before_payment(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport
) -> None:
    class Nobody:
        def order(self, candidates: Sequence[Runner], request: JobRequest) -> Sequence[Runner]:
            return ()

    engine = await provisioned(make_engine, admin, "alice", supply=EngineSupply(selection=Nobody()))
    with pytest.raises(JobFailed) as failed:
        await engine.jobs.run(alice, JobRequest(app=APP))
    job = failed.value.job
    assert job.state == "failed" and job.attempts == () and transport.calls == []
    assert job.failure is not None and job.failure.kind == "no_offering"


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
    seen_during_call: list[Job] = []

    class Observing(ScriptedTransport):
        async def call(self, r, request, credential, on_signed=None):  # type: ignore[no-untyped-def]
            seen_during_call.extend(await store.jobs_in_state("running"))
            return ScriptedTransport.ok()

    engine = await provisioned(make_engine, admin, "alice", supply=EngineSupply(transport=Observing()))
    await engine.jobs.run(alice, JobRequest(app=APP))
    assert len(seen_during_call) == 1
    assert [a.outcome for a in seen_during_call[0].attempts] == ["in_flight"]


def running_job(clock: FixedClock) -> Job:
    return Job(
        id=uuid4(), actor_id="alice", application_id="default", operation_ref=None, mode="immediate", app=APP,
        capability=None, model=None, state="running", attempts=(), failure=None, result=None, usage=None,
        created_at=clock.now(), completed_at=None,
    )  # fmt: skip


async def test_restart_recovery_marks_maybe_paid_jobs_uncertain(
    make_engine: EngineFactory, store: SqlStore, clock: FixedClock
) -> None:
    engine = await make_engine()
    job = running_job(clock)
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


async def test_restart_after_an_unknown_payment_stays_uncertain(
    make_engine: EngineFactory, store: SqlStore, clock: FixedClock
) -> None:
    """The attempt was stored as payment_unknown, then the engine stopped before the job's end was written."""
    engine = await make_engine()
    job = running_job(clock)
    await store.create_job(job, None)
    attempt = Attempt(
        job_id=job.id, number=1, runner_url="https://r1", orchestrator_url=None, allocation_id="a", sessions=(),
        payment_sent=False, outcome="in_flight", status_code=None, started_at=clock.now(), ended_at=None,
    )  # fmt: skip
    await store.start_attempt(attempt)
    await store.finish_attempt(attempt.model_copy(update={"outcome": "payment_unknown"}))
    assert await engine.jobs.recover_interrupted() == 1
    recovered = await store.get_job(job.id)
    assert recovered is not None and recovered.state == "uncertain"  # never `failed`: it may have paid


async def test_a_payload_that_is_not_json_is_refused_before_any_attempt() -> None:
    with pytest.raises(ValidationError, match="payload must be JSON"):
        JobRequest(app=APP, payload={"amount": Decimal("1.5")})


async def test_restart_between_attempts_after_a_paid_failover_is_failed(
    make_engine: EngineFactory, store: SqlStore, clock: FixedClock
) -> None:
    """Attempt 1 was paid and refused (it failed over); the engine stopped before attempt 2 began."""
    engine = await make_engine()
    job = running_job(clock)
    await store.create_job(job, None)
    attempt = Attempt(
        job_id=job.id, number=1, runner_url="https://r1", orchestrator_url=None, allocation_id="a", sessions=(),
        payment_sent=False, outcome="in_flight", status_code=None, started_at=clock.now(), ended_at=None,
    )  # fmt: skip
    await store.start_attempt(attempt)
    await store.finish_attempt(
        attempt.model_copy(update={"payment_sent": True, "outcome": "refused", "status_code": 503})
    )
    assert await engine.jobs.recover_interrupted() == 1
    recovered = await store.get_job(job.id)
    assert recovered is not None and recovered.state == "failed"  # nothing could have run
    assert recovered.failure is not None and recovered.failure.payment_sent


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
    release, entered = asyncio.Event(), asyncio.Event()

    class Slow(ScriptedTransport):
        async def call(self, r, request, credential, on_signed=None):  # type: ignore[no-untyped-def]
            entered.set()  # the first job now holds the only admission slot
            await release.wait()
            return ScriptedTransport.ok()

    engine = await provisioned(
        make_engine, admin, "alice", max_concurrent_jobs=1, supply=EngineSupply(transport=Slow())
    )
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
    with pytest.raises(NotFound):  # D44: permission and ownership come before the 501; the owner's 501 is
        await engine.jobs.result(alice, uuid4())  # in test_ownership.py


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
