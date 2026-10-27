"""Failover: what a failed attempt means for its job, and which runner is tried next (D51).

A failed attempt either moves the job to the next runner (``failover``), or ends it ``failed`` or
``uncertain``. Payment alone does not bind a job to a runner: an attempt that was paid and refused
before it ran, or that the runner or orchestrator failed, fails over to a runner on another
orchestrator. Its charge stays on the allocation as ``failed_after_payment`` and is never billable
(services/costs.py). What stops a paid job is a failure that may have run the work (``uncertain``) or
one that would repeat, and be charged, everywhere (``failed``).
"""

from collections.abc import Iterator, Sequence
from typing import Literal

from livepeer_builder.contracts import AttemptOutcome, Failure, Runner
from livepeer_builder.errors import RunnerCallError
from livepeer_builder.services.streams import failure_kind
from livepeer_builder.utils import BODY_LIMIT

Verdict = Literal["failover", "failed", "uncertain"]

FAILOVER_OUTCOMES: frozenset[AttemptOutcome] = frozenset({"refused", "unreachable", "payment_refused"})
UNCERTAIN_OUTCOMES: frozenset[AttemptOutcome] = frozenset({"unreachable", "timeout"})  # paid: it may have run
_PAID_FAILOVER_OUTCOMES: frozenset[AttemptOutcome] = frozenset({"refused", "payment_refused"})
# 404 and 409: the orchestrator lost the runner or its session (a runner stop, livepeer-python-gateway#83),
# or the session already exists. Any other 4xx is about the request and would fail on every runner.
_PAID_FAILOVER_STATUSES = frozenset({404, 409})


def attempt_verdict(outcome: AttemptOutcome, status_code: int | None, paid: bool) -> Verdict:
    """What a failed attempt means for its job (docs/contracts/engine.md#behavior-the-contract-fixes)."""
    if not paid:
        return "failover" if outcome in FAILOVER_OUTCOMES else "failed"
    if outcome in UNCERTAIN_OUTCOMES:
        return "uncertain"
    if outcome in _PAID_FAILOVER_OUTCOMES:
        return "failover"
    runner_side = status_code is not None and (status_code >= 500 or status_code in _PAID_FAILOVER_STATUSES)
    return "failover" if outcome == "http" and runner_side else "failed"


def failure_of(error: RunnerCallError, paid: bool, tried: int = 1) -> Failure:
    """The failure a job ends with, in the last runner's words. ``tried`` counts the runners it failed on."""
    return Failure(
        kind=failure_kind(error.kind, error.status_code),
        message=str(error) if tried == 1 else f"all {tried} runners failed; last: {error}",
        status_code=error.status_code,
        body=error.body[:BODY_LIMIT] if error.body else None,
        payment_sent=paid,
    )


class Candidates:
    """The runners a job may try, in selection order, at most ``max_attempts`` of them.

    ``exclude`` drops every later runner on an orchestrator, so one that took a payment and failed is
    never paid again for the same job. A skipped runner does not count as an attempt.
    """

    def __init__(self, ordered: Sequence[Runner], max_attempts: int) -> None:
        self._ordered = tuple(ordered)
        self._max_attempts = max_attempts
        self._excluded: set[str] = set()

    def __iter__(self) -> Iterator[Runner]:
        tried = 0
        for runner in self._ordered:
            if tried >= self._max_attempts:
                return
            if runner.orchestrator_url is not None and runner.orchestrator_url in self._excluded:
                continue
            tried += 1
            yield runner

    def exclude(self, runner: Runner) -> None:
        if runner.orchestrator_url is not None:
            self._excluded.add(runner.orchestrator_url)
