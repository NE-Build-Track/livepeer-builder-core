"""Costs: sync provider usage, classify each charge, persist per-job projections, reconcile allocations.

Classification (accepted 12 October, docs/contracts/payments.md#cost-classification):
  delivered             the attempt succeeded
  failed_after_payment  payment went out and the attempt failed (refused, rejected tickets, errors),
                        including an attempt the job failed over from (D52)
  uncertain             the attempt that left the job uncertain, or one that never reported back
  pending               payment went out and no applied usage row has arrived yet (never zero)
Rows on an allocation that match no recorded attempt are ``unattributed`` (reconciliation only).
``billable`` is the delivered fees only: what an enterprise bills its user for.
A job's cost is ``pending`` until the job ends, and only an ended job is projected (#33): only cost
sync stores projections, and reads compute live any job no projection covers yet (running, or ended
since its projection was stored), so a paid job never reads as ``none``.
A signed session whose usage has not arrived stays ``pending``, so the job is not stored
``observed`` with a zero bill.
Join key: (allocation_id, payment_session_id) after cost sync resolves each signed session's
manifest_id. A manifest that maps to more than one Batteries session, or to more than one log
entry, is ambiguous: once no signed session on the attempt is still waiting, the attempt is
``uncertain`` and those rows stay unattributed. Fees already matched on a sibling session stay
on the attempt. A debit whose manifest was never signed is unattributed on the allocation.
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
    add_fees,
    billable_fees,
)
from livepeer_builder.errors import CursorMismatch, NotFound, ProviderUnavailable
from livepeer_builder.protocols import (
    AccessPolicy,
    AllowanceReader,
    Clock,
    CostRepo,
    CostSnapshot,
    JobRepo,
    UsageSource,
)
from livepeer_builder.services.ownership import check_owner

_LOG = logging.getLogger(__name__)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_FAR_FUTURE = datetime(9999, 1, 1, tzinfo=UTC)
COST_CLASSES: tuple[CostClass, ...] = ("delivered", "failed_after_payment", "uncertain", "pending")
_ENDED: frozenset[str] = frozenset({"succeeded", "failed", "uncertain"})  # store.matching.ENDED


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
            found = await self._store.job_for_projection(job.id)  # reloaded with its evidence version
            if found is not None:
                await self._refresh_projection(*found)
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
        found = await self._store.job_for_projection(job_id)
        if found is None:
            return None
        return await self._refresh_projection(*found)

    async def _refresh_projection(self, job: Job, evidence_version: int) -> JobCost:
        computed = await self._compute(job)
        if job.state not in _ENDED:
            return computed  # it may still pay: never stored, computed on each read instead
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
        await self._store.save_projection(projection, evidence_version=evidence_version)
        return projection

    async def for_job(self, actor: ActorContext, job_id: UUID) -> JobCost:
        self._policy.check(actor, Operation.COSTS_READ)  # before any lookup: 403, then ownership 404 (D44)
        async with self._store.cost_snapshot() as reads:
            found = await reads.job_with_projection(job_id)
            if found is None:
                raise NotFound(f"job {job_id} not found")
            job, projection = found
            owner = _resource(job.application_id, job.actor_id)
            check_owner(self._policy, actor, Operation.COSTS_READ, owner, f"job {job_id} not found")
            if projection is not None and _covered(projection, job):
                return projection
            return (await _compute_all([job], reads))[0]  # running, or ended since sync stored it (#33)

    async def recent(self, actor: ActorContext, limit: int = 25) -> list[JobCost]:
        target = None if actor.credential_kind == "admin" else actor.actor_id
        self._policy.check(actor, Operation.COSTS_READ, _resource(actor.application_id, target))
        async with self._store.cost_snapshot() as reads:
            recent = await reads.recent_with_projections(actor.application_id, target, limit)
            due = [job for job, cost in recent if not _covered(cost, job)]
            computed = {cost.job_id: cost for cost in await _compute_all(due, reads)}
        return [
            cost if cost is not None and _covered(cost, job) else computed[job.id] for job, cost in recent
        ]

    async def summary(
        self, actor: ActorContext, start: datetime, end: datetime, actor_id: str | None = None
    ) -> CostSummary:
        target = actor_id or (None if actor.credential_kind == "admin" else actor.actor_id)
        self._policy.check(actor, Operation.COSTS_READ, _resource(actor.application_id, target))
        totals: dict[CostClass, list[Fees]] = {cls: [] for cls in COST_CLASSES}
        with_cost = pending = 0
        async with self._store.cost_snapshot() as reads:
            costs = await _rollup(reads, actor.application_id, target, start, end)
        for cost in costs:
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
        """provider spent == delivered + failed_after_payment + uncertain + pending + unattributed.

        The engine side (the classes and the unattributed rows) comes from one snapshot, so a sync running
        alongside can never count a row twice or nowhere. ``spent`` is read from the provider afterwards
        and can be ahead of usage the engine has not synced yet; ``balanced`` is then ``False`` until it is.
        """
        self._policy.check(admin, Operation.ADMIN_COSTS)
        if self._allowances is None:
            raise ProviderUnavailable("no payment provider is configured")
        allocation_id = await self._allowances.allocation_for(application_id, actor_id)
        if allocation_id is None:
            raise NotFound("no allocation pays for that actor or application")
        totals: dict[CostClass, list[Fees]] = {cls: [] for cls in COST_CLASSES}
        async with self._store.cost_snapshot() as reads:
            costs = await _rollup(reads, application_id, actor_id, _EPOCH, _FAR_FUTURE)
            unattributed = fees_of(await reads.unattributed_usage(allocation_id))
        for cost in costs:
            for attempt in cost.attempts:
                totals[attempt.cost_class].append(attempt.fees)
        by_class = {cls: add_fees(totals[cls]) for cls in COST_CLASSES}
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
        return (await _compute_all([job], self._store))[0]


class _UsageReads(Protocol):
    async def usage_for_payment_sessions(self, payment_session_ids: Sequence[str]) -> list[UsageRow]: ...


async def _rollup(
    reads: CostSnapshot, application_id: str, actor_id: str | None, start: datetime, end: datetime
) -> list[JobCost]:
    """One cost per job in the window: the stored projections, plus a live compute of the jobs none covers."""
    stored, not_projected = await reads.projections_and_unprojected(application_id, actor_id, start, end)
    costs = {cost.job_id: cost for cost in stored}
    due = [job for job in not_projected if not _covered(costs.get(job.id), job)]
    costs.update((cost.job_id, cost) for cost in await _compute_all(due, reads))
    return list(costs.values())


async def _compute_all(jobs: Sequence[Job], reads: _UsageReads) -> list[JobCost]:
    """Classify jobs from their matched usage rows, read once for all of them. Never stored."""
    keys = sorted({key for job in jobs for key in _matched_sessions(job)})
    rows = await reads.usage_for_payment_sessions(keys) if keys else []
    return [
        classify(job, [row for row in rows if row.payment_session_id in _matched_sessions(job)])
        for job in jobs
    ]


def _matched_sessions(job: Job) -> set[str]:
    return {
        session.payment_session_id
        for attempt in job.attempts
        for session in attempt.sessions
        if session.match == "matched" and session.payment_session_id
    }


def _covered(cost: JobCost | None, job: Job) -> bool:
    """A stored projection stands for the job only once the job ended and was projected after that."""
    if cost is None or job.state not in _ENDED or cost.updated_at is None:
        return False
    return job.completed_at is None or cost.updated_at >= job.completed_at


def classify(job: Job, rows: Sequence[UsageRow]) -> JobCost:
    """Pure classification of one job's charges. Only ``applied`` rows count as cost.

    A job that has not ended may still pay, so it is ``pending`` and so is each of its attempts, with the
    fees matched so far: never ``none``, never a final class. A signed session whose usage has not arrived
    stays ``pending`` too, including the attempt that left the job uncertain.
    """
    applied = [row for row in rows if row.status == "applied"]
    running = job.state not in _ENDED
    last = max((attempt.number for attempt in job.attempts), default=0)
    attempts: list[AttemptCost] = []
    for attempt in job.attempts:
        # A manifest the signer refused is no charge (D54); a stored ``unknown`` one may be, so it waits.
        sessions = tuple(session for session in attempt.sessions if session.payment_state != "unpaid")
        if not sessions and not attempt.payment_sent:
            continue
        matched_ids = {
            session.payment_session_id
            for session in sessions
            if session.match == "matched" and session.payment_session_id
        }
        mine = [row for row in applied if row.payment_session_id in matched_ids]
        last_word = attempt.outcome in ("in_flight", "payment_unknown") or (
            job.state == "uncertain" and attempt.number == last
        )
        if running:
            attempts.append(
                AttemptCost(attempt_number=attempt.number, cost_class="pending", fees=fees_of(mine))
            )
            continue
        arrived = {row.payment_session_id for row in mine}
        # Unmatched, or matched with no applied row yet: the charge is not in what we can see.
        # Ambiguous is not waiting: the usage arrived and the join was not unique.
        waiting = any(
            session.match == "unmatched"
            or (session.match == "matched" and session.payment_session_id not in arrived)
            for session in sessions
        )
        if waiting:
            attempts.append(
                AttemptCost(attempt_number=attempt.number, cost_class="pending", fees=fees_of(mine))
            )
            continue
        # Pending wins while a sibling is still waiting. Once usage has been seen, an ambiguous
        # signed session makes the attempt uncertain. Its rows stay unattributed; a sibling's
        # matched fee stays on the attempt.
        if any(session.match == "ambiguous" for session in sessions) and not waiting:
            attempts.append(
                AttemptCost(attempt_number=attempt.number, cost_class="uncertain", fees=fees_of(mine))
            )
            continue
        cost_class: CostClass
        if attempt.outcome == "succeeded":
            cost_class = "delivered"
        elif last_word:
            cost_class = "uncertain"  # only the last attempt can leave a job uncertain (D52)
        else:
            cost_class = "failed_after_payment"
        attempts.append(AttemptCost(attempt_number=attempt.number, cost_class=cost_class, fees=fees_of(mine)))
    status: CostStatus
    if running:
        status = "pending"
    elif not attempts:
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
        billable=billable_fees(attempts),
    )


def fees_of(rows: Iterable[UsageRow]) -> Fees:
    applied = [row for row in rows if row.status == "applied"]
    eth = sum((row.computed_fee_eth or Decimal(0) for row in applied), Decimal(0))
    # No applied rows means nothing was charged: a known zero. None means some charge lacks a USD price.
    usd: Decimal | None = None
    if all(row.computed_fee_usd is not None for row in applied):
        usd = sum((row.computed_fee_usd or Decimal(0) for row in applied), Decimal(0))
    return Fees(fee_eth=eth, fee_usd=usd, row_count=len(applied))


def _balanced(spent: Money | None, parts: list[Fees]) -> bool | None:
    if spent is None:
        return None
    total = add_fees(parts)
    if spent.currency == "ETH":
        return total.fee_eth == spent.amount
    return None if total.fee_usd is None else total.fee_usd == spent.amount


def _resource(application_id: str, owner: str | None) -> ResourceRef:
    return ResourceRef(kind="costs", application_id=application_id, owner_actor_id=owner)
