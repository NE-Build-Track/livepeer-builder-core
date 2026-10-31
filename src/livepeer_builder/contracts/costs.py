"""Cost contracts: provider usage rows, per-job projections, classification and reconciliation.

Network cost is the expected value of tickets at signing (docs/contracts/payments.md). It is not
settlement and not a retail price. Missing evidence is ``pending``, never zero.
"""

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field

from livepeer_builder.contracts.common import Contract, Money

UsageStatus = Literal["applied", "quarantined", "ignored", "duplicate"]


class UsageRow(Contract):
    """One Batteries ``GET /v1/usage`` item. Batteries remains the authority; this is a cached copy."""

    id: str
    event_id: str | None
    status: UsageStatus
    manifest_id: str | None
    allocation_id: str | None
    payment_session_id: str | None  # Batteries session id: the cost key once the session log matches it
    request_id: str | None
    pipeline: str | None
    computed_fee_eth: Decimal | None
    computed_fee_usd: Decimal | None
    currency: str | None
    created_at: datetime


class UsagePage(Contract):
    items: tuple[UsageRow, ...]
    next_cursor: str = ""


class SyncCheckpoint(Contract):
    resume_cursor: str | None
    filters: dict[str, str] = Field(default_factory=dict)


class SyncReport(Contract):
    pages: int
    rows_seen: int
    rows_new: int
    jobs_updated: int = 0


CostClass = Literal["delivered", "failed_after_payment", "uncertain", "pending"]
CostStatus = Literal["none", "pending", "observed", "corrected"]


class Fees(Contract):
    fee_eth: Decimal = Decimal(0)
    fee_usd: Decimal | None = Decimal(0)  # None when any applied row lacks a USD price; zero when no rows
    row_count: int = 0


class AttemptCost(Contract):
    attempt_number: int
    cost_class: CostClass
    fees: Fees


class JobCost(Contract):
    """``fees`` is everything the job's attempts cost the allocation. ``billable`` is the part an
    enterprise bills its user for: the ``delivered`` attempts only, so a job that failed, or the
    attempts a job failed over from, are never billable (D52). Both are final once ``status`` is
    ``observed`` or ``corrected``."""

    job_id: UUID
    status: CostStatus
    fees: Fees
    attempts: tuple[AttemptCost, ...]
    first_observed_at: datetime | None
    updated_at: datetime | None
    billable: Fees = Field(default_factory=Fees)


def add_fees(parts: Iterable[Fees]) -> Fees:
    items = list(parts)
    eth = sum((fees.fee_eth for fees in items), Decimal(0))
    priced = [fees for fees in items if fees.row_count]
    usd: Decimal | None = Decimal(0) if not priced else None
    if priced and all(fees.fee_usd is not None for fees in priced):
        usd = sum((fees.fee_usd or Decimal(0) for fees in priced), Decimal(0))
    return Fees(fee_eth=eth, fee_usd=usd, row_count=sum(fees.row_count for fees in items))


def billable_fees(attempts: Iterable[AttemptCost]) -> Fees:
    """What the user is billed for: the ``delivered`` attempts' fees."""
    return add_fees(attempt.fees for attempt in attempts if attempt.cost_class == "delivered")


class CostSummary(Contract):
    start: datetime
    end: datetime
    by_class: dict[CostClass, Fees]
    jobs_with_cost: int
    jobs_pending: int  # reported beside the totals, never folded into them


class Reconciliation(Contract):
    """Batteries spent = delivered + failed_after_payment + uncertain + unattributed (per allocation)."""

    application_id: str
    actor_id: str | None
    allocation_id: str
    provider_spent: Money | None
    by_class: dict[CostClass, Fees]
    unattributed: Fees
    balanced: bool | None  # None when the provider figure is unavailable
