"""``FakeUsageSource``: the payment provider's usage feed, in memory."""

from collections.abc import Mapping, Sequence

from livepeer_builder.contracts import UsagePage, UsageRow
from livepeer_builder.errors import CursorMismatch


class FakeUsageSource:
    """Pages a fixed row list like Batteries: opaque cursors, empty cursor on the last page.

    ``grant_of`` says which grant each allocation was allotted from, as Batteries knows it: a tenant's
    id is its grant's id (D83), so ``tenant_page`` returns the rows of the allocations it maps to that
    tenant, and no row of an allocation it does not name. Its cursor is bound to the tenant, as
    Batteries binds a cursor to its filters."""

    def __init__(
        self, rows: Sequence[UsageRow] = (), page_size: int = 2, grant_of: Mapping[str, str] | None = None
    ) -> None:
        self.rows = list(rows)
        self.page_size = page_size
        self.grant_of = dict(grant_of or {})

    async def page(self, cursor: str | None, limit: int = 1000) -> UsagePage:
        return self._paged(self.rows, "c", cursor, limit)

    async def tenant_page(self, tenant_id: str, cursor: str | None, limit: int = 1000) -> UsagePage:
        rows = [row for row in self.rows if self.grant_of.get(row.allocation_id or "") == tenant_id]
        return self._paged(rows, f"t:{tenant_id}:", cursor, limit)

    def _paged(self, rows: Sequence[UsageRow], bound_to: str, cursor: str | None, limit: int) -> UsagePage:
        start = 0
        if cursor:
            if not cursor.startswith(bound_to) or not cursor[len(bound_to) :].isdigit():
                raise CursorMismatch("invalid cursor")
            start = int(cursor[len(bound_to) :])
        end = start + min(limit, self.page_size)
        return UsagePage(
            items=tuple(rows[start:end]), next_cursor=f"{bound_to}{end}" if end < len(rows) else ""
        )
