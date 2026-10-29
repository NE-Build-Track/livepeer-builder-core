"""Providers layer: the extension protocols. Integrators swap an implementation here, never core source.

Imports only ``contracts`` and ``errors``. See ARCHITECTURE.md#providers.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from livepeer_builder.contracts import (
    AccessKey,
    ActorContext,
    Allowance,
    Attempt,
    AuthRequest,
    EventPage,
    Failure,
    GrantPage,
    GrantRecord,
    Job,
    JobCost,
    JobRequest,
    JobState,
    MeasuredUsage,
    Money,
    NewAllocationKey,
    Operation,
    OperationRecord,
    PaymentCredential,
    ProvisionedActor,
    PurgeReport,
    ResourceRef,
    ResultRef,
    Runner,
    RunnerReply,
    SecretRef,
    SeenActor,
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


class RunnerTransport(Protocol):
    """One runner call. Raises RunnerCallError, carrying ``payment_sent`` and ``manifest_ids``.

    A paying transport awaits ``on_signed(manifest_id)`` for each session the signer signs, before the
    paid request goes out (D42): the engine stores that evidence there, so a cancel or a crash during
    the paid request keeps the charge attributed to its job. The engine's recorder also has
    ``remember(manifest_id)``, which a transport awaits before asking the signer and signs nothing if it
    raises, and ``refused(manifest_id)`` for a refusal (D54; ``adapters/runner_call.py``).
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
    ``request.first_byte_timeout_s`` and raise ``RunnerCallError("timeout", ...)`` with the payment
    evidence so far; the engine stops an ``open`` that runs well past that and treats it as paid (D35).
    """

    async def open(
        self, runner: Runner, request: JobRequest, credential: str | None, on_signed: OnSigned | None = None
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

    async def grant_page(self, cursor: str | None) -> GrantPage:
        """One page of grants, oldest first, from ``cursor`` (``None`` for the first): a resumed grant create
        records the cursor after each page, so the next resume goes on from there (D57)."""
        ...

    async def create_grant(
        self, name: str, amount: Money, metadata: Mapping[str, str] | None = None
    ) -> GrantRecord:
        """Create a grant carrying ``metadata``, which ``list_grants`` returns (the engine's tag, D57)."""
        ...


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

    async def save_sighting(self, seen: SeenActor) -> None: ...  # keeps the first first_seen_at

    async def get_sighting(self, application_id: str, actor_id: str) -> SeenActor | None: ...

    async def list_sightings(self, application_id: str | None = None) -> list[SeenActor]: ...


class KeyRepo(Protocol):
    async def insert_access_key(
        self, key: AccessKey, token_hash: str, operation: OperationRecord | None = None
    ) -> bool:
        """Insert ``key``. With ``operation`` (the ``done`` ledger record of the command that issued it, D57),
        store both in one transaction; when that reference is already held, write nothing: ``False``."""
        ...

    async def get_operation(
        self, application_id: str, actor_id: str, operation_ref: str
    ) -> OperationRecord | None: ...  # the ledger record the key writes share (OperationRepo)

    async def access_key_by_hash(self, token_hash: str) -> AccessKey | None: ...

    async def revoke_access_key(self, key_id: str, revoked_at: datetime) -> bool: ...

    async def get_access_key(self, key_id: str) -> AccessKey | None: ...  # never the token hash

    async def rotate_access_key(
        self,
        old_key_id: str,
        new_key: AccessKey,
        new_token_hash: str,
        revoked_at: datetime,
        operation: OperationRecord | None = None,
    ) -> bool:
        """In one transaction: revoke ``old_key_id`` if it is active, store ``operation`` (its ledger record,
        D57) and insert ``new_key``. ``False``, and nothing written, when the old key is unknown or already
        revoked, or the operation's reference is held."""
        ...

    async def list_access_keys(self, application_id: str | None = None) -> list[AccessKey]: ...

    async def count_active_admin_keys(self) -> int: ...


class JobRepo(Protocol):
    async def create_job(self, job: Job, fingerprint: str | None, *, retry_of: UUID | None = None) -> None:
        """Insert a job. A reference already held raises OperationExists with the holder, or
        OperationConflict when the holder's fingerprint differs (B2).

        ``retry_of`` names a failed job holding the same reference that never reached a runner (D57). In one
        transaction, mark it superseded by this job only if it is still failed and not superseded (a
        compare-and-set), then insert this job. When another retry won, answer as above from the job
        now holding the reference. ``job_by_operation_ref`` returns only the job not superseded."""
        ...

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
        """Store the attempt's payment evidence while the call is still going (D42, D54): add sessions not
        stored yet, move a stored session's ``payment_state`` on unless it is ``paid`` (final), and mark
        the attempt paid when ``payment_sent`` is set, never unpaid. An ``unknown`` session stored before
        signing is not a payment. Leaves outcome, status, timing and the match columns alone, so a late
        report never reopens the attempt."""
        ...

    async def finish_attempt(self, attempt: Attempt) -> None:
        """Update the attempt after the call. May run more than once (a stream writes at open and end).

        Sessions are stored as in ``record_payment_evidence``: add the manifests not stored yet and move
        a stored session's ``payment_state`` on unless it is ``paid``. Never clear ``payment_sent``, and
        never write the match columns, which cost sync owns (D39, D54).
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


class OperationRepo(Protocol):
    """The operation ledger (D57, ISS-11): one record per caller scope and ``operation_ref``, stored under a
    unique constraint. Every write after ``begin_operation`` is fenced: it applies only while the record is
    ``started`` with the caller's ``fencing_token``, and returns ``False`` and writes nothing otherwise."""

    async def begin_operation(self, record: OperationRecord) -> OperationRecord | None:
        """Insert ``record`` (``started``). ``None`` when inserted; when the reference is held, write nothing
        and return the record holding it."""
        ...

    async def resume_operation(self, record: OperationRecord, started_at: datetime) -> OperationRecord | None:
        """Take over a stale run: a compare-and-set that adds 1 to the token and sets ``started_at``. The
        record now held, or ``None`` when another run moved it first."""
        ...

    async def remember_operation(self, record: OperationRecord) -> bool: ...  # store ``record.context``

    async def complete_operation(self, record: OperationRecord, result: Any, completed_at: datetime) -> bool:
        """``started`` -> ``done`` with the public result (JSON, never a token)."""
        ...

    async def abandon_operation(self, record: OperationRecord) -> bool: ...  # delete it: a definite refusal

    async def get_operation(
        self, application_id: str, actor_id: str, operation_ref: str
    ) -> OperationRecord | None: ...


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


class EngineStore(ActorRepo, KeyRepo, JobRepo, OperationRepo, CostRepo, SecretRepo, Protocol):
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
