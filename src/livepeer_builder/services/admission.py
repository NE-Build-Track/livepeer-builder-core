"""Admission limits: refuse work with NoCapacity *before* any payment (proposal gap 6). An engine that
runs no jobs at all, a payment clearinghouse's (D82), admits none. The async worker's own gate, its
lane, is here too (ISS-26): what a queued job takes before it is claimed."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from livepeer_builder.errors import InvalidRequest, NoCapacity


class AdmissionLimiter:
    """Caps concurrent jobs globally and per runner app. ``None`` disables a cap.

    Admitting and releasing never await, so no other task runs in between and no lock is needed. A
    release can therefore never be cancelled half way, even under anyio, which cancels every await of a
    cancelled request task (#44 review).

    ``runs_jobs=False`` is an engine in its ``clearinghouse`` role (#169): every job is refused here,
    before it is recorded, a runner is chosen or a payment is asked for. It is not a lack of capacity,
    so the answer is not ``NoCapacity``: a retry would be refused again.

    ``async_workers`` is how many async jobs run at once (``LPB_ASYNC_WORKERS``), inside the two caps:
    the worker takes its slots through ``async_lane``."""

    def __init__(
        self,
        max_concurrent: int | None,
        max_per_app: int | None,
        *,
        runs_jobs: bool = True,
        async_workers: int = 4,
    ) -> None:
        self._max_concurrent = max_concurrent
        self._max_per_app = max_per_app
        self._runs_jobs = runs_jobs
        self._running = 0
        self._per_app: dict[str, int] = {}
        self.async_lane = AsyncLane(self, async_workers)

    @asynccontextmanager
    async def admit(self, app: str) -> AsyncIterator[None]:
        self.check_runs_jobs()
        refusal = self.refusal(app)
        if refusal is not None:
            raise NoCapacity(refusal)
        self.take(app)
        try:
            yield
        finally:
            self.give(app)

    def refusal(self, app: str) -> str | None:
        """Why a job of ``app`` gets no slot now; ``None`` when it would get one."""
        if self._max_concurrent is not None and self._running >= self._max_concurrent:
            return "engine is at its concurrent job limit"
        if self._max_per_app is not None and self._per_app.get(app, 0) >= self._max_per_app:
            return f"app {app!r} is at its concurrent job limit"
        return None

    def take(self, app: str) -> None:
        """Count one more job of ``app``. The caller asked ``refusal`` first, with no await in between."""
        self._running += 1
        self._per_app[app] = self._per_app.get(app, 0) + 1

    def give(self, app: str) -> None:
        self._running -= 1
        self._per_app[app] -= 1
        if self._per_app[app] == 0:
            del self._per_app[app]

    def check_runs_jobs(self) -> None:
        """Refuse a job on an engine that runs none: where a job is admitted, and where an async job is
        submitted, which takes no slot (ISS-26)."""
        if not self._runs_jobs:
            raise InvalidRequest(
                "this engine is in its clearinghouse role (LPB_ROLE) and runs no jobs: a job runs in "
                "the enterprise's own engine, which signs at the clearinghouse's signer itself"
            )

    @property
    def running(self) -> int:
        return self._running

    @property
    def free(self) -> int | None:
        """How many more jobs the engine admits now; ``None`` when it has no limit."""
        return None if self._max_concurrent is None else max(self._max_concurrent - self._running, 0)

    def full_apps(self) -> frozenset[str]:
        """The runner apps at their concurrent job limit: the worker leaves their queued jobs waiting."""
        cap = self._max_per_app
        return frozenset() if cap is None else frozenset(a for a, n in self._per_app.items() if n >= cap)


class AsyncSlot:
    """What one async job holds from before its claim until its end is written: a worker, an admission
    slot and its own id. ``release`` gives all three back, once, however often it is called."""

    def __init__(self, lane: "AsyncLane", app: str, job_id: UUID) -> None:
        self._lane: AsyncLane | None = lane
        self._app = app
        self._job_id = job_id

    def release(self) -> None:
        lane, self._lane = self._lane, None
        if lane is not None:
            lane.released(self._app, self._job_id)


class AsyncLane:
    """The one gate an async job passes before it is claimed (ISS-26).

    ``reserve`` checks and takes everything a claim needs in one call that never awaits: the lane is
    open, the job is not already held, a worker is free (``LPB_ASYNC_WORKERS``) and the engine admits a
    job of that app. So two steps of the worker that overlap cannot both take the last worker or hold
    the same job, and a job whose claim is being written is already held: another step leaves it alone.
    ``close`` is the engine's stop: every later ``reserve`` is refused, also the next one of a step
    that is half way through a page of the queue. Immediate jobs and streams are not the lane's."""

    def __init__(self, limiter: AdmissionLimiter, workers: int) -> None:
        self._limiter = limiter
        self._workers = workers
        self._held: set[UUID] = set()
        self._closed = False

    def reserve(self, app: str, job_id: UUID) -> AsyncSlot | None:
        """A slot for the job, or ``None``: the lane is closed, the job is held, no worker is free, or
        the engine or the app is at its limit."""
        if self._closed or job_id in self._held or len(self._held) >= self._workers:
            return None
        if self._limiter.refusal(app) is not None:
            return None
        self._limiter.take(app)
        self._held.add(job_id)
        return AsyncSlot(self, app, job_id)

    def released(self, app: str, job_id: UUID) -> None:
        self._held.discard(job_id)
        self._limiter.give(app)

    def close(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    def holds(self, job_id: UUID) -> bool:
        return job_id in self._held

    @property
    def held(self) -> int:
        """How many jobs hold a slot of the lane, from before their claim until their end is written."""
        return len(self._held)

    @property
    def room(self) -> int:
        """How many more async jobs could be reserved now: free workers, within the engine's free
        slots; none once the lane is closed."""
        if self._closed:
            return 0
        workers = max(self._workers - len(self._held), 0)
        free = self._limiter.free
        return workers if free is None else min(workers, free)
