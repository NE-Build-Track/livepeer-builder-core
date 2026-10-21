"""BuilderEngine: wires settings, adapters and services into one object (App Wiring in ARCHITECTURE.md).

Imported mode: ``engine = await BuilderEngine.from_settings(settings)`` then call services with an
``ActorContext`` the application built. Service mode: livepeer_builder.http wraps this same object.
"""

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version

from livepeer_builder.adapters.auth import ApiKeyAuthenticator, AuthenticatorChain
from livepeer_builder.adapters.batteries import BatteriesClient, BatteriesUsageSource
from livepeer_builder.adapters.clock import SystemClock
from livepeer_builder.adapters.sdk import (
    OrchestratorListDiscoverySource,
    SdkRunnerTransport,
    SignerDiscoverySource,
)
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
_RETENTION_INTERVAL_S = 6 * 3600


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
        keys: KeyService,
        admission: AdmissionLimiter,
        events: EventFeed,
        sessions: SessionManager,
        closers: Sequence[object] = (),
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
        self._closers = tuple(closers)
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
        if settings.orchestrator_list and settings.discovery_url:
            raise InvalidRequest(
                "LPB_ORCHESTRATOR_LIST and LPB_DISCOVERY_URL are both set; configure one discovery source"
            )
        if settings.batteries_url and not (
            settings.batteries_operator_token and settings.batteries_operator_token.get_secret_value()
        ):
            raise InvalidRequest("LPB_BATTERIES_OPERATOR_TOKEN is required when LPB_BATTERIES_URL is set")
        clock = clock or SystemClock()
        policy = policy or DefaultPolicy()
        closers: list[object] = []
        if store is None:
            store = await SqlStore.connect(settings.database_url, migrate=settings.auto_migrate)
            closers.append(store)

        shared_key = settings.shared_payment_key.get_secret_value() if settings.shared_payment_key else None
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
            client = BatteriesClient(settings.batteries_url, token)
            allocation_manager = client
            closers.append(client)
        grants_client = None
        operator_token = None
        if settings.batteries_operator_token:
            operator_token = settings.batteries_operator_token.get_secret_value()
        if settings.batteries_url and operator_token:
            grants_client = BatteriesClient(settings.batteries_url, operator_token)
            closers.append(grants_client)
        if usage_source is None and settings.batteries_url and token:
            source = BatteriesUsageSource(settings.batteries_url, token)
            usage_source = source
            closers.append(source)

        if discovery_source is None and settings.orchestrator_list:
            # The engine owns the list; the signer only pays (D42).
            discovery_source = OrchestratorListDiscoverySource(settings.orchestrator_list)
        elif discovery_source is None:
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
            closers=closers,
        )

    async def start(self) -> None:
        """Resolve jobs a restart interrupted, take one discovery snapshot, then start background loops."""
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
        for closer in reversed(self._closers):
            close = getattr(closer, "aclose", None) or getattr(closer, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    _LOG.exception(
                        "close failed", extra={"event": "engine.close_failed", "closer": repr(closer)}
                    )
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
                await asyncio.wait_for(self._stop.wait(), timeout=_RETENTION_INTERVAL_S)
            except TimeoutError:
                continue
