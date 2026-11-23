"""What the engine offers: the consumer-facing surface, as Protocols.

``protocols`` holds what the engine *needs* (ports it calls). This module holds what it *offers*
(what applications and the HTTP layer call). ``BuilderEngine`` satisfies these structurally, and so
does ``livepeer_builder.testing.FakeEngine`` for the actor API.

The engine has two halves (D84), and the facade follows them:

- ``ActorApi``: what an application calls on behalf of its actors (its users), plus the engine's
  lifecycle. Type against this when you only run jobs.
- ``WorkApi``: the work half. The actor API, and what an admin of the engine does there: its own
  access keys, reconciliation and cost sync. The work routes are typed against it.
- ``ClearinghouseApi``: the clearinghouse half: tenants and their grants, allotment and top-ups, and
  a tenant's access keys. The routes under ``/v1/clearinghouse`` are typed against it.
- ``EngineApi``: both halves. ``create_app`` is typed against it.

Optional capabilities are ``X | None`` members, decided once at construction: ``jobs.streaming``
(the transport can stream), ``keys`` (engine API keys are on), ``payments.tenants`` (the tenant desk
is on), ``payments.usage`` (the usage source reads one tenant's records), ``payments.wallet`` (the grants
manager reports the escrow and the transport can ask the signer), ``runner_list`` (the discovery source
holds the document it read). Check for ``None`` before use; never probe with ``hasattr``. Members
are read-only properties so a wider protocol can narrow a member it combines.
"""

from collections.abc import AsyncGenerator, Iterable, Mapping, Sequence
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from livepeer_builder.contracts import (
    AccessKey,
    ActorAllocation,
    ActorContext,
    AllocationStatus,
    AllotRequest,
    Allowance,
    CostSummary,
    CredentialKind,
    DeliveredAllocation,
    EngineHealth,
    IssuedKey,
    Job,
    JobCost,
    JobRequest,
    JobResult,
    KnownActor,
    Money,
    Offering,
    OnboardedTenant,
    Reconciliation,
    Runner,
    SyncReport,
    TenantGrant,
    TenantState,
    TenantStatus,
    TopUpRequest,
    UsagePage,
    WalletReport,
)
from livepeer_builder.protocols import Authenticator, EventFeed, SessionManager

# -- the actor API -------------------------------------------------------------------------------


class JobStreamApi(Protocol):
    """One streamed job. Use it with ``async with`` and iterate the bytes.

    The job ends once. Reading to the end, or ``complete()`` once the caller has what it needs (D38),
    records it ``succeeded`` with a result describing the bytes read. ``aclose()``, or leaving the block
    early (``break``, ``return``, an error), records the attempt ``abandoned`` at once; the job is then
    ``uncertain`` if the call was paid. A cancellation leaving the block, and ``disconnected()``, give
    the runner ``LPB_STREAM_SETTLE_S`` to finish first: the stream then succeeds, fails with the
    runner's error, or (when the window runs out) is abandoned (D38, D40). Later endings do nothing.
    """

    @property
    def job(self) -> Job: ...
    @property
    def status_code(self) -> int: ...
    @property
    def headers(self) -> tuple[tuple[str, str], ...]: ...

    @property
    def content_type(self) -> str: ...
    @property
    def finished(self) -> bool: ...
    def __aiter__(self) -> AsyncGenerator[bytes, None]: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None: ...
    async def complete(self) -> None: ...
    async def aclose(self) -> None: ...
    async def disconnected(self) -> None: ...


class StreamingApi(Protocol):
    """Present only when the configured transport can stream (D33)."""

    async def stream(self, actor: ActorContext, request: JobRequest) -> JobStreamApi: ...


class JobsApi(Protocol):
    async def run(self, actor: ActorContext, request: JobRequest) -> JobResult: ...
    async def get(self, actor: ActorContext, job_id: UUID) -> Job: ...
    async def list_jobs(
        self, actor: ActorContext, *, limit: int = 50, before: datetime | None = None
    ) -> Sequence[Job]: ...
    async def result(self, actor: ActorContext, job_id: UUID) -> JobResult: ...
    async def report_usage(self, actor: ActorContext, job_id: UUID, quantities: Mapping[str, int]) -> Job: ...
    @property
    def streaming(self) -> StreamingApi | None: ...


