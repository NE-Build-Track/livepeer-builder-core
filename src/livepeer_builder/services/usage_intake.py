"""Which usage rows cost sync stores, and which it sets aside (#114, C13).

PostgreSQL refuses a text longer than its column, and a NUL; no database takes a lone surrogate. One such
row failed its page on every pass, so the cursor never moved and no later cost arrived. The rule
(``contracts.costs.unstorable_field``) is checked here, before the store is asked, so the sync does the
same on SQLite and on PostgreSQL, whatever the usage source. The Batteries adapter checks it first, on the
item it still holds, so its log line is the row as received.
"""

from collections.abc import Sequence

from livepeer_builder.contracts import SetAsideUsage, UsagePage, UsageRow
from livepeer_builder.contracts.costs import set_aside_record, unstorable_field, unstorable_reason
from livepeer_builder.protocols import UsageSetAsideRepo


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
    """The record of a row no source set aside itself: its content is the row as that source read it."""
    return set_aside_record(
        unstorable_reason(field),
        row.model_dump(mode="json"),
        row_id=row.id,
        fee_eth=row.computed_fee_eth,
        fee_usd=row.computed_fee_usd,
    )


class SetAsideRecorder:
    """Where the rows left out go. A store that keeps them (``UsageSetAsideRepo``, as the packaged SQL
    store does) records each once. A supplied store without that still syncs: the ids are remembered for
    this process, so a row on a page that is read again is reported once."""

    def __init__(self, store: object) -> None:
        self._store = store
        self._seen: set[str] = set()

    async def record(self, rows: Sequence[SetAsideUsage]) -> list[SetAsideUsage]:
        """The rows not recorded before."""
        if isinstance(self._store, UsageSetAsideRepo):
            return await self._store.set_aside_usage(rows)
        new: list[SetAsideUsage] = []
        for row in rows:
            if row.id not in self._seen:
                self._seen.add(row.id)
                new.append(row)
        return new


async def count_set_aside(store: object) -> int | None:
    """Every row left out so far, or None when the store keeps no such record."""
    return await store.count_set_aside_usage() if isinstance(store, UsageSetAsideRepo) else None
