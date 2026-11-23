"""A Batteries usage item, read as a usage row or set aside (#114, C13).

Split from ``batteries.py``, which holds the HTTP calls: this module holds no client, only what one item
of ``GET /v1/usage`` means. Baseline: livepeer/clearinghouse-batteries v0.0.3
(docs/references/batteries-api.md).
"""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from livepeer_builder.contracts import SetAsideReason, SetAsideUsage, UsageRow
from livepeer_builder.contracts.costs import set_aside_record, unstorable_field, unstorable_reason
from livepeer_builder.errors import ProviderError

_USAGE_STATUSES = frozenset({"applied", "quarantined", "ignored", "duplicate"})
_NO_FEE = ("", "[error]")  # what Batteries serves, with null, for a fee it has not or could not convert
# A usage item's text fields: Batteries serves each as a string or null
_USAGE_TEXT = (
    "event_id", "manifest_id", "allocation_id", "payment_session_id", "request_id", "pipeline", "currency"
)  # fmt: skip


def read_usage_item(item: dict[str, Any]) -> UsageRow | SetAsideUsage:
    """The item as a usage row, or the record that sets it aside when it cannot be read or stored (#114,
    C13): one such item no longer fails its page, which the sync would read again on every pass. The
    record's content is the item as received."""
    reason = _unreadable(item)
    if reason is None:
        try:
            row = usage_row_from_item(item)
        except ValidationError:  # the backstop: whatever the checks miss, the row never fails its page
            reason = "unreadable_row"
        else:
            field = unstorable_field(row)
            if field is None:
                return row
            reason = unstorable_reason(field)
    return set_aside_record(
        reason,
        item,
        row_id=_row_id(item),
        fee_eth=decimal_or_none(item.get("computed_fee_eth")),
        fee_usd=decimal_or_none(item.get("computed_fee_usd")),
    )


def _unreadable(item: dict[str, Any]) -> SetAsideReason | None:
    """Why the item is no usage row, as a short code; None when it is one. Each value is judged as it was
    received, so the value checked is the value stored: nothing is turned into text first."""
    status = item.get("status")
    if _row_id(item) is None:
        return "missing_id"
    if not isinstance(status, str) or status not in _USAGE_STATUSES:
        return "unknown_status"
    if _created_at(item) is None:
        return "unreadable_created_at"
    for name in _USAGE_TEXT:
        if not isinstance(item.get(name), str | None):
            return unstorable_reason(name)  # no text: the column would keep its Python form
    fees = (item.get("computed_fee_eth"), item.get("computed_fee_usd"))
    return "unreadable_fee" if any(_unreadable_fee(fee) for fee in fees) else None


def _unreadable_fee(value: Any) -> bool:
    """Whether a fee is stated and is no finite decimal: ``NaN``, ``Infinity``, or no number at all. Such
    a fee is never read as no fee, which an applied row would then cost."""
    if value is None or value in _NO_FEE:
        return False
    amount = decimal_or_none(value)
    return amount is None or not amount.is_finite()


def _row_id(item: dict[str, Any]) -> str | None:
    """The row's id as text: a non-empty string, or a whole number as its digits."""
    value = item.get("id")
    usable = isinstance(value, str | int) and not isinstance(value, bool) and value != ""
    return str(value) if usable else None


def _created_at(item: dict[str, Any]) -> datetime | None:
    """``created_at_ms`` as a time, or None when it is absent, not a JSON number (a string of digits and
    ``true`` are not), or past what a clock holds."""
    value = item.get("created_at_ms")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def usage_row_from_item(item: dict[str, Any]) -> UsageRow:
    status = item.get("status")
    if status not in _USAGE_STATUSES:
        raise ProviderError(200, f"unknown usage status {status!r}")
    return UsageRow(
        id=str(item["id"]),
        event_id=_text(item.get("event_id")),
        status=status,
        manifest_id=_text(item.get("manifest_id")),
        allocation_id=_text(item.get("allocation_id")),
        payment_session_id=_text(item.get("payment_session_id")),
        request_id=_text(item.get("request_id")),
        pipeline=_text(item.get("pipeline")),
        computed_fee_eth=decimal_or_none(item.get("computed_fee_eth")),
        computed_fee_usd=decimal_or_none(item.get("computed_fee_usd")),
        currency=_text(item.get("currency")),
        created_at=_created_at(item),
    )


def _text(value: str | None) -> str | None:
    """A usage item's text field: Batteries serves ``""`` where it has none."""
    return value or None


def decimal_or_none(value: Any) -> Decimal | None:
    """A provider amount as a ``Decimal``; ``None`` for none stated, or for one that is no number."""
    if value is None or value in _NO_FEE:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None
