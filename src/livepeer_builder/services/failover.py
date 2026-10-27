"""Failover: what a failed attempt means for its job, and which runner is tried next (D52).

A failed attempt either moves the job to the next runner (``failover``), or ends it ``failed`` or
``uncertain``. Payment alone does not bind a job to a runner: an attempt that was paid and refused
before the work ran fails over to a runner on another orchestrator. Its charge stays on the allocation
as ``failed_after_payment`` and is never billable (services/costs.py). What stops a paid job is a
failure that may have run the work, or a payment whose outcome is unknown (``uncertain``), or one that
would repeat, and be charged, everywhere (``failed``).
"""

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
# 404 and 409 come from the orchestrator before the app ran: it lost the runner or its session (a
# runner stop, livepeer-python-gateway#83), or the session already exists. Another runner may succeed.
NOT_RUN_STATUSES = frozenset({404, 409})
# 502 and 504: the orchestrator's proxy lost the runner, before or during its answer, so the work may
# have run. Any other status is the runner's (a 500 after it ran) or about the request (other 4xx).
MAY_HAVE_RUN_STATUSES = frozenset({502, 504})


def attempt_verdict(
    outcome: AttemptOutcome, status_code: int | None, paid: bool, *, payment_unknown: bool = False
) -> Verdict:
    """What a failed attempt means for its job (docs/contracts/engine.md#behavior-the-contract-fixes).

    ``payment_unknown``: the signer was asked and never gave a validated answer, so it may have charged.
    The paid request never left, but a second payment could double the charge: the job stops there.
    """
    not_run = outcome == "http" and status_code in NOT_RUN_STATUSES
    if payment_unknown or outcome == "payment_unknown":
        return "uncertain"
    if not paid:
        return "failover" if outcome in FAILOVER_OUTCOMES or not_run else "failed"
    if outcome in UNCERTAIN_OUTCOMES or (outcome == "http" and status_code in MAY_HAVE_RUN_STATUSES):
        return "uncertain"
    return "failover" if outcome in _PAID_FAILOVER_OUTCOMES or not_run else "failed"


def payment_unresolved(attempts: Sequence[Attempt]) -> bool:
    """A job left unfinished (a restart, an engine error) may hold a payment for work whose outcome is
    unknown when its last attempt is still in flight, or was paid and did not end in a failover. The
    attempts it failed over from ended with a known outcome, so they never make it ``uncertain``."""
    if not attempts:
        return False
    last = max(attempts, key=lambda attempt: attempt.number)
    if last.outcome in ("in_flight", "payment_unknown"):  # under way, or the signer may have charged
        return True
    return last.payment_sent and attempt_verdict(last.outcome, last.status_code, True) != "failover"


def failure_of(error: RunnerCallError, paid: bool, tried: int = 1) -> Failure:
    """The failure a job ends with, in the last runner's words. ``tried`` counts the runners it failed on.

    A 404 or 409 from the orchestrator is a refusal before the work ran, so it is ``refused`` (retry
    later), not ``runner_rejected``: the REST service would otherwise answer the caller 404 or 409.
    ``status_code`` keeps the orchestrator's own status.
    """
    not_run = error.kind == "http" and error.status_code in NOT_RUN_STATUSES
    return Failure(
        kind="refused" if not_run else failure_kind(error.kind, error.status_code),
        message=str(error) if tried == 1 else f"all {tried} runners failed; last: {error}",
        status_code=error.status_code,
        body=error.body[:BODY_LIMIT] if error.body else None,
        payment_sent=paid,
    )


class Candidates:
    """The runners a job may try, in selection order, at most ``max_attempts`` of them.

    ``exclude`` drops every later runner on an orchestrator, so one that took a payment and failed is
    never paid again for the same job. A runner is matched to its orchestrator by ``orchestrator_url``,
    or by its own URL's origin when that is missing: a Live Runner is reached through its
    orchestrator. A skipped runner does not count as an attempt.
    """

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
    """``scheme://host:port`` of the runner's orchestrator, lower-cased, with the default port filled in."""
    url = runner.orchestrator_url or runner.url
    parts = urlsplit(url if "://" in url else f"https://{url}")
    scheme = parts.scheme.lower()
    port = parts.port or (443 if scheme == "https" else 80)
    return f"{scheme}://{(parts.hostname or '').lower()}:{port}"
