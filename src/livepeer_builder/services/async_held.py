"""A job the async worker holds, and the end the worker owes it (D69, ISS-26).

``Held`` is the one owner of a claimed job: its slot in the worker's lane (a worker, an admission slot
and the job's id, taken together before the claim), the number of its claim, its deadline and its
attempts. ``Ends`` writes the end of a held job that its dispatch did not write: back to the queue when
nothing went out for it, otherwise ended as a stopped job is. The slot is given back when that write
has been made, and only then. A write the store refused is kept (the job's record, its attempts and
how it ends: never its request or a runner's reply) and made again by the worker's next step, so at
most ``LPB_ASYNC_WORKERS`` of them are kept and no claimed job stays ``running`` once the store answers.
``Waiting`` is the worker's memory of the jobs it is not to claim again yet.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from uuid import UUID

from livepeer_builder.contracts import ActorContext, Attempt, Job, JobRequest
from livepeer_builder.protocols import JobRepo
from livepeer_builder.services.admission import AsyncSlot
from livepeer_builder.services.dispatch import ClaimLost, Dispatcher
from livepeer_builder.services.failover import MAX_CLAIMS, error_end, interrupted_end
from livepeer_builder.utils import spawn_shielded

_LOG = logging.getLogger(__name__)

MAX_WAIT_POLLS = 32  # a job that keeps being set aside waits 1, 2, 4... poll intervals, at most this many


@dataclass
class Held:
    """A job this worker holds, from before its claim until its end is written."""

    job_id: UUID
    slot: AsyncSlot  # given back once, when the job's end is written or the job turns out not to be ours
    job: Job | None = None  # None until the claim's answer is back
    request: JobRequest | None = None
    actor: ActorContext | None = None
    claim: int | None = None  # the claim's number, which every later write for the job carries
    deadline: float = 0.0  # the loop time at which timeout_s runs out, counted from the claim
    attempts: list[Attempt] = field(default_factory=list)

    def claimed(self) -> tuple[Job, JobRequest, ActorContext]:
        """What the claim answered with. Only a job whose claim was answered is started."""
        if self.job is None or self.request is None or self.actor is None:
            raise RuntimeError("the job's claim was never answered")
        return self.job, self.request, self.actor


@dataclass(frozen=True)
class End:
    """How a held job ends when its dispatch did not end it."""

    cause: str  # the name of what stopped it, for the failure's message
    back: bool = False  # it goes back to the queue if nothing went out for it
    counted: bool = True  # going back uses up one of its claims (not for a job that only has to wait)
    # Counted or not, the claim's number is spent: the store never issues it again.
    reason: str = ""  # why it has to wait, for the log


STOPPED = End("CancelledError", back=True)  # the engine's stop cancelled the job


def log_lost(held: Held) -> None:
    _LOG.warning(
        "the job no longer runs under this worker's claim; nothing more is written for it",
        extra={"event": "jobs.async_claim_lost", "job_id": str(held.job_id), "claim": held.claim},
    )


class Ends:
    """Writes the ends the worker owes, keeps the ones the store refused, and knows which writes are
    still under way."""

    def __init__(self, store: JobRepo, dispatcher: Dispatcher) -> None:
        self._store = store
        self._dispatcher = dispatcher
        self._owed: dict[UUID, tuple[Held, End]] = {}  # at most one per worker: each keeps its slot
        self._writes: set[asyncio.Task[None]] = set()

    @property
    def owed(self) -> int:
        """How many ends the store refused and a later step still has to write."""
        return len(self._owed)

    async def shielded(self, held: Held, end: End | None) -> None:
        """``settle`` in a task of its own, which the caller's cancellation does not stop and
        ``written`` waits for: a job's task may be cancelled while its end is being written."""
        task = spawn_shielded(self.settle(held, end))
        self._writes.add(task)
        task.add_done_callback(self._writes.discard)
        await asyncio.shield(task)

    async def written(self) -> None:
        """Return once no write of an end is under way."""
        while self._writes:
            await asyncio.wait(list(self._writes))

    async def settle_owed(self) -> None:
        """Write again the ends the store refused. One it still refuses stays owed."""
        for held, end in list(self._owed.values()):
            await self.settle(held, end)

    async def settle(self, held: Held, end: End | None) -> None:
        """Write the end ``held`` is owed (``None``: its dispatch wrote it) and give its slot back. A
        write that fails is logged and kept, with the slot: nothing is raised."""
        if end is not None and not await self._written(held, end):
            return
        self._owed.pop(held.job_id, None)
        held.slot.release()

    async def _written(self, held: Held, end: End) -> bool:
        """Write ``end``. ``False`` when the store refused it: it stays owed."""
        try:
            await self._write(held, end)
        except ClaimLost:
            log_lost(held)
        except Exception:
            held.request = held.actor = None  # what the job ran with is not needed to end it: let it go
            self._owed[held.job_id] = (held, end)
            _LOG.exception(
                "could not write the end of an async job; a later step writes it",
                extra={"event": "jobs.async_end_failed", "job_id": str(held.job_id)},
            )
            return False
        return True

    async def _write(self, held: Held, end: End) -> None:
        """Back to the queue when nothing went out for the job and it may go back; otherwise its end.

        A claim whose answer never came is looked up first. The job's id was taken at the gate before
        the claim, so nobody else in this process claimed it: if it is ``running``, that claim was
        written and is this worker's to resolve, and if it is not, there is nothing to do."""
        job = held.job
        if job is None:
            job = await self._store.get_job(held.job_id)
            if job is None or job.state != "running":
                return
        if end.back and not held.attempts:
            if await self._back(job, held, end):
                return
            state, failure = interrupted_end(())  # as a restart ends it once its claims are used up
        else:
            state, failure = error_end(held.attempts, end.cause)
        await self._dispatcher.close(job, held.attempts, state, failure, claim=held.claim)

    async def _back(self, job: Job, held: Held, end: End) -> bool:
        """Put the job back in the queue. ``False`` when ``MAX_CLAIMS`` of its claims have counted."""
        if await self._store.requeue_unstarted(job.id, MAX_CLAIMS, claim=held.claim, counted=end.counted):
            _LOG.info(
                "async job put back in the queue",
                extra={
                    "event": "jobs.async_requeued",
                    "job_id": str(job.id),
                    "reason": end.reason or end.cause,
                },
            )
            return True
        if not end.counted:  # only its claim was in the way of giving it back
            raise ClaimLost(job.id, held.claim)
        return False


