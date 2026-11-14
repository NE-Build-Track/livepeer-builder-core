"""Cost contracts: provider usage rows, per-job projections, classification and reconciliation.

Network cost is the expected value of tickets at signing (docs/contracts/payments.md). It is not
settlement and not a retail price. Missing evidence is ``pending``, never zero.
"""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, cast
from uuid import UUID

from pydantic import Field

from livepeer_builder.contracts.common import Contract, Money, StoredText

UsageStatus = Literal["applied", "quarantined", "ignored", "duplicate"]

# Why cost sync set a usage row aside (#114, C13): a closed set, listed in docs/contracts/payments.md. An
# ``unstorable_<field>`` names a text column of ``lpb_usage_rows`` (``USAGE_ROW_LIMITS`` below;
# tests/store/test_usage_set_aside.py fails when the two disagree).
SetAsideReason = Literal[
    "missing_id",
    "unknown_status",
    "unreadable_created_at",
    "unreadable_fee",
    "unreadable_row",
    "unstorable_id",
    "unstorable_event_id",
    "unstorable_manifest_id",
    "unstorable_allocation_id",
    "unstorable_payment_session_id",
    "unstorable_request_id",
    "unstorable_pipeline",
    "unstorable_computed_fee_eth",
    "unstorable_computed_fee_usd",
    "unstorable_currency",
]


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


class SetAsideUsage(Contract):
    """A provider usage row the cost sync could not read or store, left out of every cost (#114, C13).

    Kept so reconciliation can name what is missing. ``raw`` is the row as received, for the log only: the
    store never holds it.
    """

    # the provider's row id; "sha256:" and a digest of the content when it has none the store can keep
    id: Annotated[StoredText, Field(min_length=1)]
    reason: SetAsideReason
    computed_fee_eth: Decimal | None = None  # as the provider stated it, whatever the row's status
    computed_fee_usd: Decimal | None = None
    raw: str = ""


SET_ASIDE_RAW_LIMIT = 2000  # how much of a set-aside row's content the log line keeps
# PostgreSQL takes no NUL in text; no database takes a lone surrogate, which is not Unicode text.
_UNSTORABLE = re.compile(r"[\x00\ud800-\udfff]")


def storable_text(value: str, limit: int = 200) -> bool:
    """Whether a text column of ``limit`` characters keeps ``value`` on SQLite and on PostgreSQL."""
    return len(value) <= limit and _UNSTORABLE.search(value) is None


# The longest text ``lpb_usage_rows`` keeps in each column (``store/schema.py``): ids are ``ID``, fees
# are decimal strings of ``AMOUNT``. tests/store/test_usage_set_aside.py fails when the two disagree.
USAGE_ROW_LIMITS: dict[str, int] = {
    "id": 200,
    "event_id": 200,
    "manifest_id": 200,
    "allocation_id": 200,
    "payment_session_id": 200,
    "request_id": 200,
    "pipeline": 200,
    "computed_fee_eth": 64,
    "computed_fee_usd": 64,
    "currency": 8,
}


def unstorable_field(row: UsageRow) -> str | None:
    """The first field of ``row`` the store cannot keep, or None when it keeps the row."""
    for name, limit in USAGE_ROW_LIMITS.items():
        value = getattr(row, name)
        if value is not None and not storable_text(str(value), limit):
            return name
    return None


def unstorable_reason(field: str) -> SetAsideReason:
    """The reason for a row whose ``field`` (one of ``USAGE_ROW_LIMITS``) the store cannot keep."""
    return cast(SetAsideReason, f"unstorable_{field}")


def set_aside_record(
    reason: SetAsideReason,
    content: Mapping[str, object],
    *,
    row_id: str | None,
    fee_eth: Decimal | None,
    fee_usd: Decimal | None,
) -> SetAsideUsage:
    """The record of one row left out. ``content`` is the row as received; it must be JSON data.

    The same content always gets the same id, so a page read again records nothing twice. A fee is kept
    when it is a finite number and the table can hold its digits."""
    raw = json.dumps(content, sort_keys=True, default=str)
    kept_id = row_id if row_id and storable_text(row_id) else None
    return SetAsideUsage(
        id=kept_id or "sha256:" + hashlib.sha256(raw.encode()).hexdigest(),
        reason=reason,
        computed_fee_eth=_storable_fee(fee_eth),
        computed_fee_usd=_storable_fee(fee_usd),
        raw=raw[:SET_ASIDE_RAW_LIMIT],
    )


def _storable_fee(fee: Decimal | None) -> Decimal | None:
    """The fee when it is finite and its digits fit the store's decimal-string column (64 characters);
    otherwise None. The record takes no ``NaN`` or ``Infinity``, so no fee stops a row being set aside."""
    return fee if fee is not None and fee.is_finite() and len(str(fee)) <= 64 else None


class UsagePage(Contract):
    items: tuple[UsageRow, ...]
    next_cursor: str = ""
    set_aside: tuple[SetAsideUsage, ...] = ()  # the page's items its source could not read as a UsageRow


class SyncCheckpoint(Contract):
    resume_cursor: str | None
    filters: dict[str, str] = Field(default_factory=dict)


class SyncReport(Contract):
    pages: int
    rows_seen: int  # every row the provider delivered, set aside or not
    rows_new: int
    jobs_updated: int = 0
    rows_set_aside: int = 0  # rows this sync left out and had not recorded before (#114, C13)


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
    """The costs of the jobs created in ``[start, end)``, in the caller's scope. ``by_app`` attributes
    the same attempt fees to each job's runner app, kept by class as in ``by_class`` (D60), so a final fee
    and a ``pending`` one that can still grow are never one figure. For every class, the apps add up to
    its ``by_class`` total. An app appears once one of its jobs has a costed attempt, with only the classes
    its attempts have."""

    start: datetime
    end: datetime
    by_class: dict[CostClass, Fees]
    jobs_with_cost: int
    jobs_pending: int  # reported beside the totals, never folded into them
    by_app: dict[str, dict[CostClass, Fees]] = Field(default_factory=dict)


class Reconciliation(Contract):
    """Batteries spent = delivered + failed_after_payment + uncertain + unattributed (per allocation)."""

    application_id: str
    actor_id: str | None
    allocation_id: str  # the one reconciled: only the attempts it paid are counted (#115, C9)
    # The actor's other allocations, each reconciled by naming it: the current one, and every earlier one
    # that paid an attempt.
    other_allocations: tuple[str, ...] = ()
    provider_spent: Money | None
    by_class: dict[CostClass, Fees]
    unattributed: Fees
    balanced: bool | None  # None when the provider figure is unavailable
