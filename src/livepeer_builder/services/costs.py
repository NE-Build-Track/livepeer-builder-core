"""Costs: sync provider usage, classify each charge, persist per-job projections, reconcile allocations.

Classification (accepted 12 October, docs/contracts/payments.md#cost-classification):
  delivered             the attempt succeeded
  failed_after_payment  payment went out and the attempt failed (refused, rejected tickets, errors)
  uncertain             the job is uncertain, or the attempt never reported back
  pending               payment went out and no applied usage row has arrived yet (never zero)
Rows on an allocation that match no recorded attempt are ``unattributed`` (reconciliation only).
Join key: (allocation_id, payment_session_id) after cost sync resolves each signed session's
manifest_id. A manifest that maps to more than one Batteries session, or to more than one log
entry, is ambiguous: the attempt is uncertain and those rows stay unattributed.
"""

import asyncio
import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from livepeer_builder.contracts import (
    ActorContext,
    AttemptCost,
    CostClass,
    CostStatus,
    CostSummary,
    Fees,
    Job,
    JobCost,
    Money,
    Operation,
    Reconciliation,
    ResourceRef,
    SyncCheckpoint,
    SyncReport,
    UsageRow,
)
from livepeer_builder.errors import AccessDenied, CursorMismatch, NotFound, ProviderUnavailable
from livepeer_builder.protocols import AccessPolicy, AllowanceReader, Clock, CostRepo, JobRepo, UsageSource

_LOG = logging.getLogger(__name__)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_FAR_FUTURE = datetime(9999, 1, 1, tzinfo=UTC)
COST_CLASSES: tuple[CostClass, ...] = ("delivered", "failed_after_payment", "uncertain", "pending")


class CostStore(JobRepo, CostRepo, Protocol):
    """What the cost service needs from persistence."""


class CostSyncWorker:
    """Copies Batteries usage rows after a durable cursor. Re-reads the last page when the cursor ends."""

    def __init__(
        self, source: UsageSource, store: CostRepo, filters: Mapping[str, str] | None = None
    ) -> None:
        self._source = source
        self._store = store
        self._filters = dict(filters or {})

    async def sync_once(self) -> tuple[SyncReport, list[UsageRow]]:
        checkpoint = await self._store.cost_checkpoint()
        cursor = checkpoint.resume_cursor if checkpoint and checkpoint.filters == self._filters else None
        pages = rows_seen = 0
        new_rows: list[UsageRow] = []
        restarted = False
        while True:
            try:
                page = await self._source.page(cursor, limit=1000)
            except CursorMismatch:
                if restarted:
                    raise
                restarted, cursor = True, None
                await self._store.apply_usage((), SyncCheckpoint(resume_cursor=None, filters=self._filters))
                continue
            resume = page.next_cursor or cursor  # an empty cursor means "re-read this page next time"
            new_rows += await self._store.apply_usage(
                page.items, SyncCheckpoint(resume_cursor=resume, filters=self._filters)
            )
            pages += 1
            rows_seen += len(page.items)
            if not page.next_cursor:
                return SyncReport(pages=pages, rows_seen=rows_seen, rows_new=len(new_rows)), new_rows
            cursor = page.next_cursor


