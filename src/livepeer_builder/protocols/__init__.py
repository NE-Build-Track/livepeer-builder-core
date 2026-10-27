"""Providers layer: the extension protocols. Integrators swap an implementation here, never core source.

Imports only ``contracts`` and ``errors``. See ARCHITECTURE.md#providers.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
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
    PaymentState,
    ProvisionedActor,
    PurgeReport,
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


OnSigned = Callable[[str], Awaitable[None]]
"""Called with each signed session's ``manifest_id`` (``""`` when the challenge has none)."""

OnPaymentState = Callable[[str, PaymentState], Awaitable[None]]
"""Persist unknown before signing, or an explicit unpaid refusal; never infer unpaid from silence."""


class RunnerTransport(Protocol):
    """One runner call. Raises RunnerCallError, carrying ``payment_sent`` and ``manifest_ids``.

    A paying transport awaits ``on_signed(manifest_id)`` for each session the signer signs, before the
    paid request goes out (D42): the engine stores that evidence there, so a cancel or a crash during
    the paid request keeps the charge attributed to its job.
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
    ``request.first_byte_timeout_s`` and raise ``RunnerCallError("timeout", ...)`` with the payment
    evidence so far; the engine stops an ``open`` that runs well past that and treats it as paid (D35).
    """

    async def open(
        self, runner: Runner, request: JobRequest, credential: str | None, on_signed: OnSigned | None = None
    ) -> RunnerStream: ...


@runtime_checkable
class PaymentIntentTransport(RunnerTransport, Protocol):
    """Optional transport capability: await durable intent before any signing request."""

    async def call_with_payment_intent(
        self,
        runner: Runner,
        request: JobRequest,
        credential: str | None,
        *,
        on_signed: OnSigned,
        on_payment_state: OnPaymentState,
    ) -> RunnerReply: ...


@runtime_checkable
class PaymentIntentStreamingTransport(StreamingRunnerTransport, Protocol):
    """Streaming counterpart of PaymentIntentTransport."""

    async def open_with_payment_intent(
        self,
        runner: Runner,
        request: JobRequest,
        credential: str | None,
        *,
        on_signed: OnSigned,
        on_payment_state: OnPaymentState,
    ) -> RunnerStream: ...


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

    async def get_access_key(self, key_id: str) -> AccessKey | None: ...  # never the token hash

    async def rotate_access_key(
        self, old_key_id: str, new_key: AccessKey, new_token_hash: str, revoked_at: datetime
    ) -> bool:
        """In one transaction: revoke ``old_key_id`` if it is active and insert ``new_key``. ``False``, and
        nothing written, when the old key is unknown or already revoked."""
        ...

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

    async def record_payment_evidence(self, attempt: Attempt) -> None:
        """Persist intent state and signed evidence without changing outcome, status or timing.

        Never regress paid evidence or rewrite cost-sync match columns. A pre-sign intent write
        must succeed before signing (D54); a late report never reopens an attempt (D42).
        """
        ...

    async def finish_attempt(self, attempt: Attempt) -> None:
        """Update the attempt after the call. May run more than once (a stream writes at open and end).

        Add new manifests and update unconfirmed intent state. Preserve paid evidence and match
        columns owned by cost sync (D39, D54), even when finishing with a stale attempt snapshot.
        """

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

    async def job_for_projection(self, job_id: UUID) -> tuple[Job, int] | None:
        """The job and its evidence version, the version read first (pass it to ``save_projection``)."""
        ...

    async def usage_for_payment_sessions(self, payment_session_ids: Sequence[str]) -> list[UsageRow]: ...

    async def unattributed_usage(self, allocation_id: str) -> list[UsageRow]: ...

    async def get_projection(self, job_id: UUID) -> JobCost | None: ...

    def cost_snapshot(self) -> AbstractAsyncContextManager["CostSnapshot"]:
        """Cost reads that see one consistent state, so a sync committing between two of a read's
        queries can never make it count a row twice or nowhere."""
        ...

    async def save_projection(self, cost: JobCost, *, evidence_version: int) -> None:
        """Store a cost computed from the job at ``evidence_version``; never over one from newer evidence."""
        ...


class CostSnapshot(Protocol):
    """The reads a cost query makes, all against one consistent state of the store."""

    async def job_with_projection(self, job_id: UUID) -> tuple[Job, JobCost | None] | None: ...

    async def projections_and_unprojected(
        self, application_id: str, actor_id: str | None, start: datetime, end: datetime
    ) -> tuple[list[JobCost], list[Job]]:
        """The stored projections of jobs created in the window, and the jobs no projection covers:
        running, or ended since their projection was stored."""
        ...

    async def recent_with_projections(
        self, application_id: str, actor_id: str | None, limit: int
    ) -> list[tuple[Job, JobCost | None]]: ...

    async def usage_for_payment_sessions(self, payment_session_ids: Sequence[str]) -> list[UsageRow]: ...

    async def unattributed_usage(self, allocation_id: str) -> list[UsageRow]: ...


class SecretRepo(Protocol):
    async def put_secret(self, ref: str, ciphertext: str, created_at: datetime) -> None: ...

    async def get_secret(self, ref: str) -> str | None: ...

    async def delete_secret(self, ref: str) -> None: ...


class StoreMaintenance(Protocol):
    """Housekeeping a store may offer: retention purges and its schema revision."""

    async def purge(self, now: datetime, jobs_days: int | None, usage_days: int | None) -> PurgeReport: ...

    async def schema_revision(self) -> str | None: ...


class EngineStore(ActorRepo, KeyRepo, JobRepo, CostRepo, SecretRepo, Protocol):
    """Every engine-owned record, behind one interface. SQL implementation: livepeer_builder.store."""

    @property
    def maintenance(self) -> StoreMaintenance | None:
        """``None`` when the store has no housekeeping: the engine then purges nothing and reports no
        schema revision."""
        ...

    async def close(self) -> None: ...


# -- reserved contracts (defined in M2, implemented later) -------------------------------------------


class EventFeed(Protocol):
    """Versioned events after a cursor (M3). An expired cursor raises an error mapped to 410."""

    async def after(self, actor: ActorContext, cursor: int, limit: int = 500) -> EventPage: ...


class SessionManager(Protocol):
    """Persistent runner sessions, proxied by the engine (M4, ISS-27)."""

    async def open(self, actor: ActorContext, request: SessionRequest) -> Session: ...

    async def close(self, actor: ActorContext, session_id: UUID) -> Session: ...
