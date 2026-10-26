"""The cache behind ``GET /discovery``: the current directory and each orchestrator's last good inventory.

Retention follows livepeer-node's discovery: a failed probe keeps an orchestrator's last inventory
until ``max_failed_refreshes`` consecutive failures (``0`` evicts on the first). A failed directory
load keeps the previous directory; an orchestrator that leaves the directory drops out at once.

The refresh loop survives its own errors: a failed pass is logged and the next tick runs (#46 review). A
directory load has ``directory_timeout_s``, so a stuck one cannot stop probing. The directory error shown on
the unauthenticated ``/readyz`` is a stable reason, never another system's text.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel

from livepeer_builder.discovery.abi import ChainError
from livepeer_builder.discovery.directory import Orchestrator, OrchestratorDirectory
from livepeer_builder.discovery.probe import Probe

_LOG = logging.getLogger(__name__)
_OWN_MESSAGES = (ValueError, ChainError)  # written by this package, so safe to show


class Probes(Protocol):
    async def probe_all(self, orchestrators: Sequence[Orchestrator]) -> list[Probe]: ...


class CatalogStatus(BaseModel):
    ready: bool  # a directory has loaded and a probe pass has finished
    orchestrators: int  # in the directory
    serving: int  # with an inventory in GET /discovery
    directory_loaded_at: datetime | None
    probed_at: datetime | None
    directory_error: str | None  # the last directory load's error; None once one succeeds


class Catalog:
    def __init__(
        self,
        directory: OrchestratorDirectory,
        prober: Probes,
        *,
        max_failed_refreshes: int = 6,
        directory_timeout_s: float = 300.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if max_failed_refreshes < 0:
            raise ValueError("max_failed_refreshes cannot be negative")
        self._directory = directory
        self._prober = prober
        self._evict_after = max(max_failed_refreshes, 1)
        self._directory_timeout_s = directory_timeout_s
        self._now = now
        self._orchestrators: tuple[Orchestrator, ...] | None = None
        self._inventory: dict[str, dict[str, Any]] = {}
        self._failures: dict[str, int] = {}
        self._loaded_at: datetime | None = None
        self._probed_at: datetime | None = None
        self._directory_error: str | None = None

    async def load_directory(self) -> None:
        try:
            async with asyncio.timeout(self._directory_timeout_s):
                orchestrators = await self._directory.orchestrators()
        except Exception as exc:  # keep the directory we had; report the error
            self._directory_error = _reason(exc)
            _LOG.warning(
                "directory load failed; keeping the previous directory",
                extra={"event": "discovery.directory_failed", "error": f"{type(exc).__name__}: {exc}"[:300]},
            )
            return
        self._orchestrators = orchestrators
        listed = {o.url for o in orchestrators}
        self._inventory = {url: entry for url, entry in self._inventory.items() if url in listed}
        self._failures = {url: count for url, count in self._failures.items() if url in listed}
        self._loaded_at, self._directory_error = self._now(), None

    async def probe(self) -> None:
        if self._orchestrators is None:
            return
        for probe in await self._prober.probe_all(self._orchestrators):
            url = probe.orchestrator.url
            if probe.entry is not None:
                self._inventory[url], self._failures[url] = probe.entry, 0
                continue
            self._failures[url] = self._failures.get(url, 0) + 1
            if self._failures[url] >= self._evict_after:
                self._inventory.pop(url, None)
        self._probed_at = self._now()

    def inventory(self) -> list[dict[str, Any]]:
        """livepeer-node's ``/discovery`` inventory: an entry per serving orchestrator, directory order."""
        return [self._inventory[o.url] for o in self._orchestrators or () if o.url in self._inventory]

    def status(self) -> CatalogStatus:
        return CatalogStatus(
            ready=self._loaded_at is not None and self._probed_at is not None,
            orchestrators=len(self._orchestrators or ()),
            serving=len(self.inventory()),
            directory_loaded_at=self._loaded_at,
            probed_at=self._probed_at,
            directory_error=self._directory_error,
        )

    async def run(self, stop: asyncio.Event, *, directory_interval_s: float, probe_interval_s: float) -> None:
        """Reload the directory every ``directory_interval_s`` (every probe tick until one has loaded) and
        probe every ``probe_interval_s``, until ``stop`` is set."""
        next_load = 0.0
        while not stop.is_set():
            try:
                if time.monotonic() >= next_load:
                    await self.load_directory()
                    if self._orchestrators is not None:
                        next_load = time.monotonic() + directory_interval_s
                await self.probe()
            except Exception:
                _LOG.exception(
                    "discovery refresh failed; trying again next tick",
                    extra={"event": "discovery.refresh_failed"},
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=probe_interval_s)
            except TimeoutError:
                continue


def _reason(exc: BaseException) -> str:
    """For /readyz: the error's type, and its message only when this package wrote it."""
    message = str(exc) if isinstance(exc, _OWN_MESSAGES) else ""
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__
