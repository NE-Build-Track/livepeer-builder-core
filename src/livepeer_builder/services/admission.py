"""Admission limits: refuse work with NoCapacity *before* any payment (proposal gap 6). An engine that
runs no jobs at all, a payment clearinghouse's (D82), admits none."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from livepeer_builder.errors import InvalidRequest, NoCapacity


class AdmissionLimiter:
    """Caps concurrent jobs globally and per runner app. ``None`` disables a cap.

    Admitting and releasing never await, so no other task runs in between and no lock is needed. A
    release can therefore never be cancelled half way, even under anyio, which cancels every await of a
    cancelled request task (#44 review).

    ``runs_jobs=False`` is an engine in its ``clearinghouse`` role (#169): every job is refused here,
    before it is recorded, a runner is chosen or a payment is asked for. It is not a lack of capacity,
    so the answer is not ``NoCapacity``: a retry would be refused again."""

    def __init__(
        self, max_concurrent: int | None, max_per_app: int | None, *, runs_jobs: bool = True
    ) -> None:
        self._max_concurrent = max_concurrent
        self._max_per_app = max_per_app
        self._runs_jobs = runs_jobs
        self._running = 0
        self._per_app: dict[str, int] = {}

    @asynccontextmanager
    async def admit(self, app: str) -> AsyncIterator[None]:
        self.check_runs_jobs()
        if self._max_concurrent is not None and self._running >= self._max_concurrent:
            raise NoCapacity("engine is at its concurrent job limit")
        if self._max_per_app is not None and self._per_app.get(app, 0) >= self._max_per_app:
            raise NoCapacity(f"app {app!r} is at its concurrent job limit")
        self._running += 1
        self._per_app[app] = self._per_app.get(app, 0) + 1
        try:
            yield
        finally:
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
                "the enterprise's own engine, which pays through this one"
            )

    @property
    def running(self) -> int:
        return self._running

    @property
    def has_room(self) -> bool:
        """Whether the engine is under its concurrent job limit: what the async worker asks before it
        looks at the queue (ISS-26). ``admit`` is still what admits."""
        return self._max_concurrent is None or self._running < self._max_concurrent

    def full_apps(self) -> frozenset[str]:
        """The runner apps at their concurrent job limit: the worker leaves their queued jobs waiting."""
        cap = self._max_per_app
        return frozenset() if cap is None else frozenset(a for a, n in self._per_app.items() if n >= cap)
