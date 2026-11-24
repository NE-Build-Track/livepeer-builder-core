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
from typing import Final

from livepeer_builder.adapters.auth import ApiKeyAuthenticator, AuthenticatorChain
from livepeer_builder.adapters.clock import SystemClock
from livepeer_builder.config import DISCOVERY_URL_REQUIRED, EngineSettings
from livepeer_builder.contracts import Runner
from livepeer_builder.protocols import (
    AccessPolicy,
    AllocationManager,
    AllowanceReader,
    Authenticator,
    Clock,
    CredentialResolver,
    DiscoveryDocumentSource,
    DiscoverySource,
    EngineStore,
    EscrowSource,
    EventFeed,
    KeyRepo,
    RunnerTransport,
    SecretStore,
    SelectionPolicy,
    SessionManager,
    SignerProbe,
    TenantUsageSource,
    UsageSource,
)
from livepeer_builder.runtime.payment_defaults import payment_defaults
from livepeer_builder.services.admission import AdmissionLimiter
from livepeer_builder.services.costs import CostService, CostSyncWorker
from livepeer_builder.services.discovery import DiscoveryService, MostCapacityFirst
from livepeer_builder.services.jobs import JobService
from livepeer_builder.services.keys import KeyService
from livepeer_builder.services.ledger import OperationLedger
from livepeer_builder.services.payments import PaymentsService
from livepeer_builder.services.policy import DefaultPolicy
from livepeer_builder.services.reserved import ReservedEventFeed, ReservedSessions
from livepeer_builder.services.runner_list import RunnerList
from livepeer_builder.services.sightings import SightingRecorder
from livepeer_builder.services.streams import StreamLimits
from livepeer_builder.services.tenant_usage import TenantUsage
from livepeer_builder.services.tenants import TenantDesk
from livepeer_builder.services.wallet import Wallet

_LOG = logging.getLogger(__name__)


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
    # The two payment ports the engine answers from its own records unless it is given others (#168):
    # None is the engine's own, the payment key in its secret store and the allowance at its provider.
    credentials: CredentialResolver | None = None
    allowances: AllowanceReader | None = None


@dataclass(frozen=True)
class EngineSupply:
    """Parts the caller already has (Replace). Every field defaults to ``UNSET``; see ``EngineParts``."""

    store: EngineStore | _Unset = UNSET
    clock: Clock | _Unset = UNSET
    policy: AccessPolicy | _Unset = UNSET
    authenticators: tuple[Authenticator, ...] | _Unset = UNSET
    api_keys: ApiKeys | _Unset | None = UNSET
    transport: RunnerTransport | _Unset = UNSET
    selection: SelectionPolicy | _Unset = UNSET
    allocation_manager: AllocationManager | _Unset | None = UNSET
    grants_manager: AllocationManager | _Unset | None = UNSET
    usage_source: UsageSource | _Unset | None = UNSET
    secret_store: SecretStore | _Unset = UNSET
    events: EventFeed | _Unset = UNSET
    sessions: SessionManager | _Unset = UNSET
    credentials: CredentialResolver | _Unset | None = UNSET
    allowances: AllowanceReader | _Unset | None = UNSET


Decorate = Callable[[EngineParts], EngineParts]