class CostsApi(Protocol):
    async def for_job(self, actor: ActorContext, job_id: UUID) -> JobCost: ...
    async def summary(
        self,
        actor: ActorContext,
        start: datetime | None = None,
        end: datetime | None = None,
        actor_id: str | None = None,
    ) -> CostSummary: ...


class DiscoveryApi(Protocol):
    def offerings(self, *, app: str | None = None) -> list[Offering]: ...
    def runners(self, actor: ActorContext, app: str) -> list[Runner]: ...
    def candidates(self, app: str) -> list[Runner]: ...


class AllowanceApi(Protocol):
    async def allowance(self, actor: ActorContext, actor_id: str | None = None) -> Allowance: ...


class ActorApi(Protocol):
    """What an end-user application needs: run jobs, read their results and costs, see the catalog."""

    @property
    def jobs(self) -> JobsApi: ...
    @property
    def costs(self) -> CostsApi: ...
    @property
    def discovery(self) -> DiscoveryApi: ...
    @property
    def payments(self) -> AllowanceApi: ...
    def health(self) -> EngineHealth: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


# -- the work half ---------------------------------------------------------------------------------


class KeysApi(Protocol):
    """Engine API keys: present only when they are on (``api_keys`` is not ``None``). ``tenant_id`` on
    ``get``, ``rotate`` and ``revoke`` is the tenant a route named: a key of another tenant is then
    missing. Without it the key is found by its id alone, as the engine's own ``/v1/keys`` does."""

    async def issue(
        self,
        admin: ActorContext,
        *,
        kind: CredentialKind,
        actor_id: str,
        tenant_id: str = "default",
        permissions: Iterable[str] | None = None,
        label: str = "",
        operation_ref: str,
    ) -> IssuedKey: ...
    async def list_keys(self, admin: ActorContext, tenant_id: str | None = None) -> list[AccessKey]: ...
    async def get(self, admin: ActorContext, key_id: str, tenant_id: str | None = None) -> AccessKey: ...
    async def rotate(
        self,
        admin: ActorContext,
        key_id: str,
        *,
        label: str | None = None,
        operation_ref: str,
        tenant_id: str | None = None,
    ) -> IssuedKey: ...
    async def revoke(self, admin: ActorContext, key_id: str, tenant_id: str | None = None) -> None: ...
    async def bootstrap_admin(self, label: str = "bootstrap") -> IssuedKey: ...


class CostsAdminApi(Protocol):
    async def recent(self, actor: ActorContext, limit: int = 25) -> list[JobCost]: ...
    async def reconcile(
        self, admin: ActorContext, tenant_id: str, actor_id: str | None, allocation_id: str | None = None
    ) -> Reconciliation: ...
    async def sync_now(self, admin: ActorContext) -> SyncReport: ...


class EngineCostsApi(CostsApi, CostsAdminApi, Protocol):
    pass


class WorkApi(ActorApi, Protocol):
    """The work half: jobs, runners, streaming and the cost of each job, with the engine's own access
    keys, reconciliation and cost sync. Of payments it offers the allowance read alone."""

    @property
    def costs(self) -> EngineCostsApi: ...
    @property
    def keys(self) -> KeysApi | None: ...
    @property
    def authenticator(self) -> Authenticator: ...
    @property
    def events(self) -> EventFeed: ...
    @property
    def sessions(self) -> SessionManager: ...


# -- the clearinghouse half ------------------------------------------------------------------------


class TenantsApi(Protocol):
    """The tenant desk: present only with a grants manager (``LPB_BATTERIES_GRANTS_TOKEN``). A tenant's
    id is its grant's id (D83)."""

    async def onboard(
        self,
        admin: ActorContext,
        name: str,
        amount: Money,
        *,
        operation_ref: str,
        admin_actor_id: str = "tenant-admin",
    ) -> OnboardedTenant: ...
    async def list_tenants(self, admin: ActorContext) -> list[TenantGrant]: ...
    async def grant(self, admin: ActorContext, tenant_id: str) -> TenantGrant: ...
    async def set_status(
        self, admin: ActorContext, tenant_id: str, status: TenantStatus, *, operation_ref: str
    ) -> TenantState: ...