class Waiting:
    """The jobs the worker does not claim again yet: one whose claim raised, and one that was claimed
    and could not be dispatched for a reason that passes. Each waits ``poll_s``, and twice as long each
    time it is set aside again, up to ``MAX_WAIT_POLLS`` intervals, so a job that cannot run costs a
    claim now and then and never a loop. What it knows is forgotten once it is stale: it never grows
    past the jobs set aside within that time."""

    def __init__(self) -> None:
        self._until: dict[UUID, tuple[float, int]] = {}  # job -> (loop time it is due, times set aside)
        self._stale_s = 0.0

    def put(self, job_id: UUID, poll_s: float) -> None:
        times = self._until.get(job_id, (0.0, 0))[1]
        wait = poll_s * min(2**times, MAX_WAIT_POLLS)
        self._stale_s = poll_s * MAX_WAIT_POLLS
        self._until[job_id] = (asyncio.get_running_loop().time() + wait, times + 1)

    def due(self, job_id: UUID) -> bool:
        """Whether the job may be claimed now."""
        waits = self._until.get(job_id)
        return waits is None or waits[0] <= asyncio.get_running_loop().time()

    def forget(self, job_id: UUID) -> None:
        self._until.pop(job_id, None)

    def set_aside(self) -> int:
        """How many jobs are set aside and not due: the rows of a page of the queue they may take up.
        What has been due for long is forgotten here."""
        now = asyncio.get_running_loop().time()
        for job_id in [job_id for job_id, (due, _) in self._until.items() if due + self._stale_s < now]:
            del self._until[job_id]
        return sum(1 for due, _ in self._until.values() if due > now)
