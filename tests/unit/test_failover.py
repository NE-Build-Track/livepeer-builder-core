"""D52: what a failed attempt means for its job, and which runner is tried next."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from livepeer_builder.contracts import Attempt, AttemptOutcome
from livepeer_builder.errors import RunnerCallError
from livepeer_builder.services.failover import (
    Candidates,
    Verdict,
    attempt_verdict,
    failure_of,
    orchestrator_of,
    payment_unresolved,
)
from livepeer_builder.testing import runner


@pytest.mark.parametrize(
    ("outcome", "status", "paid", "verdict"),
    [
        # before payment: a refusal, no answer, a signer refusal for this orchestrator, or the
        # orchestrator losing the runner (404, 409) moves on; nothing is spent by trying another
        ("refused", 503, False, "failover"),
        ("unreachable", None, False, "failover"),
        ("payment_refused", 400, False, "failover"),
        ("http", 404, False, "failover"),
        ("http", 409, False, "failover"),
        ("payment", 402, False, "failed"),
        ("timeout", None, False, "failed"),
        ("http", 500, False, "failed"),  # a free runner's own error: it ran
        ("http", 502, False, "failed"),
        # after payment, refused before the work ran: another orchestrator may succeed
        ("refused", 503, True, "failover"),
        ("payment_refused", 481, True, "failover"),
        ("http", 404, True, "failover"),  # the runner stopped the session (livepeer-python-gateway#83)
        ("http", 409, True, "failover"),
        # after payment: it may have run, so it is never run again
        ("timeout", None, True, "uncertain"),
        ("unreachable", None, True, "uncertain"),
        ("http", 502, True, "uncertain"),  # the proxy lost the runner, perhaps mid-answer
        ("http", 504, True, "uncertain"),
        # after payment: it ran and failed, or it would fail and be charged everywhere
        ("http", 500, True, "failed"),
        ("http", 400, True, "failed"),
        ("http", 422, True, "failed"),
        ("http", None, True, "failed"),
        ("payment", 402, True, "failed"),
    ],
)
def test_attempt_verdict(outcome: AttemptOutcome, status: int | None, paid: bool, verdict: Verdict) -> None:
    assert attempt_verdict(outcome, status, paid) == verdict


@pytest.mark.parametrize("outcome", ["unreachable", "timeout", "payment", "refused"])
def test_an_unknown_payment_stops_the_job_whatever_the_outcome(outcome: AttemptOutcome) -> None:
    assert attempt_verdict(outcome, None, False, payment_unknown=True) == "uncertain"
    assert attempt_verdict("payment_unknown", None, False) == "uncertain"  # as stored on the attempt


def test_candidates_skip_an_excluded_orchestrator_without_counting_it() -> None:
    a1 = runner(url="https://a1", orchestrator_url="https://orch-a.example")
    a2 = runner(url="https://a2", orchestrator_url="https://orch-a.example")
    b1 = runner(url="https://b1", orchestrator_url="https://orch-b.example")
    c1 = runner(url="https://c1", orchestrator_url="https://orch-c.example")
    candidates = Candidates((a1, a2, b1, c1), max_attempts=2)
    tried = []
    for candidate in candidates:
        tried.append(candidate.url)
        candidates.exclude(candidate)
    assert tried == ["https://a1", "https://b1"]  # a2 skipped, and c1 is past max_attempts


def test_a_runner_without_an_orchestrator_is_matched_by_its_own_origin() -> None:
    """A Live Runner is reached through its orchestrator, so its URL's origin names the orchestrator."""
    known = runner(url="https://orch.example/apps/a", orchestrator_url="https://ORCH.example:443/")
    same = runner(url="https://orch.example/apps/b", orchestrator_url=None)
    other = runner(url="https://other.example:8935/apps/c", orchestrator_url=None)
    candidates = Candidates((known, same, other), max_attempts=3)
    tried = []
    for candidate in candidates:
        tried.append(candidate.url)
        candidates.exclude(candidate)
    assert tried == ["https://orch.example/apps/a", "https://other.example:8935/apps/c"]
    assert orchestrator_of(other) == "https://other.example:8935"


def _attempt(number: int, outcome: AttemptOutcome, paid: bool, status: int | None = None) -> Attempt:
    return Attempt(
        job_id=uuid4(), number=number, runner_url="r", orchestrator_url=None, allocation_id="a", sessions=(),
        payment_sent=paid, outcome=outcome, status_code=status, started_at=datetime.now(UTC), ended_at=None,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("attempts", "unresolved"),
    [
        ((), False),
        ((_attempt(1, "in_flight", False),), True),  # the call was under way: it may have paid and run
        ((_attempt(1, "refused", True, 503), _attempt(2, "in_flight", False)), True),
        ((_attempt(1, "refused", True, 503),), False),  # failed over, and the next attempt never started
        ((_attempt(1, "succeeded", True, 200),), True),  # it ran, but the job's end was never written
        ((_attempt(1, "timeout", False),), False),
        (
            (_attempt(1, "payment_unknown", False),),
            True,
        ),  # the signer may have charged: stored, so a restart sees it
    ],
)
def test_only_the_last_attempt_can_leave_a_payment_unresolved(
    attempts: tuple[Attempt, ...], unresolved: bool
) -> None:
    assert payment_unresolved(attempts) is unresolved


def test_failure_of_counts_the_runners_tried() -> None:
    error = RunnerCallError("http", "boom", payment_sent=True, status_code=500, body="x" * 5000)
    one, three = failure_of(error, paid=True), failure_of(error, paid=True, tried=3)
    assert (one.kind, one.message, one.payment_sent) == ("runner_error", "boom", True)
    assert three.message == "all 3 runners failed; last: boom"
    assert one.body is not None and len(one.body) < 5000  # bounded


@pytest.mark.parametrize("status", [404, 409])
def test_an_orchestrator_refusal_is_reported_as_refused_not_as_the_runner_s_status(status: int) -> None:
    error = RunnerCallError("http", "runner session not found", payment_sent=True, status_code=status)
    failure = failure_of(error, paid=True, tried=2)
    assert (failure.kind, failure.status_code) == ("refused", status)  # REST answers 503, retry later