class TenantUsageApi(Protocol):
    """A tenant's payment records: present only with a usage source that reads one tenant's
    (``TenantUsageSource``, as the Batteries source does)."""

    async def page(
        self, reader: ActorContext, tenant_id: str, cursor: str | None = None, limit: int = 1000
    ) -> UsagePage: ...


class WalletApi(Protocol):
    """The wallet report: present only when the grants manager reports the escrow (``EscrowSource``) and
    the runner transport can ask the signer (``SignerProbe``), as the packaged ones do."""

    async def report(self, admin: ActorContext) -> WalletReport: ...


class PaymentsAdminApi(Protocol):
    async def allot_for_delivery(self, admin: ActorContext, request: AllotRequest) -> DeliveredAllocation: ...

    async def rotate_for_delivery(
        self, admin: ActorContext, tenant_id: str, actor_id: str, operation_ref: str
    ) -> DeliveredAllocation: ...

    async def allot(self, admin: ActorContext, request: AllotRequest) -> ActorAllocation: ...
    async def top_up(
        self, admin: ActorContext, tenant_id: str, actor_id: str, request: TopUpRequest
    ) -> ActorAllocation: ...
    async def set_status(
        self, admin: ActorContext, tenant_id: str, actor_id: str, status: AllocationStatus
    ) -> ActorAllocation: ...
    async def rotate_payment_key(
        self, admin: ActorContext, tenant_id: str, actor_id: str, operation_ref: str
    ) -> ActorAllocation: ...
    async def revoke(self, admin: ActorContext, tenant_id: str, actor_id: str) -> ActorAllocation: ...
    async def top_up_grant(self, admin: ActorContext, tenant_id: str, request: TopUpRequest) -> None: ...
    async def list_actors(
        self,
        admin: ActorContext,
        *,
        tenant_id: str | None = None,
        status: AllocationStatus | None = None,
        allotted: bool | None = None,
        limit: int = 500,
    ) -> list[KnownActor]: ...
    async def get_actor(self, admin: ActorContext, tenant_id: str, actor_id: str) -> ActorAllocation: ...
    @property
    def tenants(self) -> TenantsApi | None: ...
    @property
    def usage(self) -> TenantUsageApi | None: ...
    @property
    def wallet(self) -> WalletApi | None: ...


class RunnerListApi(Protocol):
    """The clearinghouse's runner list: present only when the discovery source holds the livepeer/node
    document it read (``DiscoveryDocumentSource``, as the HTTP reader does)."""

    def document(self, reader: ActorContext) -> bytes | None: ...


class ClearinghouseApi(Protocol):
    """The clearinghouse half: tenants and their grants, allotment and top-ups, a tenant's access keys
    and usage, and the runner list it publishes. It runs no job. Like the work half, it authenticates
    its callers, reports health and has the engine's lifecycle."""

    @property
    def payments(self) -> PaymentsAdminApi: ...
    @property
    def keys(self) -> KeysApi | None: ...
    @property
    def runner_list(self) -> RunnerListApi | None: ...
    @property
    def authenticator(self) -> Authenticator: ...
    def health(self) -> EngineHealth: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


# -- the whole engine ------------------------------------------------------------------------------


class PaymentsApi(AllowanceApi, PaymentsAdminApi, Protocol):
    pass


class EngineApi(WorkApi, ClearinghouseApi, Protocol):
    """The whole engine, both halves, as ``create_app`` uses it."""

    @property
    def payments(self) -> PaymentsApi: ...


__all__ = [
    "ActorApi",
    "AllowanceApi",
    "ClearinghouseApi",
    "CostsAdminApi",
    "CostsApi",
    "DiscoveryApi",
    "EngineApi",
    "EngineCostsApi",
    "JobStreamApi",
    "JobsApi",
    "KeysApi",
    "PaymentsAdminApi",
    "PaymentsApi",
    "RunnerListApi",
    "StreamingApi",
    "TenantUsageApi",
    "TenantsApi",
    "WalletApi",
    "WorkApi",
]
