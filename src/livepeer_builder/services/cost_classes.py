"""The cost class of each of a job's attempts: a pure function of the job and its usage rows
(docs/contracts/payments.md#cost-classification). ``services/costs.py`` reads, syncs and reports."""

from collections.abc import Iterable, Sequence
from decimal import Decimal

from livepeer_builder.contracts import (
    Attempt,
    AttemptCost,
    CostClass,
    CostStatus,
    Fees,
    Job,
    JobCost,
    SignedSession,
    UsageRow,
    add_fees,
    billable_fees,
)

ENDED: frozenset[str] = frozenset({"succeeded", "failed", "uncertain"})  # store.matching.ENDED


def classify(job: Job, rows: Sequence[UsageRow]) -> JobCost:
    """Pure classification of one job's charges. Only ``applied`` rows count as cost.

    A job that has not ended may still pay, so it is ``pending`` and so is each of its attempts, with the
    fees matched so far: never ``none``, never a final class. A signed session whose usage has not arrived
    stays ``pending`` too, including the attempt that left the job uncertain.
    """
    applied = [row for row in rows if row.status == "applied"]
    running = job.state not in ENDED
    costs = (_attempt_cost(job, attempt, applied, running) for attempt in job.attempts)
    attempts = [cost for cost in costs if cost is not None]
    return JobCost(
        job_id=job.id,
        status=_status(running, attempts),
        fees=add_fees(attempt.fees for attempt in attempts),
        attempts=tuple(attempts),
        first_observed_at=None,
        updated_at=None,
        billable=billable_fees(attempts),
    )


def _attempt_cost(
    job: Job, attempt: Attempt, applied: Sequence[UsageRow], running: bool
) -> AttemptCost | None:
    """One attempt's class and the fees matched to it. ``None`` for an attempt that signed nothing."""
    # A manifest the signer refused is no charge (D54); a stored ``unknown`` one may be, so it waits.
    sessions = tuple(session for session in attempt.sessions if session.payment_state != "unpaid")
    if not sessions and not attempt.payment_sent:
        return None
    matched_ids = {
        session.payment_session_id
        for session in sessions
        if session.match == "matched" and session.payment_session_id
    }
    mine = [row for row in applied if row.payment_session_id in matched_ids]
    cost_class: CostClass = "pending" if running else _ended_class(job, attempt, sessions, mine)
    return AttemptCost(attempt_number=attempt.number, cost_class=cost_class, fees=fees_of(mine))


def _ended_class(
    job: Job, attempt: Attempt, sessions: Sequence[SignedSession], mine: Sequence[UsageRow]
) -> CostClass:
    """The class of an attempt of a job that has ended, from its sessions and the rows matched to it."""
    arrived = {row.payment_session_id for row in mine}
    # Unmatched, or matched with no applied row yet: the charge is not in what we can see.
    # Ambiguous is not waiting: the usage arrived and the join was not unique.
    waiting = any(
        session.match == "unmatched"
        or (session.match == "matched" and session.payment_session_id not in arrived)
        for session in sessions
    )
    if waiting:
        return "pending"
    # Pending wins while a sibling is still waiting. Once usage has been seen, an ambiguous
    # signed session makes the attempt uncertain. Its rows stay unattributed; a sibling's
    # matched fee stays on the attempt.
    if any(session.match == "ambiguous" for session in sessions):
        return "uncertain"
    # A runner's answer the engine could not keep is paid for and not delivered: the attempt succeeded,
    # the job did not, and the cost follows the job (D69 (4)).
    reply_lost = job.failure is not None and job.failure.kind == "result_not_stored"
    if attempt.outcome == "succeeded" and not reply_lost:
        return "delivered"
    last = max((each.number for each in job.attempts), default=0)
    last_word = attempt.outcome in ("in_flight", "payment_unknown") or (
        job.state == "uncertain" and attempt.number == last
    )
    # only the last attempt can leave a job uncertain (D52)
    return "uncertain" if last_word else "failed_after_payment"


def _status(running: bool, attempts: Sequence[AttemptCost]) -> CostStatus:
    if running:
        return "pending"
    if not attempts:
        return "none"
    return "pending" if any(attempt.cost_class == "pending" for attempt in attempts) else "observed"


def fees_of(rows: Iterable[UsageRow]) -> Fees:
    applied = [row for row in rows if row.status == "applied"]
    eth = sum((row.computed_fee_eth or Decimal(0) for row in applied), Decimal(0))
    # No applied rows means nothing was charged: a known zero. None means some charge lacks a USD price.
    usd: Decimal | None = None
    if all(row.computed_fee_usd is not None for row in applied):
        usd = sum((row.computed_fee_usd or Decimal(0) for row in applied), Decimal(0))
    return Fees(fee_eth=eth, fee_usd=usd, row_count=len(applied))
