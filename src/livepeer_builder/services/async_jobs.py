"""The worker that runs queued async jobs (D69, ISS-26).

``lpb_jobs`` is the queue. ``JobService.submit`` stores a job ``queued``; this worker claims it and runs
the same paid call an immediate job runs (``services/dispatch.py``), in the engine's own process. Rules:

- **One owner for a claimed job** (``Held``, ``services/async_held.py``). What a job takes before its
  claim (a worker, an admission slot and its own id, in one call that never awaits:
  ``AdmissionLimiter.async_lane``), the number of its claim, its deadline and the duty to write its end
  are one object's, from before the claim until the end is written. Nothing is given back before that.
- **Every write after the claim checks the claim.** The claim has a number, and the first attempt, the
  requeue and the end of the job apply only while that number holds the job. A worker that lost the
  job writes nothing more for it and does not end it.
- **Oldest first**, leaving out an app that is at its per-app limit and one no runner can take now, so
  one busy or absent app does not hold up the others.
- **Payment happens at dispatch, never while a job waits.** The actor's allocation is looked up when the
  job is claimed: one revoked or exhausted in the meantime fails the job unpaid.
- **A job waits at most ``queue_ttl_s``**, for a worker, for a runner to be listed or to have room, or
  for its actor's credential to be readable. One still queued after that ends ``failed``,
  ``no_capacity``, with nothing paid.
- **One deadline.** ``timeout_s`` counts from the claim, once for all of the job's attempts.
- **Nothing is run twice.** A job that was claimed is never dispatched again by the engine, with one
  exception: one that was stopped before its first attempt was written goes back to the queue, at most
  ``MAX_CLAIMS`` claims that count (``services/failover.py`` at start, ``Ends`` here).
- **The end is the worker's to write.** One the store refused is kept, with the job's slot, and written
  by a later step: no job this worker claimed stays ``running`` once the store answers again.

``run_once()`` is one step, for tests and for anybody who drives the queue by hand; ``run(stop)`` is
the loop the engine starts, woken at each submit and every ``poll_s``; ``close()`` and ``drain()`` are
the engine's stop.
"""

import asyncio
import contextlib
import functools
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from livepeer_builder.contracts import Attempt, Failure, Job, ResultRef, RunnerReply
from livepeer_builder.contracts.http import response_content_type
from livepeer_builder.errors import JobFailed
from livepeer_builder.protocols import Clock, JobRepo
from livepeer_builder.services.admission import AdmissionLimiter
from livepeer_builder.services.async_held import STOPPED, End, Ends, Held, Waiting, log_lost
from livepeer_builder.services.dispatch import ClaimLost, Dispatcher, NotNow

_LOG = logging.getLogger(__name__)

RESULT_LOCATION = "/v1/jobs/{job_id}/result"  # where REST serves a stored result
_MIB = 1024 * 1024


