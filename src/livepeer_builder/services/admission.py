"""Admission limits: refuse work with NoCapacity *before* any payment (proposal gap 6)."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from livepeer_builder.errors import NoCapacity


class AdmissionLimiter:
    """Caps concurrent jobs globally and per runner app. ``None`` disables a cap."""

    def __init__(self, max_concurrent: int | None, max_per_app: int | None) -> None:
        self._max_concurrent = max_concurrent
        self._max_per_app = max_per_app
        self._running = 0
        self._per_app: dict[str, int] = {}
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def admit(self, app: str) -> AsyncIterator[None]:
        async with self._lock:
            if self._max_concurrent is not None and self._running >= self._max_concurrent:
                raise NoCapacity("engine is at its concurrent job limit")
            if self._max_per_app is not None and self._per_app.get(app, 0) >= self._max_per_app:
                raise NoCapacity(f"app {app!r} is at its concurrent job limit")
            self._running += 1
            self._per_app[app] = self._per_app.get(app, 0) + 1
        try:
            yield
        finally:
            async with self._lock:
                self._running -= 1
                self._per_app[app] -= 1
                if self._per_app[app] == 0:
                    del self._per_app[app]

    @property
    def running(self) -> int:
        return self._running