class CostService:
    def __init__(
        self,
        store: CostStore,
        policy: AccessPolicy,
        clock: Clock,
        worker: CostSyncWorker | None = None,
        allowances: AllowanceReader | None = None,
        interval_s: float = 30.0,
    ) -> None:
        self._store = store
        self._policy = policy
        self._clock = clock
        self._worker = worker
        self._allowances = allowances
        self._interval_s = interval_s
        self.last_synced_at: datetime | None = None
        self.last_error: str | None = None

    async def sync_once(self) -> SyncReport:
        if self._worker is None:
            raise ProviderUnavailable("no usage source is configured")
        report, new_rows = await self._worker.sync_once()
        affected = await self._store.match_sessions(new_rows)
        jobs = await self._store.jobs_for_projection(list(affected))
        for job in jobs:
            await self._refresh_projection(job)
        self.last_synced_at, self.last_error = self._clock.now(), None
        return report.model_copy(update={"jobs_updated": len(jobs)})

    async def sync_now(self, admin: ActorContext) -> SyncReport:
        """Operator-triggered sync (the background loop calls sync_once directly)."""
        self._policy.check(admin, Operation.ADMIN_COSTS)
        return await self.sync_once()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if self._worker is not None:
                try:
                    await self.sync_once()
                except Exception as exc:  # keep syncing; surface the error in health
                    self.last_error = str(exc)
                    _LOG.warning("cost sync failed", extra={"event": "costs.sync_failed", "error": str(exc)})
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue

    async def refresh_projection(self, job_id: UUID) -> JobCost | None:
        job = await self._store.get_job(job_id)
        if job is None:
            return None
        return await self._refresh_projection(job)

    async def _refresh_projection(self, job: Job) -> JobCost:
        computed = await self._compute(job)
        previous = await self._store.get_projection(job.id)
        status: CostStatus = computed.status
        if previous is not None and previous.status in ("observed", "corrected") and status == "observed":
            status = (
                "corrected"
                if previous.fees != computed.fees or previous.status == "corrected"
                else "observed"
            )
        first = previous.first_observed_at if previous and previous.first_observed_at else None
        if first is None and status in ("observed", "corrected"):
            first = self._clock.now()
        projection = computed.model_copy(
            update={"status": status, "first_observed_at": first, "updated_at": self._clock.now()}
        )
        await self._store.save_projection(projection)
        return projection

    async def for_job(self, actor: ActorContext, job_id: UUID) -> JobCost:
        job = await self._store.get_job(job_id)
        if job is None:
            raise NotFound(f"job {job_id} not found")
        try:
            self._policy.check(actor, Operation.COSTS_READ, _resource(job.application_id, job.actor_id))
        except AccessDenied as exc:
            raise NotFound(f"job {job_id} not found") from exc
        projection = await self._store.get_projection(job_id)
        return projection if projection is not None else classify(job, ())

    async def recent(self, actor: ActorContext, limit: int = 25) -> list[JobCost]:
        target = None if actor.credential_kind == "admin" else actor.actor_id
        self._policy.check(actor, Operation.COSTS_READ, _resource(actor.application_id, target))
        return await self._store.recent_projections(actor.application_id, target, limit)

    async def summary(
        self, actor: ActorContext, start: datetime, end: datetime, actor_id: str | None = None
    ) -> CostSummary:
        target = actor_id or (None if actor.credential_kind == "admin" else actor.actor_id)
        self._policy.check(actor, Operation.COSTS_READ, _resource(actor.application_id, target))
        totals: dict[CostClass, list[Fees]] = {cls: [] for cls in COST_CLASSES}
        with_cost = pending = 0
        for cost in await self._store.projections_between(actor.application_id, target, start, end):
            with_cost += cost.status in ("observed", "corrected")
            pending += cost.status == "pending"
            for attempt in cost.attempts:
                totals[attempt.cost_class].append(attempt.fees)
        return CostSummary(
            start=start,
            end=end,
            by_class={cls: add_fees(totals[cls]) for cls in COST_CLASSES},
            jobs_with_cost=with_cost,
            jobs_pending=pending,
        )

    async def reconcile(
        self, admin: ActorContext, application_id: str, actor_id: str | None
    ) -> Reconciliation:
        """provider spent == delivered + failed_after_payment + uncertain + unattributed."""
        self._policy.check(admin, Operation.ADMIN_COSTS)
        if self._allowances is None:
            raise ProviderUnavailable("no payment provider is configured")
        allocation_id = await self._allowances.allocation_for(application_id, actor_id)
        if allocation_id is None:
            raise NotFound("no allocation pays for that actor or application")
        totals: dict[CostClass, list[Fees]] = {cls: [] for cls in COST_CLASSES}
        for cost in await self._store.projections_between(application_id, actor_id, _EPOCH, _FAR_FUTURE):
            for attempt in cost.attempts:
                totals[attempt.cost_class].append(attempt.fees)
        by_class = {cls: add_fees(totals[cls]) for cls in COST_CLASSES}
        unattributed = fees_of(await self._store.unattributed_usage(allocation_id))
        spent: Money | None = None
        try:
            spent = (await self._allowances.read_allowance(allocation_id)).spent
        except ProviderUnavailable:
            spent = None
        return Reconciliation(
            application_id=application_id,
            actor_id=actor_id,
            allocation_id=allocation_id,
            provider_spent=spent,
            by_class=by_class,
            unattributed=unattributed,
            balanced=_balanced(spent, [*by_class.values(), unattributed]),
        )

    async def _compute(self, job: Job) -> JobCost:
        session_ids = sorted(
            {
                session.payment_session_id
                for attempt in job.attempts
                for session in attempt.sessions
                if session.match == "matched" and session.payment_session_id
            }
        )
        rows = await self._store.usage_for_payment_sessions(session_ids) if session_ids else []
        return classify(job, rows)


