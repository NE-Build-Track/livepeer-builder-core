"""The worker that runs queued async jobs (D69, ISS-26).

``lpb_jobs`` is the queue. ``JobService.submit`` stores a job ``queued``; this worker claims it and runs
the same paid call an immediate job runs (``services/dispatch.py``), in the engine's own process. Rules:

- **A slot before a claim.** The worker takes an admission slot (``AdmissionLimiter``, shared with
  immediate jobs and streams), then claims the job with a compare-and-set. A claim that finds the job
  gone (a cancel, the queue's time limit) gives the slot back. So no job runs outside the admission
  limits, and at most ``workers`` of them run at once.
- **Oldest first**, leaving out an app that is at its per-app limit, so one busy app does not hold up
  the others.
- **Payment happens at dispatch, never while a job waits.** The actor's allocation is looked up when the
  job is claimed: one revoked or exhausted in the meantime fails the job unpaid.
- **A job waits at most ``queue_ttl_s``.** One still queued after that ends ``failed``,
  ``no_capacity``, with nothing paid. Its ``timeout_s`` counts from the claim.
- **Nothing is run twice.** A job that was claimed is never dispatched again by the engine, with one
  exception: one that was stopped before its first attempt was written goes back to the queue, at most
  ``MAX_CLAIMS`` claims in all (``services/failover.py`` at start, ``_stopped`` here at stop).

``run_once()`` is one step, for tests and for anybody who drives the queue by hand; ``run(stop)`` is
the loop the engine starts, woken at each submit and every ``poll_s``; ``drain()`` is the engine's stop.
"""

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from livepeer_builder.contracts import ActorContext, Attempt, Failure, Job, JobRequest, ResultRef, RunnerReply
from livepeer_builder.contracts.http import response_content_type
from livepeer_builder.errors import JobFailed, NoCapacity
from livepeer_builder.protocols import Clock, JobRepo
from livepeer_builder.services.admission import AdmissionLimiter
from livepeer_builder.services.dispatch import Dispatcher
from livepeer_builder.services.failover import MAX_CLAIMS
from livepeer_builder.utils import run_shielded

_LOG = logging.getLogger(__name__)

RESULT_LOCATION = "/v1/jobs/{job_id}/result"  # where REST serves a stored result
_MIB = 1024 * 1024


@dataclass(frozen=True)
class AsyncLimits:
    """The tuning of async jobs, from ``EngineSettings`` (the ``LPB_ASYNC_*`` settings; defaults D69 (6))."""

    workers: int = 4  # async jobs dispatched at once, inside the admission limits
    max_queued: int = 1000  # a submit beyond this is refused, NoCapacity
    queue_ttl_s: float = 3600.0  # the longest a job waits for a worker
    result_max_bytes: int = 16 * _MIB  # a larger reply is not stored
    results_days: int | None = 7  # how long a stored result is kept; None: as long as its job
    poll_s: float = 1.0  # how often the loop looks at the queue when nothing wakes it
    drain_timeout_s: float = 30.0  # at stop, how long running jobs may take to end


@dataclass(frozen=True)
class Claimed:
    """A job this worker claimed, what it runs with, and the admission slot it holds until it ends."""

    job: Job
    request: JobRequest
    actor: ActorContext
    slot: contextlib.AsyncExitStack


