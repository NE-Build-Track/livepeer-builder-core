"""Cost contracts: provider usage rows, per-job projections, classification and reconciliation.

Network cost is the expected value of tickets at signing (docs/contracts/payments.md). It is not
settlement and not a retail price. Missing evidence is ``pending``, never zero.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
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


class CostClass(StrEnum):
    """What one attempt's charge was for (docs/contracts/payments.md#cost-classification)."""

    DELIVERED = "delivered"  # the attempt succeeded
    FAILED_AFTER_PAYMENT = "failed_after_payment"  # payment went out and the attempt failed
    UNCERTAIN = "uncertain"  # the job is uncertain, or the attempt never reported back
    PENDING = "pending"  # payment went out and no applied usage row has arrived yet (never zero)


class CostStatus(StrEnum):
    """A job's cost as a whole (docs/contracts/payments.md#job-cost-status)."""

    NONE = "none"  # the job ended succeeded or failed, and nothing was paid
    PENDING = "pending"  # not ended, ended uncertain without evidence, or a paid attempt lacks rows
    OBSERVED = "observed"
    CORRECTED = "corrected"  # the total changed after it was first observed

    @property
    def has_cost(self) -> bool:
        """Every paid attempt has rows, so the total is known."""
        return self in (CostStatus.OBSERVED, CostStatus.CORRECTED)


class Fees(Contract):
    fee_eth: Decimal = Decimal(0)
    fee_usd: Decimal | None = Decimal(0)  # None when any applied row lacks a USD price; zero when no rows
    row_count: int = 0


class AttemptCost(Contract):
    attempt_number: int
    cost_class: CostClass
    fees: Fees


class JobCost(Contract):
    job_id: UUID
    status: CostStatus
    fees: Fees
    attempts: tuple[AttemptCost, ...]
    first_observed_at: datetime | None
    updated_at: datetime | None


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
