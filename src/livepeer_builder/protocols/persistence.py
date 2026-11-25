"""The store's protocols: every engine-owned record, behind one interface (docs/contracts/persistence.md).

``EngineStore`` is what the engine is given; a service asks for the narrow repo it needs. The optional
store protocols, which the engine asks for with ``isinstance``, are in ``conditional_writes`` and
``clearinghouse``. All of them are re-exported from ``livepeer_builder.protocols``: they live here
because that module is at its size limit.
"""

from collections.abc import Collection, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from livepeer_builder.contracts import (
    AccessKey,
    ActorAllocation,
    ActorContext,
    Attempt,
    Failure,
    Job,
    JobCost,
    JobRequest,
    JobState,
    MeasuredUsage,
    OperationRecord,
    PurgeReport,
    ResultRef,
    SeenActor,
    SetAsideUsage,
    SyncCheckpoint,
    UsageRow,
)


class ActorRepo(Protocol):
    async def get_actor(self, tenant_id: str, actor_id: str) -> ActorAllocation | None: ...

    async def save_actor(self, actor: ActorAllocation) -> None: ...

    async def list_actors(self, tenant_id: str | None = None) -> list[ActorAllocation]: ...

    async def save_sighting(self, seen: SeenActor) -> None: ...  # keeps the first first_seen_at

    async def get_sighting(self, tenant_id: str, actor_id: str) -> SeenActor | None: ...

    async def list_sightings(self, tenant_id: str | None = None) -> list[SeenActor]: ...


class KeyRepo(Protocol):
    async def insert_access_key(
        self, key: AccessKey, token_hash: str, operation: OperationRecord | None = None
    ) -> bool:
        """Insert ``key``. With ``operation`` (the ``done`` ledger record of the command that issued it, D57),
        store both in one transaction; when that reference is already held, write nothing: ``False``."""
        ...

    async def get_operation(
        self, tenant_id: str, actor_id: str, operation_ref: str
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

    async def list_access_keys(self, tenant_id: str | None = None) -> list[AccessKey]: ...

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
        self, tenant_id: str, actor_id: str, operation_ref: str
    ) -> tuple[Job, str | None] | None: ...

    async def get_job(self, job_id: UUID) -> Job | None: ...

    async def list_jobs(
        self, tenant_id: str, actor_id: str | None, limit: int = 50, before: datetime | None = None
    ) -> list[Job]: ...

    async def start_attempt(self, attempt: Attempt, *, claim: int | None = None) -> bool:
        """Insert the attempt, before its call. ``claim`` is the number of the claim an async job runs
        under (ISS-26): the attempt is then written only while the job is ``running`` under that claim,
        in one transaction, and ``False`` says it was not. An immediate job and a stream name no claim,
        and their attempt is written as before. A store that answers nothing has written."""
        ...

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
        *,
        claim: int | None = None,
    ) -> bool:
        """End a ``running`` job, and only a running one (ISS-26): ``False``, and nothing written, for a
        job that already ended or is still queued, so a late writer never reopens a job. With ``claim``,
        an async job's, also only while that claim holds the job. The same transaction deletes the
        request stored for an async job."""
        ...

    async def set_usage(self, job_id: UUID, usage: MeasuredUsage) -> None: ...

    async def jobs_in_state(self, state: JobState) -> list[Job]: ...

    # -- the queue of async jobs (D69, ISS-26) --------------------------------------------------------

    async def enqueue_job(
        self,
        job: Job,
        fingerprint: str | None,
        request: JobRequest,
        actor: ActorContext,
        *,
        retry_of: UUID | None = None,
    ) -> None:
        """Insert a ``queued`` job with what it will run with, in one transaction: its request, and of the
        actor it was authorized for the id, the tenant, the permissions, the credential kind and the
        attributes (never ``auth`` or ``display_name``). A reference already held, and ``retry_of``, are
        answered as ``create_job`` answers them."""
        ...

    async def queued_jobs(self, limit: int, skip_apps: Collection[str] = ()) -> list[tuple[UUID, str]]:
        """The id and the runner app of ``queued`` jobs, oldest first, leaving out the apps named."""
        ...

    async def claim_job(
        self, job_id: UUID, started_at: datetime
    ) -> tuple[Job, JobRequest, ActorContext, int] | None:
        """Compare-and-set: a ``queued`` job becomes ``running``, with ``started_at`` and one more claim.
        The job, its request, its actor and the claim's number, which is the job's count of claims once
        this one is made; ``None`` when it is no longer queued (another claim, a cancel or the queue's
        time limit moved it first).

        The number is what the worker's later writes for the job carry (``claim=``): ``start_attempt``,
        ``requeue_unstarted``, ``finish_job`` and ``finish_job_with_result`` each apply only while the
        job is ``running`` with that count of claims, so a worker that lost the job writes nothing."""
        ...

    async def requeue_unstarted(
        self, job_id: UUID, max_claims: int, *, claim: int | None = None, counted: bool = True
    ) -> bool:
        """Put a ``running`` job back in the queue when nothing went out for it: it has no attempt row,
        its request is still stored, and it was claimed fewer than ``max_claims`` times. One statement.
        With ``claim``, only while that claim holds the job.

        ``counted=False`` gives the claim back: the job's count of claims goes down by one and
        ``max_claims`` is not looked at. It is for a job that could not be dispatched for a reason that
        passes (no runner listed, none with room), which may wait as often as it takes."""
        ...

    async def cancel_queued(self, job_id: UUID, failure: Failure, completed_at: datetime) -> bool:
        """One conditional write: a ``queued`` job ends ``failed`` with ``failure`` and its stored request
        is deleted. ``False``, and nothing written, when it is not queued."""
        ...

    async def expire_queued(self, created_before: datetime, failure: Failure, completed_at: datetime) -> int:
        """End, as ``cancel_queued`` does, every job still ``queued`` that was created before the time
        given. How many it ended."""
        ...

    async def finish_job_with_result(
        self,
        job_id: UUID,
        result: ResultRef,
        content: bytes,
        completed_at: datetime,
        *,
        claim: int | None = None,
    ) -> bool:
        """End a ``running`` job ``succeeded`` and keep its result, in one transaction: the state and the
        ``ResultRef``, the bytes (until ``result.expires_at``), and the deletion of the stored request.
        Nothing is written unless all of it is, so no job is ``succeeded`` without its bytes. With
        ``claim``, only while that claim holds the job: ``False``, and no bytes, otherwise."""
        ...

    async def read_result(self, job_id: UUID, now: datetime) -> bytes | None:
        """The stored bytes of a job's result; ``None`` when none is stored or it expired before ``now``."""
        ...

    async def queued_stats(self) -> tuple[int, datetime | None]:
        """How many jobs are ``queued``, and when the oldest of them was created."""
        ...


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
        self, tenant_id: str, actor_id: str, operation_ref: str
    ) -> OperationRecord | None: ...


