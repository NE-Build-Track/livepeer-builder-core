"""Providers layer: the extension protocols. Integrators swap an implementation here, never core source.

Imports only ``contracts`` and ``errors``. See ARCHITECTURE.md#providers. The store's protocols are in
``persistence`` and ``conditional_writes``, and what only the clearinghouse half needs in
``clearinghouse``; every one is re-exported here, and this is where they are
imported from.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from livepeer_builder.contracts import (
    ActorContext,
    Allowance,
    AuthRequest,
    EventPage,
    GrantPage,
    GrantRecord,
    JobRequest,
    Money,
    NewAllocationKey,
    Operation,
    PaymentKey,
    ResourceRef,
    Runner,
    RunnerReply,
    SecretRef,
    Session,
    SessionRequest,
    TenantGrant,
    UsagePage,
)
from livepeer_builder.protocols.clearinghouse import DiscoveryDocumentSource as DiscoveryDocumentSource
from livepeer_builder.protocols.clearinghouse import EscrowSource as EscrowSource
from livepeer_builder.protocols.clearinghouse import SignerProbe as SignerProbe
from livepeer_builder.protocols.clearinghouse import TenantUsageSource as TenantUsageSource
from livepeer_builder.protocols.conditional_writes import ActorCommandRepo as ActorCommandRepo
from livepeer_builder.protocols.conditional_writes import UsageOnceRepo as UsageOnceRepo
from livepeer_builder.protocols.persistence import ActorRepo as ActorRepo
from livepeer_builder.protocols.persistence import AllocationCostSnapshot as AllocationCostSnapshot
from livepeer_builder.protocols.persistence import CostRepo as CostRepo
from livepeer_builder.protocols.persistence import CostSnapshot as CostSnapshot
from livepeer_builder.protocols.persistence import EngineStore as EngineStore
from livepeer_builder.protocols.persistence import JobRepo as JobRepo
from livepeer_builder.protocols.persistence import KeyRepo as KeyRepo
from livepeer_builder.protocols.persistence import OperationRepo as OperationRepo
from livepeer_builder.protocols.persistence import SecretRepo as SecretRepo
from livepeer_builder.protocols.persistence import StoreMaintenance as StoreMaintenance
from livepeer_builder.protocols.persistence import UsageSetAsideRepo as UsageSetAsideRepo

# -- time -------------------------------------------------------------------------------------------


class Clock(Protocol):
    def now(self) -> datetime: ...  # timezone-aware UTC


# -- access (docs/contracts/access.md) --------------------------------------------------------------


@runtime_checkable
class Authenticator(Protocol):
    """Turns a request into a validated actor.

    Return ``None`` for "not my credential" (the chain tries the next authenticator). Return an
    ActorContext for a valid credential. Raise NotAuthenticated for "my credential, but invalid":
    the chain stops with 401 and never falls through.
    """

    async def authenticate(self, request: AuthRequest) -> ActorContext | None: ...

    def challenge(self) -> str | None: ...  # a WWW-Authenticate value, or None


class AccessPolicy(Protocol):
    """Raises AccessDenied when ``actor`` may not perform ``operation`` on ``resource``."""

    def check(
        self, actor: ActorContext, operation: Operation, resource: ResourceRef | None = None
    ) -> None: ...


class SecretStore(Protocol):
    """Holds secrets (payment keys). Records, events and logs only ever hold the SecretRef."""

    async def put(self, name: str, secret: str) -> SecretRef: ...

    async def get(self, ref: SecretRef) -> str: ...

    async def delete(self, ref: SecretRef) -> None: ...


@runtime_checkable
class WritableSecretStore(SecretStore, Protocol):
    """A store that can say whether ``put`` would work now. Optional: allotment and key rotation ask it
    before the provider is called, and a ``False`` refuses the command with nothing sent (#112, C2). A
    store that leaves it out is not asked."""

    async def writable(self) -> bool: ...


# -- discovery and execution ------------------------------------------------------------------------


class DiscoverySource(Protocol):
    async def fetch(self) -> tuple[Runner, ...]: ...


class SelectionPolicy(Protocol):
    def order(self, candidates: Sequence[Runner], request: JobRequest) -> Sequence[Runner]: ...


OnSigned = Callable[[str], Awaitable[None]]
"""Called with each signed session's ``manifest_id`` (``""`` when the challenge has none)."""


class RunnerTransport(Protocol):
    """One runner call. Raises RunnerCallError, carrying ``payment_sent`` and ``manifest_ids``.

    A paying transport awaits ``on_signed(manifest_id)`` for each session the signer signs, before the
    paid request goes out (D42): the engine stores that evidence there, so a cancel or a crash during
    the paid request keeps the charge attributed to its job. The engine's recorder also has
    ``remember(manifest_id)``, which a transport awaits before asking the signer and signs nothing if it
    raises, and ``refused(manifest_id)`` for a refusal (D54; ``adapters/runner_call.py``).

    A transport may also have ``pays: bool``, false when a credential never pays a runner (no signer). The
    engine then keeps the selection policy's order on every job; a transport without it is taken to pay.
    """

    async def call(
        self, runner: Runner, request: JobRequest, credential: str | None, on_signed: OnSigned | None = None
    ) -> RunnerReply: ...


class RunnerStream(Protocol):
    """A runner's response as it arrives (docs/contracts/engine.md#streaming).

    ``payment_sent`` and ``manifest_ids`` are known once ``open`` returned, because the signer pays the
    402 challenge before any byte flows. Iteration raises RunnerCallError when the runner fails
    mid-stream. ``aclose`` releases the connection and stops any ongoing funding.
    """

    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> tuple[tuple[str, str], ...]: ...

    @property
    def content_type(self) -> str: ...

    @property
    def manifest_ids(self) -> tuple[str, ...]: ...

    @property
    def payment_sent(self) -> bool: ...

    def __aiter__(self) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class StreamingRunnerTransport(RunnerTransport, Protocol):
    """A transport that can also hand the response back unbuffered. ``jobs.streaming`` exists only with one.

    ``open`` returns once the runner answered with its status and headers. It must give up after
    ``request.first_byte_timeout_s``. A deadline during signer identity, before the runner is asked, is
    ``payment`` / ``signer_unavailable`` (nothing was signed, D58 (4)); any later one is
    ``RunnerCallError("timeout", ...)`` with the payment evidence so far. The engine stops an ``open``
    that runs well past that and treats it as paid (D35).
    """

    async def open(
        self, runner: Runner, request: JobRequest, credential: str | None, on_signed: OnSigned | None = None
    ) -> RunnerStream: ...


# -- payments (docs/contracts/payments.md) ----------------------------------------------------------


class CredentialResolver(Protocol):
    """The signer credential for this actor, resolved at call time. Raises NotProvisioned."""

    async def credential(self, actor: ActorContext) -> PaymentKey: ...


class AllocationManager(Protocol):
    """The provider's management API (Batteries). Every write that moves money takes an idempotency key."""

    async def create_key_for_grant(
        self, grant_id: str, name: str, amount: Money, idempotency_key: str
    ) -> NewAllocationKey: ...

    async def create_key(self, allocation_id: str, name: str, idempotency_key: str) -> NewAllocationKey: ...

    async def top_up_allocation(self, allocation_id: str, amount: Money, idempotency_key: str) -> None: ...

    async def top_up_grant(self, grant_id: str, amount: Money, idempotency_key: str) -> None: ...

    async def set_allocation_status(self, allocation_id: str, status: str) -> None: ...

    async def revoke_allocation(self, allocation_id: str) -> None: ...

    async def revoke_key(self, api_key_id: str) -> None: ...

    async def read_allocation(self, allocation_id: str) -> Allowance: ...

    async def list_grants(self) -> list[GrantRecord]: ...

    async def grant_page(self, cursor: str | None) -> GrantPage:
        """One page of grants, oldest first, from ``cursor`` (``None`` for the first): a resumed grant create
        records the cursor after each page, so the next resume goes on from there (D57)."""
        ...

    async def create_grant(
        self, name: str, amount: Money, metadata: Mapping[str, str] | None = None
    ) -> GrantRecord:
        """Create a grant carrying ``metadata``, which ``list_grants`` returns (the engine's tag, D57)."""
        ...

    async def read_grant(self, grant_id: str) -> TenantGrant: ...  # total, unallocated balance, status

    async def grant_balances(self) -> list[TenantGrant]: ...  # every grant, oldest first

    async def set_grant_status(self, grant_id: str, status: str) -> None: ...  # active or paused


@runtime_checkable
class AllocationsByActor(Protocol):
    """Optional for an allocation manager: the manager for one actor's allocation (#168).

    ``AllocationManager`` names an allocation by its id, as Batteries does. A payment clearinghouse's
    routes name the tenant and the actor instead, and take no allocation id. A manager that needs the
    actor has this: the engine, which knows the actor of every allocation command, calls ``for_actor``
    and sends that command to the manager it returns. The members that name a grant are called on the
    manager itself. ``ClearinghouseAllocations`` has it; ``BatteriesClient`` does not, and is called as
    before. Without it nothing changes for a supplied manager."""

    def for_actor(self, tenant_id: str, actor_id: str) -> AllocationManager: ...


class UsageSource(Protocol):
    """Pages the provider's usage rows (Batteries ``GET /v1/usage``). Raises CursorMismatch."""

    async def page(self, cursor: str | None, limit: int = 1000) -> UsagePage: ...


class AllowanceReader(Protocol):
    """Finds the allocation that pays for an actor and reads its allowance."""

    async def allocation_for(self, tenant_id: str, actor_id: str) -> str | None: ...

    async def read_allowance(self, allocation_id: str) -> Allowance: ...


# -- reserved contracts (defined in M2, implemented later) -------------------------------------------


class EventFeed(Protocol):
    """Versioned events after a cursor (M3). An expired cursor raises an error mapped to 410."""

    async def after(self, actor: ActorContext, cursor: int, limit: int = 500) -> EventPage: ...


class SessionManager(Protocol):
    """Persistent runner sessions, proxied by the engine (M4, ISS-27)."""

    async def open(self, actor: ActorContext, request: SessionRequest) -> Session: ...

    async def close(self, actor: ActorContext, session_id: UUID) -> Session: ...
