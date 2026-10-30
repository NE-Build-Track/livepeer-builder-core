"""Construction as data: the engine's replaceable parts, their defaults, and the wiring step.

``BuilderEngine.create`` runs three stages (docs/design-docs/extending-the-engine.md):

1. **Supply (Replace).** ``EngineSupply`` names parts the caller already has. ``default_parts``
   builds only what was not supplied, in dependency order, so a supplied part is never built and the
   defaults that depend on it (API keys and the encrypted secret store use the store) are built from
   the supplied one. Supplied parts are borrowed: the engine never closes them.
2. **Decorate.** A synchronous ``(EngineParts) -> EngineParts`` function wraps parts at the point of
   use. A decorated part is not seen by parts built before decoration; supply a part when other
   parts depend on it.
3. **Assemble.** ``assemble`` builds every service from the final parts. It does no I/O, so it cannot
   leave anything half-open, and a decorated policy reaches every service.

Parts are ports (behaviour). Tuning values stay in ``EngineSettings``.
"""

import contextlib
import enum
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from livepeer_builder.adapters.auth import ApiKeyAuthenticator, AuthenticatorChain
from livepeer_builder.adapters.clock import SystemClock
from livepeer_builder.adapters.static import StaticDiscoverySource
from livepeer_builder.config import EngineSettings
from livepeer_builder.contracts import SecretRef
from livepeer_builder.protocols import (
    AccessPolicy,
    AllocationManager,
    Authenticator,
    Clock,
    DiscoverySource,
    EngineStore,
    EventFeed,
    KeyRepo,
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
from livepeer_builder.services.ledger import OperationLedger
from livepeer_builder.services.payments import PaymentsService
from livepeer_builder.services.policy import DefaultPolicy
from livepeer_builder.services.reserved import ReservedEventFeed, ReservedSessions
from livepeer_builder.services.sightings import SightingRecorder
from livepeer_builder.services.streams import StreamLimits

# The one deliberate conditional import: the default adapters load inside default_parts, so that
# `import livepeer_builder` stays light (tests/structure/test_import_weight.py). This annotation-only
# import keeps cryptography out of the import too.
if TYPE_CHECKING:
    from livepeer_builder.adapters.secrets import SettingsSecretStore

_LOG = logging.getLogger(__name__)
_SHARED_KEY = "shared_payment_key"


class _Unset(enum.Enum):
    UNSET = "UNSET"

    def __repr__(self) -> str:
        return "UNSET"


UNSET: Final = _Unset.UNSET
"""Marks an ``EngineSupply`` field the caller did not supply. ``None`` is a value: it means "off"."""


@dataclass(frozen=True)
class ApiKeys:
    """Engine API keys (``Bearer lpk_...``): where they live and the realm to announce.

    ``assemble`` builds both the key authenticator and the key service from this one part, so
    accepting keys and issuing them cannot disagree. ``api_keys=None`` turns engine keys off.
    """

    store: KeyRepo
    realm: str = "livepeer-builder"


@dataclass(frozen=True)
class EngineParts:
    """Every replaceable port of the engine. Change one with ``dataclasses.replace``."""

    store: EngineStore
    clock: Clock
    policy: AccessPolicy
    authenticators: tuple[Authenticator, ...]  # the deployment's own, tried before API keys
    api_keys: ApiKeys | None  # None: engine API keys are off
    discovery_source: DiscoverySource
    transport: RunnerTransport
    selection: SelectionPolicy
    allocation_manager: AllocationManager | None  # None: Batteries is not configured
    grants_manager: AllocationManager | None  # None: the grant desk is off
    usage_source: UsageSource | None  # None: no cost sync
    secret_store: SecretStore
    events: EventFeed
    sessions: SessionManager


@dataclass(frozen=True)
class EngineSupply:
    """Parts the caller already has (Replace). Every field defaults to ``UNSET``; see ``EngineParts``."""

    store: EngineStore | _Unset = UNSET
    clock: Clock | _Unset = UNSET
    policy: AccessPolicy | _Unset = UNSET
    authenticators: tuple[Authenticator, ...] | _Unset = UNSET
    api_keys: ApiKeys | _Unset | None = UNSET
    discovery_source: DiscoverySource | _Unset = UNSET
    transport: RunnerTransport | _Unset = UNSET
    selection: SelectionPolicy | _Unset = UNSET
    allocation_manager: AllocationManager | _Unset | None = UNSET
    grants_manager: AllocationManager | _Unset | None = UNSET
    usage_source: UsageSource | _Unset | None = UNSET
    secret_store: SecretStore | _Unset = UNSET
    events: EventFeed | _Unset = UNSET
    sessions: SessionManager | _Unset = UNSET


Decorate = Callable[[EngineParts], EngineParts]


def own(stack: contextlib.AsyncExitStack, resource: object, close: Callable[[], Awaitable[None]]) -> None:
    """Register ``close`` on ``stack``; a failure is logged, never raised, so the rest still close."""

    async def close_logged() -> None:
        try:
            await close()
        except Exception:
            _LOG.exception(
                "close failed", extra={"event": "engine.close_failed", "resource": type(resource).__name__}
            )

    stack.push_async_callback(close_logged)


async def default_parts(
    settings: EngineSettings, stack: contextlib.AsyncExitStack, supply: EngineSupply | None = None
) -> EngineParts:
    """Build every part that was not supplied. Resources go on ``stack`` the moment they open.

    The default adapters and the SQL store are imported here, not at module level, so importing the
    package loads no database, HTTP, crypto or SDK library (tests/structure/test_import_weight.py).
    """
    from livepeer_builder.adapters.batteries import BatteriesClient, BatteriesUsageSource
    from livepeer_builder.adapters.sdk import HttpDiscoverySource, SdkRunnerTransport
    from livepeer_builder.adapters.secrets import EncryptedDbSecretStore, RoutingSecretStore
    from livepeer_builder.store import SqlStore

    supply = supply or EngineSupply()
    clock = supply.clock if not isinstance(supply.clock, _Unset) else SystemClock()
    policy = supply.policy if not isinstance(supply.policy, _Unset) else DefaultPolicy()

    store: EngineStore
    if isinstance(supply.store, _Unset):
        sql = await SqlStore.connect(settings.database_url, migrate=settings.auto_migrate)
        own(stack, sql, sql.close)
        store = sql
    else:
        store = supply.store

    if isinstance(supply.api_keys, _Unset):
        api_keys = ApiKeys(store) if "api_keys" in settings.auth else None  # LPB_AUTH=jwt replaces keys
    else:
        api_keys = supply.api_keys

    authenticators: tuple[Authenticator, ...]
    if not isinstance(supply.authenticators, _Unset):
        authenticators = supply.authenticators
    elif "jwt" in settings.auth:
        from livepeer_builder.adapters.jwt import jwt_authenticator

        bearer = jwt_authenticator(settings)
        own(stack, bearer, bearer.aclose)
        authenticators = (bearer,)
    else:
        authenticators = ()

    secret_store: SecretStore
    if isinstance(supply.secret_store, _Unset):
        database = (
            EncryptedDbSecretStore(store, settings.secret_key.get_secret_value(), clock)
            if settings.secret_key
            else None
        )
        secret_store = RoutingSecretStore(_configured_secrets(settings), database)
    else:
        secret_store = supply.secret_store

    token = settings.batteries_token.get_secret_value() if settings.batteries_token else None
    allocation_manager: AllocationManager | None
    if not isinstance(supply.allocation_manager, _Unset):
        allocation_manager = supply.allocation_manager
    elif settings.batteries_url and token:
        client = BatteriesClient(settings.batteries_url, token, timeout_s=settings.batteries_timeout_s)
        own(stack, client, client.aclose)
        allocation_manager = client
    else:
        allocation_manager = None

    operator = (
        settings.batteries_operator_token.get_secret_value() if settings.batteries_operator_token else None
    )
    grants_manager: AllocationManager | None
    if not isinstance(supply.grants_manager, _Unset):
        grants_manager = supply.grants_manager
    elif settings.batteries_url and operator:
        desk = BatteriesClient(settings.batteries_url, operator, timeout_s=settings.batteries_timeout_s)
        own(stack, desk, desk.aclose)
        grants_manager = desk
    else:
        grants_manager = None  # the grant desk is off; the engine token cannot list or create grants

    usage_source: UsageSource | None
    if not isinstance(supply.usage_source, _Unset):
        usage_source = supply.usage_source
    elif settings.batteries_url and token:
        usage = BatteriesUsageSource(settings.batteries_url, token, timeout_s=settings.batteries_timeout_s)
        own(stack, usage, usage.aclose)
        usage_source = usage
    else:
        usage_source = None

    discovery_source: DiscoverySource
    if not isinstance(supply.discovery_source, _Unset):
        discovery_source = supply.discovery_source
    elif settings.discovery_url:  # the signer only signs; it is not a discovery source (#45)
        discovery_source = HttpDiscoverySource(settings.discovery_url, max_bytes=settings.discovery_max_bytes)
    else:
        discovery_source = StaticDiscoverySource()

    return EngineParts(
        store=store,
        clock=clock,
        policy=policy,
        authenticators=authenticators,
        api_keys=api_keys,
        discovery_source=discovery_source,
        transport=(
            supply.transport
            if not isinstance(supply.transport, _Unset)
            else SdkRunnerTransport(settings.signer_url, settings.max_payment_challenge_retries)
        ),
        selection=supply.selection if not isinstance(supply.selection, _Unset) else MostCapacityFirst(),
        allocation_manager=allocation_manager,
        grants_manager=grants_manager,
        usage_source=usage_source,
        secret_store=secret_store,
        events=supply.events if not isinstance(supply.events, _Unset) else ReservedEventFeed(),
        sessions=supply.sessions if not isinstance(supply.sessions, _Unset) else ReservedSessions(),
    )


@dataclass(frozen=True)
class EngineServices:
    """Every service, built from the final parts by ``assemble``. ``BuilderEngine`` holds them."""

    store: EngineStore
    clock: Clock
    policy: AccessPolicy
    authenticator: Authenticator  # the chain, recording the people an identity provider signs in (D48)
    discovery: DiscoveryService
    jobs: JobService
    payments: PaymentsService
    costs: CostService
    keys: KeyService | None
    admission: AdmissionLimiter
    events: EventFeed
    sessions: SessionManager
    own_authenticators: int  # the deployment's, tried before API keys


def assemble(settings: EngineSettings, parts: EngineParts) -> EngineServices:
    """Build every service from the final parts. No I/O: it opens nothing, so it leaves nothing open."""
    discovery = DiscoveryService(
        parts.discovery_source,
        parts.policy,
        parts.clock,
        settings.discovery_interval_s,
        rate_max_age_s=settings.rate_max_age_s,
    )
    ledger = OperationLedger(parts.store, parts.clock, settings.operation_stale_s)  # one, for every command
    payments = PaymentsService(
        mode=settings.allocation_mode,
        store=parts.store,
        secrets=parts.secret_store,
        policy=parts.policy,
        clock=parts.clock,
        engine_id=settings.engine_id,
        ledger=ledger,
        manager=parts.allocation_manager,
        grants=parts.grants_manager,
        currency=settings.default_currency,
        shared_secret=_shared_secret_ref(settings),
        shared_allocation_id=settings.shared_allocation_id,
    )
    admission = AdmissionLimiter(settings.max_concurrent_jobs, settings.max_concurrent_jobs_per_app)
    jobs = JobService(
        parts.store,
        discovery,
        parts.transport,
        parts.selection,
        payments,
        parts.policy,
        admission,
        parts.clock,
        StreamLimits(
            finalize_timeout_s=settings.stream_finalize_timeout_s,
            drain_timeout_s=settings.stream_drain_timeout_s,
            settle_s=settings.stream_settle_s,
            open_grace_s=settings.stream_open_grace_s,
        ),
        max_paid_attempts=settings.max_paid_attempts,
    )
    worker = CostSyncWorker(parts.usage_source, parts.store) if parts.usage_source is not None else None
    costs = CostService(
        parts.store, parts.policy, parts.clock, worker, payments, settings.cost_sync_interval_s
    )
    authenticators: list[Authenticator] = list(parts.authenticators)
    keys: KeyService | None = None
    if parts.api_keys is not None:
        authenticators.append(ApiKeyAuthenticator(parts.api_keys.store, parts.api_keys.realm))
        keys = KeyService(parts.api_keys.store, parts.policy, parts.clock)
    return EngineServices(
        store=parts.store,
        clock=parts.clock,
        policy=parts.policy,
        authenticator=SightingRecorder(AuthenticatorChain(authenticators), parts.store, parts.clock),
        discovery=discovery,
        jobs=jobs,
        payments=payments,
        costs=costs,
        keys=keys,
        admission=admission,
        events=parts.events,
        sessions=parts.sessions,
        own_authenticators=len(parts.authenticators),
    )


async def build(
    settings: EngineSettings, *, supply: EngineSupply | None = None, decorate: Decorate | None = None
) -> tuple[EngineServices, contextlib.AsyncExitStack]:
    """Supply, then decorate, then assemble: the services, and the stack of what construction opened.

    ``BuilderEngine.create`` owns the stack from here. A failure part-way closes whatever was opened.
    """
    async with contextlib.AsyncExitStack() as stack:
        parts = await default_parts(settings, stack, supply)
        if decorate is not None:
            parts = decorate(parts)
        resources = stack.pop_all()  # out of the stack: the caller takes them with the services
    try:
        return assemble(settings, parts), resources
    except BaseException:
        await resources.aclose()  # assemble failed: nothing owns them yet
        raise


def _configured_secrets(settings: EngineSettings) -> "SettingsSecretStore":
    from livepeer_builder.adapters.secrets import SettingsSecretStore

    shared_key = settings.shared_payment_key.get_secret_value() if settings.shared_payment_key else None
    return SettingsSecretStore({_SHARED_KEY: shared_key} if shared_key else {})


def _shared_secret_ref(settings: EngineSettings) -> SecretRef | None:
    return _configured_secrets(settings).ref(_SHARED_KEY) if settings.shared_payment_key else None
