"""D52: what a failed attempt means for its job, and which runner is tried next."""

import pytest

from livepeer_builder.contracts import AttemptOutcome
from livepeer_builder.errors import RunnerCallError
from livepeer_builder.services.failover import Candidates, Verdict, attempt_verdict, failure_of
from livepeer_builder.testing import runner


@pytest.mark.parametrize(
    ("outcome", "status", "paid", "verdict"),
    [
        # before payment: only a refusal, no answer, or a signer refusal for this orchestrator moves on
        ("refused", 503, False, "failover"),
        ("unreachable", None, False, "failover"),
        ("payment_refused", 400, False, "failover"),
        ("payment", 402, False, "failed"),
        ("timeout", None, False, "failed"),
        ("http", 500, False, "failed"),
        # after payment: the runner or orchestrator failed it, so another orchestrator may succeed
        ("refused", 503, True, "failover"),
        ("payment_refused", 481, True, "failover"),
        ("http", 404, True, "failover"),  # the runner stopped the session (livepeer-python-gateway#83)
        ("http", 409, True, "failover"),
        ("http", 500, True, "failover"),
        ("http", 502, True, "failover"),
        # after payment: it may have run, or it would fail and be charged everywhere
        ("timeout", None, True, "uncertain"),
        ("unreachable", None, True, "uncertain"),
        ("http", 400, True, "failed"),
        ("http", 422, True, "failed"),
        ("http", None, True, "failed"),
        ("payment", 402, True, "failed"),
    ],
)
def test_attempt_verdict(outcome: AttemptOutcome, status: int | None, paid: bool, verdict: Verdict) -> None:
    assert attempt_verdict(outcome, status, paid) == verdict


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


def test_a_runner_without_an_orchestrator_is_never_excluded() -> None:
    loose = runner(url="https://r1", orchestrator_url=None)
    other = runner(url="https://r2", orchestrator_url=None)
    candidates = Candidates((loose, other), max_attempts=3)
    candidates.exclude(loose)
    assert [candidate.url for candidate in candidates] == ["https://r1", "https://r2"]


def test_failure_of_counts_the_runners_tried() -> None:
    error = RunnerCallError("http", "boom", payment_sent=True, status_code=500, body="x" * 5000)
    one, three = failure_of(error, paid=True), failure_of(error, paid=True, tried=3)
    assert (one.kind, one.message, one.payment_sent) == ("runner_error", "boom", True)
    assert three.message == "all 3 runners failed; last: boom"
    assert one.body is not None and len(one.body) < 5000  # bounded