@runtime_checkable
class UsageSetAsideRepo(Protocol):
    """Optional for a store: where cost sync records the usage rows it left out (#114, C13). The packaged
    SQL store has it. A supplied store without it still syncs: such a row is left out and logged once per
    process, and health reports no count."""

    async def set_aside_usage(self, rows: Sequence[SetAsideUsage]) -> list[SetAsideUsage]:
        """Record usage rows cost sync left out, once per id; returns the ones not recorded before."""
        ...

    async def count_set_aside_usage(self) -> int: ...


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
        self, tenant_id: str, actor_id: str | None, start: datetime, end: datetime
    ) -> tuple[list[tuple[str, JobCost]], list[Job]]:
        """The stored projections of jobs created in the window, each with its job's runner app, and the
        jobs no projection covers: running, or ended since their projection was stored."""
        ...

    async def recent_with_projections(
        self, tenant_id: str, actor_id: str | None, limit: int
    ) -> list[tuple[Job, JobCost | None]]: ...

    async def usage_for_payment_sessions(self, payment_session_ids: Sequence[str]) -> list[UsageRow]: ...

    async def unattributed_usage(self, allocation_id: str) -> list[UsageRow]: ...


@runtime_checkable
class AllocationCostSnapshot(CostSnapshot, Protocol):
    """Optional for a cost snapshot: which allocation paid each attempt (#115, C9). The packaged SQL store
    has it. Without it reconciliation counts every attempt of the actor against the current allocation,
    as before, lists no other allocation and knows no earlier one to reconcile."""

    async def attempt_allocations(self, tenant_id: str, actor_id: str | None) -> dict[tuple[UUID, int], str]:
        """(job id, attempt number) -> allocation, for each attempt of the actor's jobs (or, with no actor,
        the tenant's) that records one."""
        ...


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
