"""BuilderEngine: wires settings, adapters and services into one object (App Wiring in ARCHITECTURE.md).

Imported mode: ``engine = await BuilderEngine.from_settings(settings)`` then call services with an
``ActorContext`` the application built. Service mode: livepeer_builder.http wraps this same object.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from importlib.metadata import PackageNotFoundError, version

from livepeer_builder.adapters.auth import ApiKeyAuthenticator, AuthenticatorChain
from livepeer_builder.adapters.batteries import BatteriesClient, BatteriesUsageSource
from livepeer_builder.adapters.clock import SystemClock
from livepeer_builder.adapters.sdk import SdkRunnerTransport, SignerDiscoverySource
from livepeer_builder.adapters.secrets import EncryptedDbSecretStore, RoutingSecretStore, SettingsSecretStore
from livepeer_builder.adapters.static import StaticDiscoverySource
from livepeer_builder.config import EngineSettings
from livepeer_builder.contracts import EngineHealth, SecretRef
from livepeer_builder.errors import InvalidRequest
from livepeer_builder.protocols import (
    AccessPolicy,
    AllocationManager,
    Authenticator,
    Clock,
    DiscoverySource,
    EngineStore,
    EventFeed,
    RunnerTransport,
    SecretStore,
    SelectionPolicy,
    SessionManager,
    UsageSource,
)
from livepeer_builder.services.admission import AdmissionLimiter
from livepeer_builder.services.costs import CostService, CostSyncWorker
from livepeer_builder.services.discovery import DiscoveryService, MostCapacityFirst
from livepeer_builder.services.jobs import JobService
from livepeer_builder.services.keys import KeyService
from livepeer_builder.services.payments import PaymentsService
from livepeer_builder.services.policy import DefaultPolicy
from livepeer_builder.services.reserved import ReservedEventFeed, ReservedSessions
from livepeer_builder.services.streams import StreamLimits
from livepeer_builder.store import PurgeReport, SqlStore, current_revision, purge

_LOG = logging.getLogger(__name__)
_SHARED_KEY = "shared_payment_key"


def package_version() -> str:
    try:
        return version("livepeer-builder")
    except PackageNotFoundError:
        return "0.0.0+unknown"


def _own(stack: contextlib.AsyncExitStack, resource: object, close: Callable[[], Awaitable[None]]) -> None:
    """Register ``close`` on ``stack``; a failure is logged, never raised, so the rest still close."""

    async def close_logged() -> None:
        try:
            await close()
        except Exception:
            _LOG.exception(
                "close failed", extra={"event": "engine.close_failed", "resource": type(resource).__name__}
            )

    stack.push_async_callback(close_logged)


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
        keys: KeyService,
        admission: AdmissionLimiter,
        events: EventFeed,
        sessions: SessionManager,
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
        self._resources = (
            resources or contextlib.AsyncExitStack()
        )  # what construction opened; stop() closes it
        self._stopped = False
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    @classmethod
    async def from_settings(
        cls,
        settings: EngineSettings | None = None,
        *,
        store: EngineStore | None = None,
        clock: Clock | None = None,
        policy: AccessPolicy | None = None,
        authenticators: Sequence[Authenticator] = (),
        discovery_source: DiscoverySource | None = None,
        transport: RunnerTransport | None = None,
        selection: SelectionPolicy | None = None,
        allocation_manager: AllocationManager | None = None,
        usage_source: UsageSource | None = None,
        secret_store: SecretStore | None = None,
    ) -> "BuilderEngine":
        """Build an engine. Every argument after ``settings`` overrides one default adapter."""
        settings = settings or EngineSettings.from_env()
        if settings.batteries_url and not (
            settings.batteries_operator_token and settings.batteries_operator_token.get_secret_value()
        ):
            raise InvalidRequest("LPB_BATTERIES_OPERATOR_TOKEN is required when LPB_BATTERIES_URL is set")
        # Each resource is registered the moment it opens; a failure part-way closes what opened.
        async with contextlib.AsyncExitStack() as stack:
            clock = clock or SystemClock()
            policy = policy or DefaultPolicy()
            if store is None:
                store = await SqlStore.connect(settings.database_url, migrate=settings.auto_migrate)
                _own(stack, store, store.close)

            shared_key = (
                settings.shared_payment_key.get_secret_value() if settings.shared_payment_key else None
            )
            configured = SettingsSecretStore({_SHARED_KEY: shared_key} if shared_key else {})
            shared_ref: SecretRef | None = configured.ref(_SHARED_KEY) if shared_key else None
            if secret_store is None:
                database = (
                    EncryptedDbSecretStore(store, settings.secret_key.get_secret_value(), clock)
                    if settings.secret_key
                    else None
                )
                secret_store = RoutingSecretStore(configured, database)

            token = settings.batteries_token.get_secret_value() if settings.batteries_token else None
            if allocation_manager is None and settings.batteries_url and token:
                client = BatteriesClient(
                    settings.batteries_url, token, timeout_s=settings.batteries_timeout_s
                )
                allocation_manager = client
                _own(stack, client, client.aclose)
            grants_client = None
            operator_token = None
            if settings.batteries_operator_token:
                operator_token = settings.batteries_operator_token.get_secret_value()
            if settings.batteries_url and operator_token:
                grants_client = BatteriesClient(
                    settings.batteries_url, operator_token, timeout_s=settings.batteries_timeout_s
                )
                _own(stack, grants_client, grants_client.aclose)
            if usage_source is None and settings.batteries_url and token:
                source = BatteriesUsageSource(
                    settings.batteries_url, token, timeout_s=settings.batteries_timeout_s
                )
                usage_source = source
                _own(stack, source, source.aclose)

            if discovery_source is None:
                discovery_source = (
                    SignerDiscoverySource(
                        signer_url=settings.signer_url,
                        discovery_url=settings.discovery_url,
                        signer_headers={"Authorization": f"Bearer {shared_key}"} if shared_key else None,
                    )
                    if settings.signer_url or settings.discovery_url
                    else StaticDiscoverySource()
                )
            discovery = DiscoveryService(
                discovery_source,
                policy,
                clock,
                settings.discovery_interval_s,
            )
            payments = PaymentsService(
                mode=settings.allocation_mode,
                store=store,
                secrets=secret_store,
                policy=policy,
                clock=clock,
                engine_id=settings.engine_id,
                manager=allocation_manager,
                grants=grants_client if grants_client is not None else allocation_manager,
                currency=settings.default_currency,
                shared_secret=shared_ref,
                shared_allocation_id=settings.shared_allocation_id,
            )
            admission = AdmissionLimiter(settings.max_concurrent_jobs, settings.max_concurrent_jobs_per_app)
            jobs = JobService(
                store,
                discovery,
                transport or SdkRunnerTransport(settings.signer_url, settings.max_payment_challenge_retries),
                selection or MostCapacityFirst(),
                payments,
                policy,
                admission,
                clock,
                StreamLimits(
                    finalize_timeout_s=settings.stream_finalize_timeout_s,
                    drain_timeout_s=settings.stream_drain_timeout_s,
                    settle_s=settings.stream_settle_s,
                    open_grace_s=settings.stream_open_grace_s,
                ),
            )
            worker = CostSyncWorker(usage_source, store) if usage_source is not None else None
            costs = CostService(store, policy, clock, worker, payments, settings.cost_sync_interval_s)
            return cls(
                settings=settings,
                store=store,
                clock=clock,
                policy=policy,
                authenticator=AuthenticatorChain([*authenticators, ApiKeyAuthenticator(store)]),
                discovery=discovery,
                jobs=jobs,
                payments=payments,
                costs=costs,
                keys=KeyService(store, policy, clock),
                admission=admission,
                events=ReservedEventFeed(),
                sessions=ReservedSessions(),
                resources=stack.pop_all(),  # success: the engine now owns what was opened
            )

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
        _LOG.info("engine started", extra={"event": "engine.started", "version": package_version()})

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
        if not isinstance(self.store, SqlStore):
            return PurgeReport(jobs_deleted=0, usage_rows_deleted=0)
        return await purge(
            self.store.engine,
            self.clock.now(),
            self.settings.retention_jobs_days,
            self.settings.retention_usage_days,
        )

    async def schema_revision(self) -> str | None:
        return await current_revision(self.store.engine) if isinstance(self.store, SqlStore) else None

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
