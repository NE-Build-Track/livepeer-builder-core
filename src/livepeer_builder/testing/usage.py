"""``FakeUsageSource``: the payment provider's usage feed, in memory."""

from collections.abc import Sequence

from livepeer_builder.contracts import UsagePage, UsageRow
from livepeer_builder.errors import CursorMismatch


class FakeUsageSource:
    """Pages a fixed row list like Batteries: opaque cursors, empty cursor on the last page."""

    def __init__(self, rows: Sequence[UsageRow] = (), page_size: int = 2) -> None:
        self.rows = list(rows)
        self.page_size = page_size

    async def page(self, cursor: str | None, limit: int = 1000) -> UsagePage:
        start = 0
        if cursor:
            if not cursor.startswith("c"):
                raise CursorMismatch("invalid cursor")
            start = int(cursor[1:])
        end = start + min(limit, self.page_size)
        items = tuple(self.rows[start:end])
        return UsagePage(items=items, next_cursor=f"c{end}" if end < len(self.rows) else "")
