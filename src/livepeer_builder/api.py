"""What the engine offers: the consumer-facing surface, as Protocols split by role.

``protocols`` holds what the engine *needs* (ports it calls). This module holds what it *offers*
(what applications and the HTTP layer call). ``BuilderEngine`` satisfies these structurally, and so
does ``livepeer_builder.testing.FakeEngine`` for the actor API.

- ``ActorApi``: what an application calls on behalf of its actors (its users), plus the engine's
  lifecycle. Type against this when you only run jobs.
- ``AdminApi``: operator work (provisioning, grants, keys, reconciliation).
- ``EngineApi``: both, plus authentication and the M4 placeholders. ``http/`` is typed against it.

Optional capabilities are ``X | None`` members, decided once at construction: ``jobs.streaming``
(the transport can stream), ``keys`` (engine API keys are on), ``payments.grants`` (the grant desk is
on). Check for ``None`` before use; never probe with ``hasattr``. Members are read-only properties so
``EngineApi`` can narrow a member it combines.
"""

from collections.abc import AsyncGenerator, Iterable, Mapping, Sequence
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from livepeer_builder.contracts import (
    AccessKey,
    ActorContext,
    AllocationStatus,
    Allowance,
    CostSummary,
    CredentialKind,
    EngineHealth,
    FundRequest,
    GrantRecord,
    IssuedKey,
    Job,
    JobCost,
    JobRequest,
    JobResult,
    Money,
    Offering,
    ProvisionedActor,
    ProvisionRequest,
    Reconciliation,
    Runner,
    SyncReport,
)
from livepeer_builder.protocols import Authenticator, EventFeed, SessionManager

# -- the actor API -------------------------------------------------------------------------------


class JobStreamApi(Protocol):
    """One streamed job. Use it with ``async with`` and iterate the bytes.

    The job ends once. Reading to the end, or ``complete()`` once the caller has what it needs (D38),
    records it ``succeeded`` with a result describing the bytes read. ``aclose()``, or leaving the block
    early (``break``, ``return``, an error), records the attempt ``abandoned`` at once; the job is then
    ``uncertain`` if the call was paid. A cancellation leaving the block, and ``disconnected()``, give
    the runner ``LPB_STREAM_SETTLE_S`` to finish first, then succeed or abandon (D38, D40). Later
    endings do nothing.
    """

    @property
    def job(self) -> Job: ...
    @property
    def status_code(self) -> int: ...
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
        self, actor: ActorContext, start: datetime, end: datetime, actor_id: str | None = None
    ) -> CostSummary: ...


class DiscoveryApi(Protocol):
    def offerings(self, actor: ActorContext, *, app: str | None = None) -> list[Offering]: ...
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


# -- administration --------------------------------------------------------------------------------


class GrantsApi(Protocol):
    """The grant desk: present only with a grants manager (``LPB_BATTERIES_OPERATOR_TOKEN``)."""

    async def list_grants(self, admin: ActorContext) -> list[GrantRecord]: ...
    async def create_grant(self, admin: ActorContext, name: str, amount: Money) -> GrantRecord: ...


class PaymentsAdminApi(Protocol):
    async def provision(self, admin: ActorContext, request: ProvisionRequest) -> ProvisionedActor: ...
    async def fund(
        self, admin: ActorContext, application_id: str, actor_id: str, request: FundRequest
    ) -> ProvisionedActor: ...
    async def set_status(
        self, admin: ActorContext, application_id: str, actor_id: str, status: AllocationStatus
    ) -> ProvisionedActor: ...
    async def rotate_key(
        self, admin: ActorContext, application_id: str, actor_id: str, operation_ref: str
    ) -> ProvisionedActor: ...
    async def revoke(self, admin: ActorContext, application_id: str, actor_id: str) -> ProvisionedActor: ...
    async def fund_grant(self, admin: ActorContext, request: FundRequest) -> None: ...
    @property
    def grants(self) -> GrantsApi | None: ...


class KeysApi(Protocol):
    """Engine API keys: present only when they are on (``api_keys`` is not ``None``)."""

    async def issue(
        self,
        admin: ActorContext,
        *,
        kind: CredentialKind,
        actor_id: str,
        application_id: str = "default",
        permissions: Iterable[str] | None = None,
        label: str = "",
    ) -> IssuedKey: ...
    async def list_keys(self, admin: ActorContext, application_id: str | None = None) -> list[AccessKey]: ...
    async def revoke(self, admin: ActorContext, key_id: str) -> None: ...
    async def bootstrap_admin(self, label: str = "bootstrap") -> IssuedKey: ...


class CostsAdminApi(Protocol):
    async def recent(self, actor: ActorContext, limit: int = 25) -> list[JobCost]: ...
    async def reconcile(
        self, admin: ActorContext, application_id: str, actor_id: str | None
    ) -> Reconciliation: ...
    async def sync_now(self, admin: ActorContext) -> SyncReport: ...


class AdminApi(Protocol):
    """Operator work: provisioning and funding, the grant desk, keys, reconciliation."""

    @property
    def payments(self) -> PaymentsAdminApi: ...
    @property
    def keys(self) -> KeysApi | None: ...
    @property
    def costs(self) -> CostsAdminApi: ...


# -- the whole engine ------------------------------------------------------------------------------


class PaymentsApi(AllowanceApi, PaymentsAdminApi, Protocol):
    pass


class EngineCostsApi(CostsApi, CostsAdminApi, Protocol):
    pass


class EngineApi(ActorApi, AdminApi, Protocol):
    """The whole engine, as the HTTP layer uses it."""

    @property
    def payments(self) -> PaymentsApi: ...
    @property
    def costs(self) -> EngineCostsApi: ...
    @property
    def authenticator(self) -> Authenticator: ...
    @property
    def events(self) -> EventFeed: ...
    @property
    def sessions(self) -> SessionManager: ...


__all__ = [
    "ActorApi",
    "AdminApi",
    "AllowanceApi",
    "CostsAdminApi",
    "CostsApi",
    "DiscoveryApi",
    "EngineApi",
    "EngineCostsApi",
    "GrantsApi",
    "JobStreamApi",
    "JobsApi",
    "KeysApi",
    "PaymentsAdminApi",
    "PaymentsApi",
    "StreamingApi",
]
