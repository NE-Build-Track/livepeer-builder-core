"""Which usage rows cost sync stores, and which it sets aside (#114, C13).

PostgreSQL refuses a text longer than its column, and a NUL; no database takes a lone surrogate. One such
row failed its page on every pass, so the cursor never moved and no later cost arrived. The rule is
checked here, before the store is asked, so the sync does the same on SQLite and on PostgreSQL.
"""

from livepeer_builder.contracts import SetAsideUsage, UsagePage, UsageRow
from livepeer_builder.contracts.costs import set_aside_record, storable_text

# The longest text ``lpb_usage_rows`` keeps in each column (``store/schema.py``): ids are ``ID``, fees
# are decimal strings of ``AMOUNT``. tests/store/test_usage_set_aside.py fails when the two disagree.
LIMITS: dict[str, int] = {
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
    for name, limit in LIMITS.items():
        value = getattr(row, name)
        if value is not None and not storable_text(str(value), limit):
            return name
    return None


def sort_page(page: UsagePage) -> tuple[list[UsageRow], list[SetAsideUsage]]:
    """The page's rows to store, and the ones to set aside: those its source could not read, then those
    the store could not keep."""
    rows: list[UsageRow] = []
    aside = list(page.set_aside)
    for row in page.items:
        field = unstorable_field(row)
        if field is None:
            rows.append(row)
        else:
            aside.append(_unstorable(row, field))
    return rows, aside


def _unstorable(row: UsageRow, field: str) -> SetAsideUsage:
    return set_aside_record(
        f"unstorable_{field}",
        row.model_dump(mode="json"),
        row_id=row.id,
        fee_eth=row.computed_fee_eth,
        fee_usd=row.computed_fee_usd,
    )
