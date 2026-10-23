"""BuilderEngine: wires settings, adapters and services into one object (App Wiring in ARCHITECTURE.md).

Imported mode: ``engine = await BuilderEngine.create(settings)`` then call services with an
``ActorContext`` the application built. Service mode: livepeer_builder.http wraps this same object.
Construction (supply, decorate, assemble) lives in livepeer_builder.runtime.assembly.
"""

import asyncio
import contextlib
import logging
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

from livepeer_builder.adapters.auth import AuthenticatorChain
from livepeer_builder.config import EngineSettings
from livepeer_builder.contracts import EngineHealth, PurgeReport
from livepeer_builder.protocols import (
    AccessPolicy,
    Clock,
    EngineStore,
    EventFeed,
    SessionManager,
)
from livepeer_builder.services.admission import AdmissionLimiter
from livepeer_builder.services.costs import CostService
from livepeer_builder.services.discovery import DiscoveryService
from livepeer_builder.services.jobs import JobService
from livepeer_builder.services.keys import KeyService
from livepeer_builder.services.payments import PaymentsService

if TYPE_CHECKING:
    from livepeer_builder.api import ActorApi, AdminApi, EngineApi
    from livepeer_builder.runtime.assembly import Decorate, EngineSupply

_LOG = logging.getLogger(__name__)


def package_version() -> str:
    try:
        return version("livepeer-builder")
    except PackageNotFoundError:
        return "0.0.0+unknown"


class BuilderEngine:
    def __init__(
        self,
        *,
        settings: EngineSettings,
        store: EngineStore,
        clock: Clock,
        policy: AccessPolicy,
        authenticator: AuthenticatorChain,
        discovery: DiscoveryService,
        jobs: JobService,
        payments: PaymentsService,
        costs: CostService,
        keys: KeyService | None,
        admission: AdmissionLimiter,
        events: EventFeed,
        sessions: SessionManager,
        own_authenticators: int = 0,
        resources: contextlib.AsyncExitStack | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.clock = clock
        self.policy = policy
        self.authenticator = authenticator
        self.discovery = discovery
        self.jobs = jobs
        self.payments = payments
        self.costs = costs
        self.keys = keys
        self.admission = admission
        self.events = events
        self.sessions = sessions
        self._own_authenticators = own_authenticators  # the deployment's, tried before API keys
        self._resources = resources or contextlib.AsyncExitStack()  # what construction opened
        self._stopped = False
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    @classmethod
    async def create(
        cls,
        settings: EngineSettings,
        *,
        supply: "EngineSupply | None" = None,
        decorate: "Decorate | None" = None,
    ) -> "BuilderEngine":
        """Build an engine: supplied parts replace defaults, then ``decorate`` wraps the parts.

        See livepeer_builder.runtime.assembly and docs/design-docs/extending-the-engine.md.
        """
        from livepeer_builder.runtime.assembly import create

        return await create(settings, supply=supply, decorate=decorate)

    async def start(self) -> None:
        """Resolve jobs a restart interrupted, take one discovery snapshot, then start background loops."""
        if self._stopped:
            raise RuntimeError("engine is stopped; create a new one")
        await self.jobs.recover_interrupted()
        await self.discovery.refresh()
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self.discovery.run(self._stop), name="lpb-discovery"),
            asyncio.create_task(self.costs.run(self._stop), name="lpb-cost-sync"),
            asyncio.create_task(self._retention_loop(), name="lpb-retention"),
        ]
        _LOG.info(
            "engine started",
            extra={
                "event": "engine.started",
                "version": package_version(),
                "api_keys": "on" if self.keys is not None else "off",
                "own_authenticators": self._own_authenticators,
                "grant_desk": "on" if self.payments.grants is not None else "off",
            },
        )

    async def stop(self) -> None:
        await self.jobs.close_streams()  # before the store closes: open streams drain, then end abandoned
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        await self._resources.aclose()  # reverse order: clients before the store; failures are logged
        self._stopped = True
        _LOG.info("engine stopped", extra={"event": "engine.stopped"})

    async def __aenter__(self) -> "BuilderEngine":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    def health(self) -> EngineHealth:
        snapshot = self.discovery.snapshot
        return EngineHealth(
            ready=snapshot.last_attempt_at is not None,
            discovery_fresh=self.discovery.is_fresh(),
            discovery_observed_at=snapshot.observed_at,
            discovery_error=snapshot.last_error,
            cost_sync_at=self.costs.last_synced_at,
            cost_sync_error=self.costs.last_error,
            running_jobs=self.admission.running,
            allocation_mode=self.payments.mode,
            version=package_version(),
        )

    async def purge(self) -> PurgeReport:
        maintenance = self.store.maintenance
        if maintenance is None:
            return PurgeReport(jobs_deleted=0, usage_rows_deleted=0)
        return await maintenance.purge(
            self.clock.now(), self.settings.retention_jobs_days, self.settings.retention_usage_days
        )

    async def schema_revision(self) -> str | None:
        maintenance = self.store.maintenance
        return None if maintenance is None else await maintenance.schema_revision()

    async def _retention_loop(self) -> None:
        if self.settings.retention_jobs_days is None and self.settings.retention_usage_days is None:
            return
        while not self._stop.is_set():
            try:
                report = await self.purge()
                _LOG.info("retention purge", extra={"event": "store.purged", **report.model_dump()})
            except Exception:
                _LOG.exception("retention purge failed", extra={"event": "store.purge_failed"})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.retention_interval_s)
            except TimeoutError:
                continue


if TYPE_CHECKING:  # mypy proves BuilderEngine offers every role's surface (livepeer_builder.api)

    def _offers(engine: BuilderEngine) -> tuple["ActorApi", "AdminApi", "EngineApi"]:
        return engine, engine, engine