class NoDiscoveryAddress:
    """The discovery source of an engine built without ``LPB_DISCOVERY_URL`` (#154): it has no address
    to read. ``lpb purge`` and ``lpb bootstrap-admin`` build such an engine and never start it; ``start()``
    refuses it before anything polls (D63). Asked anyway, it gives no answer, in the refusal's words."""

    async def fetch(self) -> tuple[Runner, ...]:  # NOSONAR - async by protocol (DiscoverySource)
        raise RuntimeError(DISCOVERY_URL_REQUIRED)


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

    The default adapters and the SQL store are imported where they are built, not at module level, so
    importing the package loads no database, HTTP, crypto or SDK library
    (tests/structure/test_import_weight.py).
    """
    supply = supply or EngineSupply()
    clock = _supplied(supply.clock, SystemClock)
    policy = _supplied(supply.policy, DefaultPolicy)
    store = await _sql_store(settings, stack) if isinstance(supply.store, _Unset) else supply.store
    api_keys = _supplied(supply.api_keys, lambda: _engine_keys(settings, store))
    authenticators = _supplied(supply.authenticators, lambda: _own_authenticators(settings, stack))
    secret_store = _supplied(supply.secret_store, lambda: _built_in_secret_store(settings, store, clock))

    # Each payment part the caller did not supply: Batteries and the signer, or a payment clearinghouse
    # (runtime/payment_defaults.py). Asked for one by one, so a supplied part is never built.
    defaults = payment_defaults(
        settings, lambda resource, close: own(stack, resource, close), store, secret_store
    )
    allocation_manager = _supplied(supply.allocation_manager, defaults.allocation_manager)
    grants_manager = _supplied(supply.grants_manager, defaults.grants_manager)
    usage_source = _supplied(supply.usage_source, defaults.usage_source)

    return EngineParts(
        store=store,
        clock=clock,
        policy=policy,
        authenticators=authenticators,
        api_keys=api_keys,
        discovery_source=_discovery_source(settings, stack),
        transport=_supplied(supply.transport, defaults.transport),
        selection=_supplied(supply.selection, MostCapacityFirst),
        allocation_manager=allocation_manager,
        grants_manager=grants_manager,
        usage_source=usage_source,
        secret_store=secret_store,
        events=_supplied(supply.events, ReservedEventFeed),
        sessions=_supplied(supply.sessions, ReservedSessions),
        credentials=_supplied(supply.credentials, defaults.credentials),
        allowances=_supplied(supply.allowances, defaults.allowances),
    )


def _discovery_source(settings: EngineSettings, stack: contextlib.AsyncExitStack) -> DiscoverySource:
    """Always the HTTP reader (D63). Callers wrap it through decorate; there is no other source. It
    carries the tenant admin key only when the address is the clearinghouse's runner list (D87). Without
    the address there is nothing to read: the engine is built, and ``start()`` refuses it (#154)."""
    from livepeer_builder.adapters.sdk import HttpDiscoverySource  # here: see default_parts

    if settings.discovery_url is None:
        return NoDiscoveryAddress()
    http_source = HttpDiscoverySource(settings.discovery_url, bearer=settings.clearinghouse_runner_list_token)
    own(stack, http_source, http_source.aclose)
    return http_source


def _supplied[T](part: T | _Unset, default: Callable[[], T]) -> T:
    """The part the caller supplied, or its default, which is built only when none was supplied."""
    return default() if isinstance(part, _Unset) else part


async def _sql_store(settings: EngineSettings, stack: contextlib.AsyncExitStack) -> EngineStore:
    from livepeer_builder.store import SqlStore

    sql = await SqlStore.connect(settings.database_url, migrate=settings.auto_migrate)
    own(stack, sql, sql.close)
    return sql


def _engine_keys(settings: EngineSettings, store: EngineStore) -> ApiKeys | None:
    return ApiKeys(store) if "api_keys" in settings.auth else None  # LPB_AUTH=jwt replaces keys


def _own_authenticators(
    settings: EngineSettings, stack: contextlib.AsyncExitStack
) -> tuple[Authenticator, ...]:
    """The deployment's own authenticators when it supplies none: the JWT bearer, with LPB_AUTH=jwt."""
    built: list[Authenticator] = []
    if "jwt" in settings.auth:
        from livepeer_builder.adapters.jwt import jwt_authenticator

        bearer = jwt_authenticator(settings)
        own(stack, bearer, bearer.aclose)
        built.append(bearer)
    return tuple(built)


def _built_in_secret_store(settings: EngineSettings, store: EngineStore, clock: Clock) -> SecretStore:
    from livepeer_builder.adapters.secrets import EncryptedDbSecretStore, RoutingSecretStore

    database = (
        EncryptedDbSecretStore(store, settings.secret_key.get_secret_value(), clock)
        if settings.secret_key
        else None
    )
    return RoutingSecretStore(database)


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
    runner_list: RunnerList | None  # None: the discovery source holds no document to serve (#166)
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
    authenticators: list[Authenticator] = list(parts.authenticators)
    keys: KeyService | None = None
    if parts.api_keys is not None:
        authenticators.append(ApiKeyAuthenticator(parts.api_keys.store, parts.api_keys.realm))
        keys = KeyService(parts.api_keys.store, parts.policy, parts.clock)
    tenants: TenantDesk | None = None
    if parts.grants_manager is not None:  # otherwise the tenant desk is off
        tenants = TenantDesk(
            parts.grants_manager, parts.policy, settings.default_currency, ledger, settings.engine_id, keys
        )
    runner_list: RunnerList | None = None
    if isinstance(parts.discovery_source, DiscoveryDocumentSource):  # otherwise no list is served (#166)
        runner_list = RunnerList(parts.discovery_source, parts.policy)
    usage: TenantUsage | None = None
    if isinstance(parts.usage_source, TenantUsageSource):  # otherwise no tenant's usage is served (#166)
        usage = TenantUsage(parts.usage_source, parts.policy)
    wallet: Wallet | None = None
    # otherwise no wallet report is served (#167): the grants manager reports the escrow and the grants,
    # and the transport asks the signer it pays through
    if isinstance(parts.grants_manager, EscrowSource) and isinstance(parts.transport, SignerProbe):
        wallet = Wallet(parts.grants_manager, parts.grants_manager, parts.transport, parts.policy)
    payments = PaymentsService(
        store=parts.store,
        secrets=parts.secret_store,
        policy=parts.policy,
        clock=parts.clock,
        engine_id=settings.engine_id,
        ledger=ledger,
        manager=parts.allocation_manager,
        tenants=tenants,
        usage=usage,
        wallet=wallet,
        currency=settings.default_currency,
        allowances=parts.allowances,
    )
    admission = AdmissionLimiter(settings.max_concurrent_jobs, settings.max_concurrent_jobs_per_app)
    jobs = JobService(
        parts.store,
        discovery,
        parts.transport,
        parts.selection,
        parts.credentials or payments,
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
        runner_list=runner_list,
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
        await _require_storable_payment_keys(parts)  # the final parts, after decoration
        resources = stack.pop_all()  # out of the stack: the caller takes them with the services
    try:
        return assemble(settings, parts), resources
    except BaseException:
        await resources.aclose()  # assemble failed: nothing owns them yet
        raise


async def _require_storable_payment_keys(parts: EngineParts) -> None:
    """Allotment would fund an allocation, then fail to keep its key (#112, C2). Judged on the parts the
    engine will run with, so a decorator that turns the provider off or installs its own store is not
    refused, and one that adds a provider is. Only the built-in store is asked here: a plug-in store that
    cannot write at boot (a sealed vault) still starts, and is asked per request."""
    from livepeer_builder.adapters.secrets import RoutingSecretStore

    if (
        parts.allocation_manager is not None
        and isinstance(parts.secret_store, RoutingSecretStore)
        and not await parts.secret_store.writable()
    ):
        raise ValueError(
            "LPB_SECRET_KEY is required with a payment provider: the built-in secret store needs it to keep "
            "each actor's payment key (lpb gen-secret-key), or supply a secret_store"
        )
