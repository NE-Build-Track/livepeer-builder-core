"""Providers layer: the extension protocols. Integrators swap an implementation here, never core source.

Imports only ``contracts`` and ``errors``. See ARCHITECTURE.md#providers.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from livepeer_builder.contracts import (
    AccessKey,
    ActorContext,
    Allowance,
    Attempt,
    AuthRequest,
    EventPage,
    Failure,
    GrantRecord,
    Job,
    JobCost,
    JobRequest,
    JobState,
    MeasuredUsage,
    Money,
    NewAllocationKey,
    Operation,
    PaymentCredential,
    ProvisionedActor,
    ResourceRef,
    ResultRef,
    Runner,
    RunnerReply,
    SecretRef,
    Session,
    SessionRequest,
    SyncCheckpoint,
    UsagePage,
    UsageRow,
)

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


# -- discovery and execution ------------------------------------------------------------------------


class DiscoverySource(Protocol):
    async def fetch(self) -> tuple[Runner, ...]: ...


class SelectionPolicy(Protocol):
    def order(self, candidates: Sequence[Runner], request: JobRequest) -> Sequence[Runner]: ...


class RunnerTransport(Protocol):
    """One runner call. Raises RunnerCallError, carrying ``payment_sent`` and ``manifest_ids``."""

    async def call(self, runner: Runner, request: JobRequest, credential: str | None) -> RunnerReply: ...


# -- payments (docs/contracts/payments.md) ----------------------------------------------------------


class CredentialResolver(Protocol):
    """The signer credential for this actor, resolved at call time. Raises NotProvisioned."""

    async def credential(self, actor: ActorContext) -> PaymentCredential: ...


class AllocationManager(Protocol):
    """The provider's management API (Batteries). Every write that moves money takes an idempotency key."""

    async def create_key_for_grant(
        self, grant_id: str, name: str, amount: Money, idempotency_key: str
    ) -> NewAllocationKey: ...

    async def create_key(self, allocation_id: str, name: str, idempotency_key: str) -> NewAllocationKey: ...

    async def fund_allocation(self, allocation_id: str, amount: Money, idempotency_key: str) -> None: ...

    async def fund_grant(self, grant_id: str, amount: Money, idempotency_key: str) -> None: ...

    async def set_allocation_status(self, allocation_id: str, status: str) -> None: ...

    async def revoke_allocation(self, allocation_id: str) -> None: ...

    async def revoke_key(self, api_key_id: str) -> None: ...

    async def read_allocation(self, allocation_id: str) -> Allowance: ...

    async def list_grants(self) -> list[GrantRecord]: ...

    async def create_grant(self, name: str, amount: Money) -> GrantRecord: ...


class UsageSource(Protocol):
    """Pages the provider's usage rows (Batteries ``GET /v1/usage``). Raises CursorMismatch."""

    async def page(self, cursor: str | None, limit: int = 1000) -> UsagePage: ...


class AllowanceReader(Protocol):
    """Finds the allocation that pays for an actor (or the application) and reads its allowance."""

    async def allocation_for(self, application_id: str, actor_id: str | None) -> str | None: ...

    async def read_allowance(self, allocation_id: str) -> Allowance: ...


# -- persistence (docs/contracts/persistence.md) ----------------------------------------------------


class ActorRepo(Protocol):
    async def get_actor(self, application_id: str, actor_id: str) -> ProvisionedActor | None: ...

    async def save_actor(self, actor: ProvisionedActor) -> None: ...

    async def list_actors(self, application_id: str | None = None) -> list[ProvisionedActor]: ...


class KeyRepo(Protocol):
    async def insert_access_key(self, key: AccessKey, token_hash: str) -> None: ...

    async def access_key_by_hash(self, token_hash: str) -> AccessKey | None: ...

    async def revoke_access_key(self, key_id: str, revoked_at: datetime) -> bool: ...

    async def list_access_keys(self, application_id: str | None = None) -> list[AccessKey]: ...

    async def count_active_admin_keys(self) -> int: ...


class JobRepo(Protocol):
    async def create_job(self, job: Job, fingerprint: str | None) -> None: ...  # raises OperationExists

    async def job_by_operation_ref(
        self, application_id: str, actor_id: str, operation_ref: str
    ) -> tuple[Job, str | None] | None: ...

    async def get_job(self, job_id: UUID) -> Job | None: ...

    async def list_jobs(
        self, application_id: str, actor_id: str | None, limit: int = 50, before: datetime | None = None
    ) -> list[Job]: ...

    async def jobs_between(
        self, application_id: str, actor_id: str | None, start: datetime, end: datetime
    ) -> list[Job]: ...

    async def start_attempt(self, attempt: Attempt) -> None: ...  # written before the call

    async def finish_attempt(self, attempt: Attempt) -> None: ...  # updated after the call

    async def finish_job(
        self,
        job_id: UUID,
        state: JobState,
        failure: Failure | None,
        result: ResultRef | None,
        completed_at: datetime,
    ) -> None: ...

    async def set_usage(self, job_id: UUID, usage: MeasuredUsage) -> None: ...

    async def jobs_in_state(self, state: JobState) -> list[Job]: ...


class CostRepo(Protocol):
    async def cost_checkpoint(self) -> SyncCheckpoint | None: ...

    async def apply_usage(self, rows: Sequence[UsageRow], checkpoint: SyncCheckpoint) -> list[UsageRow]: ...

    async def match_sessions(self, rows: Sequence[UsageRow]) -> set[UUID]: ...

    async def jobs_for_projection(self, job_ids: Sequence[UUID]) -> list[Job]: ...

    async def usage_for_payment_sessions(self, payment_session_ids: Sequence[str]) -> list[UsageRow]: ...

    async def unattributed_usage(self, allocation_id: str) -> list[UsageRow]: ...

    async def get_projection(self, job_id: UUID) -> JobCost | None: ...

    async def projections_between(
        self, application_id: str, actor_id: str | None, start: datetime, end: datetime
    ) -> list[JobCost]: ...

    async def recent_projections(
        self, application_id: str, actor_id: str | None, limit: int
    ) -> list[JobCost]: ...

    async def save_projection(self, cost: JobCost) -> None: ...


class SecretRepo(Protocol):
    async def put_secret(self, ref: str, ciphertext: str, created_at: datetime) -> None: ...

    async def get_secret(self, ref: str) -> str | None: ...

    async def delete_secret(self, ref: str) -> None: ...


class EngineStore(ActorRepo, KeyRepo, JobRepo, CostRepo, SecretRepo, Protocol):
    """Every engine-owned record, behind one interface. SQL implementation: livepeer_builder.store."""

    async def close(self) -> None: ...


# -- reserved contracts (defined in M2, implemented later) -------------------------------------------


class EventFeed(Protocol):
    """Versioned events after a cursor (M3). An expired cursor raises an error mapped to 410."""

    async def after(self, actor: ActorContext, cursor: int, limit: int = 500) -> EventPage: ...


class SessionManager(Protocol):
    """Persistent runner sessions, proxied by the engine (M4, ISS-27)."""

    async def open(self, actor: ActorContext, request: SessionRequest) -> Session: ...

    async def close(self, actor: ActorContext, session_id: UUID) -> Session: ...