class AsyncJobWorker:
    def __init__(
        self,
        store: JobRepo,
        dispatcher: Dispatcher,
        admission: AdmissionLimiter,
        clock: Clock,
        limits: AsyncLimits | None = None,
    ) -> None:
        self._store = store
        self._dispatcher = dispatcher
        self._admission = admission
        self._clock = clock
        self.limits = limits or AsyncLimits()
        self._running: set[asyncio.Task[None]] = set()
        self._claimed: set[UUID] = set()  # the jobs this worker claimed and has not ended
        self._wake = asyncio.Event()
        self._draining = False
        self._loop: asyncio.Task[object] | None = None  # the task running ``run``, for ``drain``
        self._queued: tuple[int, datetime | None] = (0, None)  # as last counted, for health
        self._news = 0  # raised by every count's start and every job queued: a count is kept if none is newer

    # -- what health reports ------------------------------------------------------------------------

    @property
    def queued_jobs(self) -> int:
        return self._queued[0]

    @property
    def oldest_queued_s(self) -> float | None:
        """How long the oldest queued job has waited: the number to alert on, not the queue's depth."""
        oldest = self._queued[1]
        return None if oldest is None else max((self._clock.now() - oldest).total_seconds(), 0.0)

    async def count_queue(self) -> int:
        """Count the queue again, after anything that changed it, and return what was read. Health is
        read without a database call.

        The read takes time, and its answer is kept only if nothing newer is known when it arrives: a job
        queued meanwhile may not be in it, and a count that began later read later. So a count never
        takes back a job ``queued`` has noted (#184 review). What stays may then say too much for a
        moment, never too little: the loop is awake, and its step counts again."""
        self._news += 1
        mine = self._news
        stats = await self._store.queued_stats()
        if mine == self._news:
            self._queued = stats
        return stats[0]

    def queued(self, job: Job) -> None:
        """``job`` was just queued: note it, and have the loop look now, not at its next poll. No
        database call, so it cannot fail after the job was stored."""
        count, oldest = self._queued
        self._queued = (count + 1, oldest or job.created_at)
        self._news += 1
        self._wake.set()

    # -- the loop, one step of it, and its end --------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Claim and start queued jobs until the engine stops. A step that fails is logged and the next
        poll tries again: the queue is in the database, so nothing is lost.

        The loop reads the database only while jobs wait, for as long as the last count was not zero,
        and once each time it is woken: by a submit, or by a job's end. ``BuilderEngine.start()`` counts
        the queue before it starts the loop (a restart may have left jobs in it), and a submit raises the
        count in this process, which is the only one that writes the queue (one engine process per
        database). So an engine nobody submits to adds no query, not even beside the caller's own first
        writes at start, and a waiting job is looked at every ``poll_s`` and whenever a job is queued or a
        worker comes free. The wake counts by itself, whatever the count says: the count is a memory, the
        queue is in the database, and a job is never left waiting on the memory alone (#184 review)."""
        self._loop, retry = asyncio.current_task(), False
        while not stop.is_set() and not self._draining:
            woken = self._wake.is_set()
            self._wake.clear()
            if retry or woken or self._queued[0]:
                retry = await self._step_logged()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.limits.poll_s)

    async def _step_logged(self) -> bool:
        """One step of the loop. ``True`` when it failed, so the next turn tries it again."""
        try:
            await self._start_ready()
        except Exception:
            _LOG.exception("async job step failed", extra={"event": "jobs.async_step_failed"})
            return True
        return False

    async def run_once(self) -> int:
        """One step: end the jobs that waited too long, claim what there is room for, and wait for the
        jobs this step started. Returns how many it started."""
        started = await self._start_ready()
        if started:
            await asyncio.wait(started)
        return len(started)

    async def drain(self) -> None:
        """Engine stop: claim nothing more, let running jobs end for up to ``drain_timeout_s``, then cancel
        the rest. A cancelled job ends as a cancelled immediate one does: ``uncertain`` if it may have
        paid. Returns once every job's end is written, so the store is never closed under one."""
        self._draining = True
        self._wake.set()
        loop, self._loop = self._loop, None
        if loop is not None and not loop.done():  # let the step it is in finish: its claims then run
            await asyncio.wait([loop], timeout=self.limits.drain_timeout_s)
            loop.cancel()
            await asyncio.wait([loop])
        running = list(self._running)
        if not running:
            return
        _, late = await asyncio.wait(running, timeout=self.limits.drain_timeout_s)
        for task in late:
            task.cancel()
        if late:
            await asyncio.wait(late)
        _LOG.info("drained async jobs", extra={"event": "jobs.async_drained", "cancelled": len(late)})

    async def _start_ready(self) -> list[asyncio.Task[None]]:
        """Claim what there is room for, then start it. Every read and write of a step is done before
        any of its jobs starts, so a job's own writes never run beside the step's."""
        self._admission.check_runs_jobs()  # an engine in its clearinghouse role runs no job
        await self._expire()
        claimed: list[Claimed] = []
        try:
            while not self._draining and (room := self._room(len(claimed))) > 0:
                ready = await self._store.queued_jobs(room, self._admission.full_apps())
                before = len(claimed)
                for job_id, app in ready:
                    # Noted as soon as it is made: a claim is committed and holds a slot, so one made
                    # before a later claim failed must still be started below (#184 review).
                    if claim := await self._claim(job_id, app):
                        claimed.append(claim)
                if len(claimed) == before:  # nothing queued, or nothing that could be admitted
                    break
            await self.count_queue()  # before any job starts: nothing else is writing yet
        finally:  # a claimed job runs, whatever stopped the step after its claim
            started = [self._start(claim) for claim in claimed]
        return started

    def _room(self, claimed: int) -> int:
        """How many more jobs may be claimed now: free workers, while the engine has a free slot."""
        free = self.limits.workers - len(self._running) - claimed
        return free if self._admission.has_room else 0

    async def _expire(self) -> None:
        now = self._clock.now()
        failure = Failure(
            kind="no_capacity",
            message=f"no worker was free within the queue's time limit ({self.limits.queue_ttl_s:g} s)",
        )
        ended = await self._store.expire_queued(
            now - timedelta(seconds=self.limits.queue_ttl_s), failure, now
        )
        if ended:
            _LOG.warning("queued jobs expired", extra={"event": "jobs.queue_expired", "count": ended})

    async def _claim(self, job_id: UUID, app: str) -> "Claimed | None":
        """A slot first, then the claim. ``None`` when there is no slot for this app, or the job is gone.

        A claim that raises may still have been written: its commit comes before its answer, and a
        cancelled step stops waiting in between. ``_unclaimed`` then resolves the job and gives the slot
        back, in a task the cancellation cannot stop."""
        slot = contextlib.AsyncExitStack()
        try:
            await slot.enter_async_context(self._admission.admit(app))
        except NoCapacity:
            return None
        try:
            claimed = await self._store.claim_job(job_id, self._clock.now())
        except BaseException as stopped:
            await run_shielded(self._unclaimed(job_id, slot, stopped))
            raise
        if claimed is None:
            await slot.aclose()
            return None
        self._claimed.add(job_id)
        return Claimed(*claimed, slot)

    async def _unclaimed(self, job_id: UUID, slot: contextlib.AsyncExitStack, stopped: BaseException) -> None:
        """A claim whose answer never came. This worker is the only one that claims, so a job it finds
        ``running`` here and does not hold is that claim's, with nobody to run it. Nothing went out for
        it: it is resolved as a job stopped before its first attempt is (``_stopped``), back to the queue
        while it has claims left. A job this worker holds was claimed by another step and is left alone:
        it is running. A failure here is logged, not raised: what stopped the claim is what the step
        reports, and a restart resolves a job this could not reach. The loop is woken at the end, as it
        is when a job ends: the job may be waiting again, whatever the count says."""
        try:
            job = None if job_id in self._claimed else await self._store.get_job(job_id)
            if job is not None and job.state == "running":
                await self._stopped(job, [], stopped)
        except Exception:
            _LOG.exception(
                "could not resolve a claim that was cut short",
                extra={"event": "jobs.async_unclaim_failed", "job_id": str(job_id)},
            )
        finally:
            await slot.aclose()
            self._wake.set()  # the job may wait again, and a slot is free: the loop looks

    def _start(self, claim: "Claimed") -> asyncio.Task[None]:
        task = asyncio.create_task(self._work(claim), name="lpb-async-job")
        self._running.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task[None]) -> None:
        self._running.discard(task)
        self._wake.set()  # a worker is free: the loop looks at the queue again

    async def _work(self, claim: "Claimed") -> None:
        """Run one claimed job to its end. The job's end is in its record, so nothing is raised here
        but a cancellation."""
        job, slot, limit = claim.job, claim.slot, self.limits.result_max_bytes
        attempts: list[Attempt] = []
        try:
            await self._dispatcher.dispatch(
                job, claim.actor, claim.request, attempts, keep=self._keep, keep_bytes=limit
            )
        except JobFailed:
            pass  # recorded on the job, where its owner reads it
        except asyncio.CancelledError as stopped:
            await run_shielded(self._stopped(job, attempts, stopped))
            raise
        except Exception as error:
            await run_shielded(self._dispatcher.close_after_error(job, attempts, error))
            _LOG.exception("async job failed", extra={"event": "jobs.async_failed", "job_id": str(job.id)})
        finally:
            self._claimed.discard(job.id)
            await run_shielded(slot.aclose())

    async def _stopped(self, job: Job, attempts: list[Attempt], stopped: BaseException) -> None:
        """The engine stopped while this job ran. Before its first attempt nothing went out, so it goes
        back to the queue, as a restart would put it (Q2). Otherwise it ends like any cancelled job."""
        if not attempts and await self._store.requeue_unstarted(job.id, MAX_CLAIMS):
            return
        await self._dispatcher.close_after_error(job, attempts, stopped)

    async def _keep(self, job: Job, reply: RunnerReply, attempts: Sequence[Attempt]) -> Job:
        """The end of an async job a runner answered: its bytes are stored in the transaction that ends
        it. A reply over ``result_max_bytes`` is not stored: the job fails ``result_not_stored`` and keeps
        the reply's size and hash, with no location. The runner was paid for an answer nobody can read,
        which the cost reports as ``failed_after_payment`` (D69 (4)).

        A transport that read the reply with that bound (``BoundedRunnerTransport``) says how long all of
        it was and what it hashed to, and holds none of a reply that was over. Any other transport read
        it whole, and it is measured here."""
        limit = self.limits.result_max_bytes
        size = len(reply.content) if reply.size_bytes is None else reply.size_bytes
        shape = ResultRef(
            storage="stored",
            content_type=response_content_type(reply.content_type),  # one rule (#158)
            size_bytes=size,
            sha256=reply.sha256 or hashlib.sha256(reply.content).hexdigest(),
        )
        if size > limit:
            failure = Failure(
                kind="result_not_stored",
                message=f"the runner's reply is {size} bytes, over the {limit} the engine stores",
                payment_sent=any(attempt.payment_sent for attempt in attempts),
            )
            raise JobFailed(await self._dispatcher.finish(job, "failed", failure, shape, attempts))
        days = self.limits.results_days
        expires = None if days is None else self._clock.now() + timedelta(days=days)
        kept = shape.model_copy(
            update={"location": RESULT_LOCATION.format(job_id=job.id), "expires_at": expires}
        )
        return await self._dispatcher.finish(job, "succeeded", None, kept, attempts, content=reply.content)
