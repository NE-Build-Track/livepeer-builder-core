"""What a failed attempt means for its job, and which runner is tried next (D52)."""

from collections.abc import Iterator, Sequence
from typing import Literal
from urllib.parse import urlsplit

from livepeer_builder.contracts import Attempt, AttemptOutcome, Failure, Runner
from livepeer_builder.errors import RunnerCallError
from livepeer_builder.services.streams import failure_kind
from livepeer_builder.utils import BODY_LIMIT

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


def failure_of(error: RunnerCallError, paid: bool, tried: int = 1) -> Failure:
    """A 404 or 409 is reported as ``refused``. ``status_code`` keeps the orchestrator's status."""
    not_run = error.kind == "http" and error.status_code in NOT_RUN_STATUSES
    return Failure(
        kind="refused" if not_run else failure_kind(error.kind, error.status_code),
        message=str(error) if tried == 1 else f"all {tried} runners failed; last: {error}",
        status_code=error.status_code,
        body=error.body[:BODY_LIMIT] if error.body else None,
        payment_sent=paid,
    )


class Candidates:
    """Selection order, at most ``max_attempts``. ``exclude`` skips that orchestrator's later runners."""

    def __init__(self, ordered: Sequence[Runner], max_attempts: int) -> None:
        self.ordered = tuple(ordered)
        self._max_attempts = max_attempts
        self._excluded: set[str] = set()

    def __iter__(self) -> Iterator[Runner]:
        tried = 0
        for runner in self.ordered:
            if tried >= self._max_attempts:
                return
            if orchestrator_of(runner) in self._excluded:
                continue
            tried += 1
            yield runner

    def exclude(self, runner: Runner) -> None:
        self._excluded.add(orchestrator_of(runner))


def orchestrator_of(runner: Runner) -> str:
    url = runner.orchestrator_url or runner.url
    try:
        parts = urlsplit(url if "://" in url else f"https://{url}")
        scheme, host, port = parts.scheme.lower(), (parts.hostname or "").lower(), parts.port
    except (ValueError, UnicodeError):  # a URL that does not parse is its own orchestrator
        return url.strip().lower()
    return f"{scheme}://{host}:{port or (443 if scheme == 'https' else 80)}"