def classify(job: Job, rows: Sequence[UsageRow]) -> JobCost:
    """Pure classification of one job's charges. Only ``applied`` rows count as cost."""
    applied = [row for row in rows if row.status == "applied"]
    attempts: list[AttemptCost] = []
    for attempt in job.attempts:
        if not attempt.sessions and not attempt.payment_sent:
            continue
        matched = {
            session.payment_session_id
            for session in attempt.sessions
            if session.match == "matched" and session.payment_session_id
        }
        mine = [row for row in applied if row.payment_session_id in matched]
        cost_class: CostClass
        if any(session.match == "ambiguous" for session in attempt.sessions):
            cost_class = "uncertain"
        elif not mine:
            cost_class = "pending"
        elif attempt.outcome == "succeeded":
            cost_class = "delivered"
        elif job.state == "uncertain" or attempt.outcome == "in_flight":
            cost_class = "uncertain"
        else:
            cost_class = "failed_after_payment"
        attempts.append(AttemptCost(attempt_number=attempt.number, cost_class=cost_class, fees=fees_of(mine)))
    status: CostStatus
    if not attempts:
        status = "none"
    elif any(attempt.cost_class == "pending" for attempt in attempts):
        status = "pending"
    else:
        status = "observed"
    return JobCost(
        job_id=job.id,
        status=status,
        fees=add_fees(attempt.fees for attempt in attempts),
        attempts=tuple(attempts),
        first_observed_at=None,
        updated_at=None,
    )


def fees_of(rows: Iterable[UsageRow]) -> Fees:
    applied = [row for row in rows if row.status == "applied"]
    eth = sum((row.computed_fee_eth or Decimal(0) for row in applied), Decimal(0))
    # No applied rows means nothing was charged: a known zero. None means some charge lacks a USD price.
    usd: Decimal | None = None
    if all(row.computed_fee_usd is not None for row in applied):
        usd = sum((row.computed_fee_usd or Decimal(0) for row in applied), Decimal(0))
    return Fees(fee_eth=eth, fee_usd=usd, row_count=len(applied))


def add_fees(parts: Iterable[Fees]) -> Fees:
    items = list(parts)
    eth = sum((fees.fee_eth for fees in items), Decimal(0))
    priced = [fees for fees in items if fees.row_count]
    usd: Decimal | None = Decimal(0) if not priced else None
    if priced and all(fees.fee_usd is not None for fees in priced):
        usd = sum((fees.fee_usd or Decimal(0) for fees in priced), Decimal(0))
    return Fees(fee_eth=eth, fee_usd=usd, row_count=sum(fees.row_count for fees in items))


def _balanced(spent: Money | None, parts: list[Fees]) -> bool | None:
    if spent is None:
        return None
    total = add_fees(parts)
    if spent.currency == "ETH":
        return total.fee_eth == spent.amount
    return None if total.fee_usd is None else total.fee_usd == spent.amount


def _resource(application_id: str, owner: str | None) -> ResourceRef:
    return ResourceRef(kind="costs", application_id=application_id, owner_actor_id=owner)