@dataclass(frozen=True)
class AsyncLimits:
    """The tuning of async jobs, from ``EngineSettings`` (the ``LPB_ASYNC_*`` settings; defaults D69 (6)).
    How many run at once is the admission limiter's (``AdmissionLimiter.async_lane``)."""

    max_queued: int = 1000  # a submit beyond this is refused, NoCapacity
    queue_ttl_s: float = 3600.0  # the longest a job waits to be dispatched
    result_max_bytes: int = 16 * _MIB  # a larger reply is not stored
    results_days: int | None = 7  # how long a stored result is kept; None: as long as its job
    poll_s: float = 1.0  # how often the loop looks at the queue when nothing wakes it
    drain_timeout_s: float = 30.0  # at stop, how long running jobs may take to end
    backstop_grace_s: float = 5.0  # how far past its deadline a dispatch runs before the worker stops it


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
        self._lane = admission.async_lane  # the gate every claim passes, and the engine's stop closes
        self._clock = clock
        self.limits = limits or AsyncLimits()
        self._ends = Ends(store, dispatcher)
        self._waiting = Waiting()  # the jobs not to be claimed again yet
        self._running: set[asyncio.Task[None]] = set()
        self._steps: set[asyncio.Future[None]] = set()  # the steps under way, for ``drain``
        self._wake = asyncio.Event()
        self._halt = asyncio.Event()  # set by ``close``: what a loop that is backing off waits for
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
        """Claim and start queued jobs until the engine stops. A step that fails is logged and tried
        again after ``poll_s``, whatever wakes the loop meanwhile: the queue is in the database, so
        nothing is lost, and a failure that repeats does not turn the loop into a spin.

        The loop reads the database only while jobs wait, for as long as the last count was not zero,
        and once each time it is woken: by a submit, or by a job's end. ``BuilderEngine.start()`` counts
        the queue before it starts the loop (a restart may have left jobs in it), and a submit raises the
        count in this process, which is the only one that writes the queue (one engine process per
        database). So an engine nobody submits to adds no query, not even beside the caller's own first
        writes at start, and a waiting job is looked at every ``poll_s`` and whenever a job is queued or a
        worker comes free. The wake counts by itself, whatever the count says: the count is a memory, the
        queue is in the database, and a job is never left waiting on the memory alone (#184 review). An
        end the store refused is a reason to step too, until it is written."""
        self._loop, failed = asyncio.current_task(), False
        while not stop.is_set() and not self._lane.closed:
            woken = self._wake.is_set()
            self._wake.clear()
            if failed or woken or self._queued[0] or self._ends.owed:
                failed = await self._step_logged()
            wait = self._halt.wait() if failed else self._wake.wait()  # no wake cuts a back-off short
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wait, timeout=self.limits.poll_s)

    async def _step_logged(self) -> bool:
        """One step of the loop. ``True`` when it failed, so the next turn tries it again."""
        try:
            await self._start_ready()
        except Exception:
            _LOG.exception("async job step failed", extra={"event": "jobs.async_step_failed"})
            return True
        return bool(self._ends.owed)

    async def run_once(self) -> int:
        """One step: write the ends an earlier step could not, end the jobs that waited too long, claim
        what there is room for, and wait for the jobs this step started. Returns how many it started."""
        started = await self._start_ready()
        if started:
            await asyncio.wait(started)
        return len(started)

    def close(self) -> None:
        """Claim nothing more, from this call on: the first thing the engine's stop does, before it
        waits for anything. It never awaits, so no claim slips in between. A submit is still accepted:
        its job waits for the next start."""
        self._lane.close()
        self._halt.set()
        self._wake.set()

    async def drain(self) -> None:
        """Engine stop: claim nothing more, let running jobs end for up to ``drain_timeout_s``, then cancel
        the rest. A cancelled job goes back to the queue if nothing went out for it, and otherwise ends
        as a cancelled immediate one does: ``uncertain`` if it may have paid. Returns once every write of
        a job's end has ended, also one its task no longer waits for, so the store is never closed
        under one. An end the store still refuses is left to the next start."""
        self.close()
        patience = self.limits.drain_timeout_s
        loop, self._loop = self._loop, None
        if loop is not None and not loop.done():  # let the step it is in finish: its claims then run
            await asyncio.wait([loop], timeout=patience)
            loop.cancel()
            await asyncio.wait([loop])
        if self._steps:  # a step somebody drives by hand: what it claimed is started before we look
            await asyncio.wait(list(self._steps), timeout=patience)
        running = list(self._running)
        if running:
            _, late = await asyncio.wait(running, timeout=patience)
            for task in late:
                task.cancel()
            if late:
                await asyncio.wait(late)
            _LOG.info("drained async jobs", extra={"event": "jobs.async_drained", "cancelled": len(late)})
        await self._ends.written()
        await self._ends.settle_owed()

    async def _start_ready(self) -> list[asyncio.Task[None]]:
        """Claim what there is room for, then start it. Every read and write of a step is done before
        any of its jobs starts, so a job's own writes never run beside the step's. A claim that raises
        does not stop the step: its job is left for later, the jobs behind it are claimed, what was
        claimed is started, and then the step raises what the first such claim raised."""
        self._admission.check_runs_jobs()  # an engine in its clearinghouse role runs no job
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._steps.add(done)
        claimed: list[Held] = []
        errors: dict[UUID, Exception] = {}  # the jobs whose claim raised in this step, and what it raised
        try:
            await self._ends.settle_owed()
            await self._expire()
            await self._claim_ready(claimed, errors)
            await self.count_queue()  # before any job starts: nothing else is writing yet
        finally:  # a claimed job runs, whatever stopped the step after its claim
            started = [self._start(held) for held in claimed]
            self._steps.discard(done)
            done.set_result(None)
        if errors:
            raise next(iter(errors.values()))
        return started

    async def _claim_ready(self, claimed: list[Held], errors: dict[UUID, Exception]) -> None:
        """Page through the queue, oldest first, while there is room and the last page changed
        something: a job was claimed, a job's claim failed and it was set aside, or an app turned out to
        have no runner for it now. The lane is asked again for every job, so a stop that arrives while a
        page is being claimed ends the claiming there."""
        absent: set[str] = set()  # apps nobody can run now: left out as an app at its limit is
        moved = True
        while moved and self._lane.room > 0:
            skip = self._admission.full_apps() | absent
            # a job set aside, or one whose claim another step is writing, is still on the page
            limit = self._lane.room + self._lane.held + self._waiting.set_aside()
            page = await self._store.queued_jobs(limit, skip)
            moved = False
            for job_id, app in page:
                if app in absent or job_id in errors or not self._waiting.due(job_id):
                    continue
                if self._dispatcher.waits(app):
                    absent.add(app)
                    moved = True
                    continue
                moved = await self._claim_one(job_id, app, claimed, errors) or moved

    async def _claim_one(
        self, job_id: UUID, app: str, claimed: list[Held], errors: dict[UUID, Exception]
    ) -> bool:
        """Claim one job. Whether anything changed: it was claimed, or its claim raised and it waits."""
        try:
            held = await self._claim(job_id, app)
        except Exception as error:
            errors[job_id] = error
            self._waiting.put(job_id, self.limits.poll_s)
            return True
        if held is not None:
            # Noted as soon as it is made: a claim is committed and holds a slot, so one made before a
            # later claim failed must still be started (#184 review).
            claimed.append(held)
        return held is not None

    async def _expire(self) -> None:
        now = self._clock.now()
        limit = self.limits.queue_ttl_s
        message = f"the job was not dispatched within the queue's time limit ({limit:g} s)"
        failure = Failure(kind="no_capacity", message=message)
        ended = await self._store.expire_queued(now - timedelta(seconds=limit), failure, now)
        if ended:
            _LOG.warning("queued jobs expired", extra={"event": "jobs.queue_expired", "count": ended})

    async def _claim(self, job_id: UUID, app: str) -> Held | None:
        """The gate first, then the claim. ``None`` when the gate refuses (closed, no worker or slot
        free, or the job is another step's) or the job is gone.

        The gate is passed before the claim is awaited, so while the claim is being written the job is
        already this step's, and another step leaves it alone. A claim that raises may still have been
        written: its commit comes before its answer, and a cancelled step stops waiting in between. The
        job is then resolved (``Ends``) in a task the cancellation cannot stop."""
        slot = self._lane.reserve(app, job_id)
        if slot is None:
            return None
        held = Held(job_id, slot)
        try:
            claimed = await self._store.claim_job(job_id, self._clock.now())
        except BaseException as stopped:
            await self._ends.shielded(held, End(type(stopped).__name__, back=True))
            self._wake.set()  # the job may wait again: the loop looks
            raise
        if claimed is None:
            slot.release()
            return None
        held.job, held.request, held.actor, held.claim = claimed
        # timeout_s counts from here, once for every attempt (D69)
        held.deadline = asyncio.get_running_loop().time() + held.request.timeout_s
        return held

    def _start(self, held: Held) -> asyncio.Task[None]:
        task = asyncio.create_task(self._work(held), name="lpb-async-job")
        self._running.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task[None]) -> None:
        self._running.discard(task)
        self._wake.set()  # a worker is free: the loop looks at the queue again

    async def _work(self, held: Held) -> None:
        """Run one claimed job to its end. The job's end is in its record, so nothing is raised here
        but a cancellation. Every end is written in a task of its own that ``drain`` waits for."""
        try:
            end = await self._dispatch(held)
        except asyncio.CancelledError:
            await self._ends.shielded(held, STOPPED)
            raise
        await self._ends.shielded(held, end)

    async def _dispatch(self, held: Held) -> End | None:
        """The paid call. ``None`` when it wrote the job's end or lost the job; else the end still owed."""
        job, request, actor = held.claimed()
        keep = functools.partial(self._keep, held.claim)
        try:
            async with asyncio.timeout_at(held.deadline + self.limits.backstop_grace_s):
                await self._dispatcher.dispatch(
                    job,
                    actor,
                    request,
                    held.attempts,
                    keep=keep,
                    keep_bytes=self.limits.result_max_bytes,
                    deadline=held.deadline,
                    claim=held.claim,
                )
        except JobFailed:
            pass  # recorded on the job, where its owner reads it
        except ClaimLost:
            log_lost(held)
        except NotNow as reason:
            self._waiting.put(job.id, self.limits.poll_s)
            return End("NotNow", back=True, counted=False, reason=str(reason))
        except Exception as error:  # the worker's backstop too: a transport that ignored its timeout
            _LOG.exception("async job failed", extra={"event": "jobs.async_failed", "job_id": str(job.id)})
            return End(type(error).__name__)
        self._waiting.forget(job.id)
        return None

    async def _keep(
        self, claim: int | None, job: Job, reply: RunnerReply, attempts: Sequence[Attempt]
    ) -> Job:
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
        finish = functools.partial(self._dispatcher.finish, claim=claim)
        if size > limit:
            failure = Failure(
                kind="result_not_stored",
                message=f"the runner's reply is {size} bytes, over the {limit} the engine stores",
                payment_sent=any(attempt.payment_sent for attempt in attempts),
            )
            raise JobFailed(await finish(job, "failed", failure, shape, attempts))
        days = self.limits.results_days
        expires = None if days is None else self._clock.now() + timedelta(days=days)
        kept = shape.model_copy(
            update={"location": RESULT_LOCATION.format(job_id=job.id), "expires_at": expires}
        )
        return await finish(job, "succeeded", None, kept, attempts, content=reply.content)
