"""A job's measured usage is stored once (#115, C20).

``report_usage`` used to check "already reported" on the job it had read and then update the job
unconditionally. Two reports that both read the job before either wrote both succeeded, and the second
write replaced the usage the first caller was told was stored.

The store now writes usage only where none is stored and the job is ``succeeded``, in one statement
(``UsageOnceRepo.set_usage_once``). A report that finds usage already stored, when it reads the job or
when its write is refused, is answered with the stored usage if its quantities are the same (a retry
after a lost answer, or the slower of two identical reports), and refused as before if they differ.

The conditional write is optional for a store. With a supplied store without it, the engine checks the
job it read and calls ``set_usage`` as before: two reports at the same moment can still both succeed.
"""

from collections.abc import Mapping
from datetime import datetime

from livepeer_builder.contracts import Job, MeasuredUsage
from livepeer_builder.errors import InvalidRequest, NotFound
from livepeer_builder.protocols import JobRepo, UsageOnceRepo


async def report_once(store: JobRepo, job: Job, quantities: Mapping[str, int], now: datetime) -> Job:
    """Store the owner's report on ``job`` (as the caller read it), or answer a repeat of it."""
    if job.usage is not None:
        return _repeated(job, quantities)
    if job.state != "succeeded":
        raise InvalidRequest("usage can only be reported for a succeeded job")
    if not quantities or any(value < 0 for value in quantities.values()):
        raise InvalidRequest("usage needs at least one non-negative quantity")
    usage = MeasuredUsage(quantities=dict(quantities), source="app_reported", reported_at=now)
    if not isinstance(store, UsageOnceRepo):
        await store.set_usage(job.id, usage)  # unconditional: this store has no conditional write
    elif not await store.set_usage_once(job.id, usage):
        return _repeated(await _stored(store, job), quantities)  # another report was stored first
    return job.model_copy(update={"usage": usage})


def _repeated(job: Job, quantities: Mapping[str, int]) -> Job:
    """The answer to a report on a job that already has usage: the stored usage for the same quantities,
    a refusal for any other."""
    if job.usage is not None and job.usage.quantities == dict(quantities):
        return job
    raise InvalidRequest("usage was already reported for this job")


async def _stored(store: JobRepo, job: Job) -> Job:
    current = await store.get_job(job.id)
    if current is None:  # purged since this report read it
        raise NotFound(f"job {job.id} not found")
    return current
