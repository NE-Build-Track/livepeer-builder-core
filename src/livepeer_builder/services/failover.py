"""What a failed attempt means for its job, and which runner is tried next (D52)."""

import logging
from collections.abc import Iterator, Sequence
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from livepeer_builder.contracts import Attempt, AttemptOutcome, Failure, FailureKind, Runner
from livepeer_builder.errors import RunnerCallError
from livepeer_builder.protocols import Clock, JobRepo
from livepeer_builder.services.streams import failure_kind
from livepeer_builder.utils import BODY_LIMIT

_LOG = logging.getLogger(__name__)

Verdict = Literal["failover", "failed", "uncertain"]

FAILOVER_OUTCOMES: frozenset[AttemptOutcome] = frozenset({"refused", "unreachable", "payment_refused"})
UNCERTAIN_OUTCOMES: frozenset[AttemptOutcome] = frozenset({"unreachable", "timeout"})  # paid: it may have run
_PAID_FAILOVER_OUTCOMES: frozenset[AttemptOutcome] = frozenset({"refused", "payment_refused"})
# 404 and 409: the orchestrator refused before the app ran (a runner stop, gateway #83).
NOT_RUN_STATUSES = frozenset({404, 409})
# 502 and 504: the proxy lost the runner, so the work may have run. A 500 ran and failed.
MAY_HAVE_RUN_STATUSES = frozenset({502, 504})


def attempt_verdict(
    outcome: AttemptOutcome, status_code: int | None, paid: bool, *, payment_unknown: bool = False
) -> Verdict:
    """``payment_unknown`` stops the job: the signer may have charged, so nobody else is paid."""
    not_run = outcome == "http" and status_code in NOT_RUN_STATUSES
    if payment_unknown or outcome == "payment_unknown":
        return "uncertain"
    if not paid:
        return "failover" if outcome in FAILOVER_OUTCOMES or not_run else "failed"
    if outcome in UNCERTAIN_OUTCOMES or (outcome == "http" and status_code in MAY_HAVE_RUN_STATUSES):
        return "uncertain"
    return "failover" if outcome in _PAID_FAILOVER_OUTCOMES or not_run else "failed"


def payment_unresolved(attempts: Sequence[Attempt]) -> bool:
    """Only the last attempt can leave a restarted job ``uncertain``."""
    if not attempts:
        return False
    last = max(attempts, key=lambda attempt: attempt.number)
    if last.outcome in ("in_flight", "payment_unknown"):  # under way, or the signer may have charged
        return True
    return last.payment_sent and attempt_verdict(last.outcome, last.status_code, True) != "failover"


async def recover_interrupted(store: JobRepo, clock: Clock) -> int:
    """Resolve jobs a restart left ``running``. One that may hold a payment for work with an unknown
    outcome becomes ``uncertain``; attempts it failed over from do not count (D52)."""
    recovered = 0
    for job in await store.jobs_in_state("running"):
        maybe_paid = payment_unresolved(job.attempts)
        failure = Failure(
            kind="interrupted",
            message="the engine stopped while this job was running",
            payment_sent=any(a.payment_sent for a in job.attempts),
        )
        await store.finish_job(job.id, "uncertain" if maybe_paid else "failed", failure, None, clock.now())
        recovered += 1
    if recovered:
        _LOG.warning("recovered interrupted jobs", extra={"event": "jobs.recovered", "count": recovered})
    return recovered


def failure_of(error: RunnerCallError, paid: bool, tried: int = 1) -> Failure:
    """A 404 or 409 is reported as ``refused``. ``status_code`` keeps the orchestrator's status.

    ``payment_unknown`` means the signer was asked and may have charged. It is not a retryable refusal.
    """
    not_run = error.kind == "http" and error.status_code in NOT_RUN_STATUSES
    kind: FailureKind
    if error.payment_unknown:
        kind = "payment_unknown"
    elif not_run:
        kind = "refused"
    else:
        kind = failure_kind(error.kind, error.status_code)
    return Failure(
        kind=kind,
        message=str(error) if tried == 1 else f"all {tried} runners failed; last: {error}",
        status_code=error.status_code,
        body=error.body[:BODY_LIMIT] if error.body else None,
        payment_sent=paid,
    )


class Candidates:
    """Selection order, at most ``max_attempts``. ``exclude`` skips that orchestrator's later runners.

    ``max_paid_failovers`` is the operator's cap on payments a job fails over from (D55): once that many
    paid attempts have failed over, no further attempt starts, because the next could be paid too.
    """

    def __init__(
        self,
        ordered: Sequence[Runner],
        max_attempts: int,
        max_paid_failovers: int | None = None,
        job_id: UUID | None = None,
    ) -> None:
        self.ordered = tuple(ordered)
        self._job_id = job_id
        self._max_attempts = max_attempts
        self._max_paid_failovers = max_paid_failovers
        self._excluded: set[str] = set()
        self.paid_failovers = 0

    @property
    def capped(self) -> bool:
        return self._max_paid_failovers is not None and self.paid_failovers >= self._max_paid_failovers

    def __iter__(self) -> Iterator[Runner]:
        tried = 0
        for runner in self.ordered:
            if tried >= self._max_attempts or self.capped:
                return
            if orchestrator_of(runner) in self._excluded:
                continue
            tried += 1
            yield runner

    def exclude(self, runner: Runner) -> None:
        self._excluded.add(orchestrator_of(runner))

    def paid_failover(self, runner: Runner) -> None:
        """A paid attempt failed over: never pay that orchestrator again, and count the payment."""
        self.exclude(runner)
        self.paid_failovers += 1
        if self.capped:
            _LOG.info(
                "paid attempt cap reached",
                extra={
                    "event": "jobs.paid_attempt_cap",
                    "job_id": str(self._job_id),
                    "paid": self.paid_failovers,
                },
            )


def orchestrator_of(runner: Runner) -> str:
    url = runner.orchestrator_url or runner.url
    try:
        parts = urlsplit(url if "://" in url else f"https://{url}")
        scheme, host, port = parts.scheme.lower(), (parts.hostname or "").lower(), parts.port
    except (ValueError, UnicodeError):  # a URL that does not parse is its own orchestrator
        return url.strip().lower()
    if port is None:
        port = 443 if scheme == "https" else 80
    if scheme not in ("http", "https") or not host or not 1 <= port <= 65535:
        return url.strip().lower()
    return f"{scheme}://{host}:{port}"
