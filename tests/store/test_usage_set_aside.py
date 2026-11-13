"""#114 C13 on each backend: usage rows set aside by cost sync, and the table that names them (0012).

PostgreSQL refuses a text longer than its column and a NUL; SQLite takes both. The cost sync decides
what the store can keep before it writes, so it behaves the same on each: these tests pass on SQLite and,
in CI, on PostgreSQL, where one such row used to fail its page on every pass.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import String, inspect, select

from livepeer_builder.contracts import SetAsideUsage, UsageRow
from livepeer_builder.services.costs import CostSyncWorker
from livepeer_builder.services.usage_intake import LIMITS
from livepeer_builder.store import SqlStore, create_engine, purge, upgrade
from livepeer_builder.store import schema as t
from livepeer_builder.store.migrate import downgrade
from livepeer_builder.testing import FakeUsageSource, FixedClock


def usage_row(clock: FixedClock, row_id: str, **fields: Any) -> UsageRow:
    values: dict[str, Any] = {
        "id": row_id, "event_id": None, "status": "applied", "manifest_id": f"m-{row_id}",
        "allocation_id": "a1", "payment_session_id": f"s-{row_id}", "request_id": None, "pipeline": None,
        "computed_fee_eth": Decimal("0.000000000000000001"), "computed_fee_usd": None, "currency": None,
        "created_at": clock.now(),
    }  # fmt: skip
    return UsageRow(**(values | fields))


async def recorded(store: SqlStore) -> list[tuple[str, str, str | None, str | None]]:
    columns = t.usage_set_aside.c
    query = select(columns.id, columns.reason, columns.computed_fee_eth, columns.computed_fee_usd)
    async with store.engine.connect() as conn:
        return sorted(tuple(row) for row in await conn.execute(query))


async def test_cost_sync_sets_aside_rows_the_store_cannot_keep_and_moves_the_cursor(
    database_url: str, clock: FixedClock
) -> None:
    store = await SqlStore.connect(database_url)
    rows = [
        usage_row(clock, "u1"),
        usage_row(clock, "long", manifest_id="m" * 201),
        usage_row(clock, "nul", payment_session_id="s\x00"),
        usage_row(clock, "surrogate", allocation_id="a\ud800"),
        usage_row(clock, "i" * 201),
        usage_row(clock, "u2"),
    ]
    worker = CostSyncWorker(FakeUsageSource(rows, page_size=4), store)
    report, new_rows = await worker.sync_once()
    assert [row.id for row in new_rows] == ["u1", "u2"]
    assert (report.pages, report.rows_seen, report.rows_new, report.rows_set_aside) == (2, 6, 2, 4)
    checkpoint = await store.cost_checkpoint()
    assert checkpoint is not None and checkpoint.resume_cursor == "c4"
    everything = await recorded(store)
    assert sorted((reason, fee_eth, fee_usd) for _, reason, fee_eth, fee_usd in everything) == [
        ("unstorable_allocation_id", "1E-18", None),
        ("unstorable_id", "1E-18", None),
        ("unstorable_manifest_id", "1E-18", None),
        ("unstorable_payment_session_id", "1E-18", None),
    ]
    named = sorted(key for key, _, _, _ in everything if not key.startswith("sha256:"))
    assert named == ["long", "nul", "surrogate"]  # the fourth has no id the table can keep: a digest
    assert await store.count_set_aside_usage() == 4
    again, _ = await worker.sync_once()  # the last page is read again: nothing is recorded twice
    assert (again.rows_new, again.rows_set_aside) == (0, 0)
    assert await store.count_set_aside_usage() == 4
    await store.close()


async def test_a_set_aside_row_is_recorded_once_with_its_reason_and_fee(database_url: str) -> None:
    store = await SqlStore.connect(database_url)
    first = SetAsideUsage(
        id="u-1", reason="unknown_status", computed_fee_eth=Decimal("0.000000039"),
        computed_fee_usd=Decimal("0.0001"), raw='{"id": "u-1", "status": "mystery"}',
    )  # fmt: skip
    no_fee = SetAsideUsage(id="sha256:" + "0" * 64, reason="missing_id")
    assert await store.set_aside_usage([first, no_fee]) == [first, no_fee]
    later = first.model_copy(update={"reason": "unreadable_created_at", "computed_fee_eth": Decimal(7)})
    assert await store.set_aside_usage([later, later]) == []  # the first record of an id is kept
    assert await store.set_aside_usage([]) == []
    assert await recorded(store) == [
        ("sha256:" + "0" * 64, "missing_id", None, None),
        ("u-1", "unknown_status", "3.9E-8", "0.0001"),
    ]
    assert await store.count_set_aside_usage() == 2
    async with store.engine.connect() as conn:  # the raw content is in the log, never in the table
        columns = {column["name"] for column in await conn.run_sync(lambda sync: inspect(sync).get_columns("lpb_usage_set_aside"))}  # fmt: skip
    assert columns == {"id", "reason", "computed_fee_eth", "computed_fee_usd", "set_aside_at"}
    await store.close()


async def test_set_aside_rows_outlive_retention_and_the_table_downgrades(
    database_url: str, clock: FixedClock
) -> None:
    """0012: the record of what cost sync left out is kept (no purge), and a downgrade drops the table."""
    engine = create_engine(database_url)
    await upgrade(engine)
    store = SqlStore(engine)
    await store.set_aside_usage([SetAsideUsage(id="u-1", reason="unknown_status")])
    clock.advance(timedelta(days=400).total_seconds())
    await purge(engine, clock.now(), jobs_days=1, usage_days=1)
    assert await store.count_set_aside_usage() == 1
    await downgrade(engine, "0011")
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
    assert "lpb_usage_set_aside" not in tables and "lpb_usage_rows" in tables
    await upgrade(engine)
    assert await store.count_set_aside_usage() == 0
    await store.close()


def test_the_intake_limits_are_the_usage_table_columns() -> None:
    """Cost sync sets aside what ``lpb_usage_rows`` cannot keep: its limits are that table's lengths."""
    lengths = {
        column.name: column.type.length
        for column in t.usage_rows.c
        if isinstance(column.type, String) and column.name != "status"  # a UsageStatus: four fixed words
    }
    assert lengths == LIMITS, "services/usage_intake.py LIMITS must list every text column of lpb_usage_rows"
    assert t.usage_set_aside.c.id.type.length == LIMITS["id"]
    assert t.usage_set_aside.c.computed_fee_eth.type.length == LIMITS["computed_fee_eth"]
