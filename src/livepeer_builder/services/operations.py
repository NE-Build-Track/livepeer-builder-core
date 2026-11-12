"""What a repeated ``operation_ref`` gets (D57, docs/contracts/engine.md#idempotency).

``retry_of`` and ``never_reached_a_runner`` are the rules, pure functions with no I/O. ``check_operation``
and ``claim_operation`` apply them to the ``JobRepo`` before a job is admitted and when it is written.
"""

from uuid import UUID

from livepeer_builder.contracts import ActorContext, Attempt, FailureKind, Job
from livepeer_builder.errors import OperationConflict, OperationExists, OperationInProgress
from livepeer_builder.protocols import JobRepo
from livepeer_builder.services.failover import NOT_RUN_STATUSES

CLAIMS = 3  # tries to take a reference other requests keep taking first

# The job failed before it chose a runner: nothing was dispatched.
BEFORE_DISPATCH: frozenset[FailureKind] = frozenset({"no_offering", "no_capacity", "not_provisioned"})


def retry_of(holder: Job, *, same_request: bool) -> UUID:
    """The answer to a repeat of the reference ``holder`` holds (D57, engine.md#idempotency).

    Returns the id of the failed job a new job supersedes, when the repeat may dispatch again. Otherwise
    raises: ``OperationConflict`` for a different request, ``OperationInProgress`` while the holder
    runs, and ``OperationExists`` for a job that ran (succeeded, uncertain, or failed after a runner
    accepted the work): its output was not retained, so it cannot be replayed (Q1).
    """
    if not same_request:
        raise OperationConflict(holder)
    if holder.state in ("queued", "running"):
        raise OperationInProgress(holder)
    if never_reached_a_runner(holder):
        return holder.id
    raise OperationExists(holder)


def never_reached_a_runner(job: Job) -> bool:
    """A failed job that provably never had a runner accept the work, so a repeat may dispatch it again.

    A closed list (D57): the job failed before dispatch or ended before its first attempt (whatever its
    failure), or every attempt was refused before the app ran (``refused``, or a 404 or 409 from the
    orchestrator) or by the signer (``payment``, ``payment_refused``), and nothing was paid.
    ``unreachable`` and an unpaid ``timeout`` are not on it: a disconnect after the request went out is
    ``unreachable``, and a free runner may have run the work (Q2).
    """
    if job.state != "failed" or job.failure is None:
        return False
    if job.failure.payment_sent or any(a.payment_sent for a in job.attempts):
        return False
    if job.failure.kind in BEFORE_DISPATCH or not job.attempts:
        return True  # no attempt: it ended before its first one, which is written before any runner call
    return all(_refused_before_the_app_ran(a) for a in job.attempts)


def _refused_before_the_app_ran(attempt: Attempt) -> bool:
    if attempt.outcome in ("refused", "payment", "payment_refused"):
        return True
    return attempt.outcome == "http" and attempt.status_code in NOT_RUN_STATUSES


async def check_operation(
    store: JobRepo, actor: ActorContext, operation_ref: str, fingerprint: str
) -> UUID | None:
    """Before admission: the failed job a new one supersedes, or None for a reference nobody holds.
    Raises as ``retry_of`` does. A fingerprint stored before fingerprints existed matches any request."""
    found = await store.job_by_operation_ref(actor.application_id, actor.actor_id, operation_ref)
    if found is None:
        return None
    existing, existing_fingerprint = found
    return retry_of(existing, same_request=existing_fingerprint in (None, fingerprint))


async def claim_operation(store: JobRepo, job: Job, fingerprint: str, retrying: UUID | None) -> None:
    """Write the job before any attempt. A reference another request took since the check is answered
    from the job holding it now, as a repeat; a holder that never reached a runner is superseded."""
    for _ in range(CLAIMS):
        try:
            if retrying is None:
                await store.create_job(job, fingerprint)
            else:
                await store.create_job(job, fingerprint, retry_of=retrying)
            return
        except OperationExists as taken:  # the store compared fingerprints (OperationConflict)
            if taken.job is None:  # a JobRepo raises with the job; anything else is not ours to answer
                raise
            holder = taken.job
            retrying = retry_of(holder, same_request=True)
    raise OperationInProgress(holder)  # other requests keep re-dispatching it
