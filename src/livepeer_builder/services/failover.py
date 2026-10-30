"""What a failed attempt means for its job, and which runner is tried next (D52)."""

import logging
from collections.abc import Iterator, Sequence
from typing import Literal, cast
from urllib.parse import urlsplit
from uuid import UUID

from livepeer_builder.contracts import Attempt, AttemptOutcome, Failure, FailureKind, Runner, reason_fits
from livepeer_builder.errors import RunnerCallError
from livepeer_builder.protocols import Clock, JobRepo, RunnerStream
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
    if last.outcome == "in_flight" and not last.payment_sent and refused_everything(last):
        return False  # every manifest it stored before signing was refused: nothing was paid (D54)
    if last.outcome in ("in_flight", "payment_unknown"):  # under way, or the signer may have charged
        return True
    return last.payment_sent and attempt_verdict(last.outcome, last.status_code, True) != "failover"


def refused_everything(attempt: Attempt) -> bool:
    """The attempt asked the signer at least once, and every manifest was refused. An attempt with no
    stored manifest may predate D54, so it does not count."""
    return bool(attempt.sessions) and all(session.payment_state == "unpaid" for session in attempt.sessions)


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


def failure_kind(outcome: AttemptOutcome, status_code: int | None) -> FailureKind:
    """Attempt outcome → failure kind, for a call that failed or a stream that ended early."""
    if outcome == "http":
        status = status_code or 0
        return "runner_rejected" if 400 <= status < 500 else "runner_error"
    if outcome == "payment_unknown":
        return "payment_unknown"
    if outcome == "payment_refused":
        return "payment"
    if outcome in ("in_flight", "succeeded"):
        return "runner_error"
    return cast(FailureKind, outcome)


def returned_failover(
    opened: RunnerStream, runner: Runner, candidates: "Candidates"
) -> RunnerCallError | None:
    """An HTTP error the caller asked to receive (``return_http_errors``, #55) still fails over when
    another runner could do the work (D52, D55), as the runner call would have raised it. ``None``:
    the caller gets this answer, unchanged."""
    status, paid = opened.status_code, opened.payment_sent
    if status < 300:
        return None
    error = RunnerCallError(
        "refused" if status == 503 else "http",
        f"HTTP {status} from {runner.url}",
        payment_sent=paid,
        manifest_ids=opened.manifest_ids,
        status_code=status,
    )
    if attempt_verdict(error.kind, status, paid) != "failover" or not candidates.more_after(runner, paid):
        return None
    return error


def failure_of(error: RunnerCallError, paid: bool, tried: int = 1) -> Failure:
    """A 404 or 409 is reported as ``refused``. ``status_code`` keeps the orchestrator's status.

    ``payment_unknown`` means the signer was asked and may have charged. It is not a retryable refusal,
    and its reason is ``signer_unknown`` (D58).
    """
    not_run = error.kind == "http" and error.status_code in NOT_RUN_STATUSES
    kind: FailureKind
    if error.payment_unknown:
        kind = "payment_unknown"
    elif not_run:
        kind = "refused"
    else:
        kind = failure_kind(error.kind, error.status_code)
    reason = "signer_unknown" if error.payment_unknown else error.reason
    if reason is not None and not reason_fits(kind, reason):
        reason = None  # a transport's reason that does not fit what the engine made of its error
    return Failure(
        kind=kind,
        message=str(error) if tried == 1 else f"all {tried} runners failed; last: {error}",
        status_code=error.status_code,
        body=error.body[:BODY_LIMIT] if error.body else None,
        payment_sent=paid,
        reason=reason,
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
        self._tried = 0
        self._at = -1  # the index of the runner yielded last

    @property
    def capped(self) -> bool:
        return self._max_paid_failovers is not None and self.paid_failovers >= self._max_paid_failovers

    def __iter__(self) -> Iterator[Runner]:
        self._tried, self._at = 0, -1
        for index, runner in enumerate(self.ordered):
            if self._tried >= self._max_attempts or self.capped:
                return
            if orchestrator_of(runner) in self._excluded:
                continue
            self._tried += 1
            self._at = index
            yield runner

    def more_after(self, runner: Runner, paid: bool) -> bool:
        """Whether another runner would be tried if ``runner``, the current one, failed over now."""
        excluded = self._excluded | ({orchestrator_of(runner)} if paid else set())
        cap = self._max_paid_failovers
        if self._tried >= self._max_attempts or (paid and cap is not None and self.paid_failovers + 1 >= cap):
            return False
        return any(orchestrator_of(later) not in excluded for later in self.ordered[self._at + 1 :])

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
