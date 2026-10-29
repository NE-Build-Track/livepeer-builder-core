"""The repeat rules for jobs (D57, docs/contracts/engine.md#idempotency), as pure functions: no I/O."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from livepeer_builder.contracts import Attempt, AttemptOutcome, Failure, FailureKind, Job, JobState
from livepeer_builder.services.operations import never_reached_a_runner

NOW = datetime(2026, 10, 29, tzinfo=UTC)


def attempt(
    job_id: UUID, outcome: AttemptOutcome, *, paid: bool = False, status_code: int | None = None
) -> Attempt:
    return Attempt(
        job_id=job_id, number=1, runner_url="https://r", orchestrator_url="https://o", allocation_id=None,
        sessions=(), payment_sent=paid, outcome=outcome, status_code=status_code, started_at=NOW, ended_at=NOW,
    )  # fmt: skip


def job(
    state: JobState = "failed",
    kind: FailureKind | None = None,
    *outcomes: AttemptOutcome,
    paid: bool = False,
    status_code: int | None = None,
) -> Job:
    job_id = uuid4()
    return Job(
        id=job_id, actor_id="alice", application_id="default", operation_ref="op-1", mode="immediate",
        app="a/b", capability=None, model=None, state=state,
        attempts=tuple(
            attempt(job_id, outcome, paid=paid, status_code=status_code).model_copy(update={"number": n})
            for n, outcome in enumerate(outcomes, start=1)
        ),
        failure=None if kind is None else Failure(kind=kind, message="m", payment_sent=paid),
        result=None, usage=None, created_at=NOW, completed_at=NOW,
    )  # fmt: skip


def test_a_job_that_failed_for_no_capacity_never_reached_a_runner() -> None:
    assert never_reached_a_runner(job("failed", "no_capacity"))


def test_a_job_that_ran_did_reach_a_runner() -> None:
    assert not never_reached_a_runner(job("succeeded", None, "succeeded", paid=True))
    assert not never_reached_a_runner(job("uncertain", "timeout", "timeout", paid=True))


def test_a_failed_job_whose_runner_answered_did_reach_it() -> None:
    assert not never_reached_a_runner(job("failed", "runner_error", "http", paid=True, status_code=500))
    assert not never_reached_a_runner(job("failed", "runner_rejected", "http", status_code=422))  # unpaid


def test_a_job_that_failed_before_dispatch_never_reached_a_runner() -> None:
    assert never_reached_a_runner(job("failed", "no_offering"))
    assert never_reached_a_runner(job("failed", "not_provisioned"))


def test_a_job_every_orchestrator_refused_before_the_app_ran_never_reached_a_runner() -> None:
    assert never_reached_a_runner(job("failed", "refused", "refused", "refused", status_code=503))
    assert never_reached_a_runner(job("failed", "refused", "http", status_code=404))
    assert never_reached_a_runner(job("failed", "refused", "http", status_code=409))


def test_a_job_the_signer_refused_never_reached_a_runner() -> None:
    assert never_reached_a_runner(job("failed", "payment", "payment", status_code=402))
    assert never_reached_a_runner(job("failed", "payment", "payment_refused", status_code=481))


def test_a_paid_attempt_means_the_work_may_have_been_accepted() -> None:
    assert not never_reached_a_runner(job("failed", "refused", "refused", paid=True, status_code=503))
    failure = Failure(kind="refused", message="m", payment_sent=True)  # an earlier attempt was paid (D52)
    assert not never_reached_a_runner(
        job("failed", "refused", "refused").model_copy(update={"failure": failure})
    )


def test_unreachable_and_unpaid_timeout_are_not_retried_q2() -> None:
    assert not never_reached_a_runner(job("failed", "unreachable", "unreachable"))
    assert not never_reached_a_runner(job("failed", "timeout", "timeout"))
    assert not never_reached_a_runner(job("failed", "refused", "refused", "unreachable"))  # mixed


def test_a_running_job_or_one_with_no_failure_is_not_retried() -> None:
    assert not never_reached_a_runner(job("running", None))
    assert not never_reached_a_runner(job("failed", None, "refused"))  # no failure recorded
    assert not never_reached_a_runner(job("failed", "interrupted"))  # no attempt says what happened


def test_only_a_failed_job_is_retried_whatever_its_failure_says() -> None:
    assert not never_reached_a_runner(job("uncertain", "no_capacity"))


def test_a_paid_attempt_counts_even_when_the_failure_does_not_say_so() -> None:
    paid_attempt = job("failed", "refused", "refused", paid=True, status_code=503)
    unpaid_failure = Failure(kind="refused", message="m", payment_sent=False)
    assert not never_reached_a_runner(paid_attempt.model_copy(update={"failure": unpaid_failure}))
