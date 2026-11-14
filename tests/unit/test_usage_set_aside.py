"""#114 C13: one usage row the engine cannot read or store no longer stops cost sync.

A row with an unknown status or no timestamp failed its page on every pass, and on PostgreSQL so did a
row with a NUL or an id longer than the column: the cursor never moved, so no later cost arrived. The
sync now sets such a row aside (its id, the reason and the fee, in ``lpb_usage_set_aside``), logs its
content once, and moves on. The sync report and health count the rows left out.
"""

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.adapters.batteries import BatteriesUsageSource
from livepeer_builder.contracts import ActorContext, UsageRow
from livepeer_builder.http import create_app
from livepeer_builder.store import SqlStore
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FakeUsageSource
from tests.conftest import EngineFactory

CREATED_MS = 1760270400000


def item(row_id: str | None, **fields: Any) -> dict[str, Any]:
    """One ``GET /v1/usage`` item as Batteries serves it."""
    base: dict[str, Any] = {
        "id": row_id, "status": "applied", "allocation_id": "a1", "payment_session_id": f"s-{row_id}",
        "manifest_id": f"m-{row_id}", "computed_fee_eth": "0.5", "computed_fee_usd": "0.25",
        "created_at_ms": CREATED_MS,
    }  # fmt: skip
    return {name: value for name, value in (base | fields).items() if value is not ...}


def batteries(*pages: Sequence[dict[str, Any]]) -> BatteriesUsageSource:
    """A usage feed of ``pages``: the cursor of page ``n`` is ``p<n>``, and the last page has none."""

    def handle(request: httpx.Request) -> httpx.Response:
        index = int(request.url.params.get("cursor", "p0")[1:])
        following = f"p{index + 1}" if index + 1 < len(pages) else ""
        body = json.dumps({"items": list(pages[index]), "next_cursor": following})  # a NaN literal too
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    client = httpx.AsyncClient(base_url="https://batteries.test", transport=httpx.MockTransport(handle))
    return BatteriesUsageSource("https://batteries.test", "t", client=client)


def usage_row(row_id: str, **fields: Any) -> UsageRow:
    values: dict[str, Any] = {
        "id": row_id, "event_id": None, "status": "applied", "manifest_id": f"m-{row_id}",
        "allocation_id": "a1", "payment_session_id": f"s-{row_id}", "request_id": None, "pipeline": None,
        "computed_fee_eth": Decimal("0.5"), "computed_fee_usd": Decimal("0.25"), "currency": None,
        "created_at": datetime(2026, 10, 12, 12, 0, tzinfo=UTC),
    }  # fmt: skip
    return UsageRow(**(values | fields))


async def stored_ids(store: SqlStore) -> list[str]:
    async with store.engine.connect() as conn:
        return sorted((await conn.execute(select(t.usage_rows.c.id))).scalars())


async def set_aside(store: SqlStore) -> list[tuple[str, str, str | None, str | None]]:
    """(id, reason, fee_eth, fee_usd) of every row the sync left out."""
    columns = t.usage_set_aside.c
    query = select(columns.id, columns.reason, columns.computed_fee_eth, columns.computed_fee_usd)
    async with store.engine.connect() as conn:
        return sorted(tuple(row) for row in await conn.execute(query))


def logged(records: Sequence[logging.LogRecord]) -> list[logging.LogRecord]:
    return [record for record in records if getattr(record, "event", "") == "costs.usage_set_aside"]


UNREADABLE = {
    "unknown status": (item("u-bad", status="mystery"), "unknown_status"),
    "a status that is not text": (item("u-bad", status=["applied"]), "unknown_status"),
    "no timestamp": (item("u-bad", created_at_ms=...), "unreadable_created_at"),
    "a timestamp that is not a number": (item("u-bad", created_at_ms="soon"), "unreadable_created_at"),
    "a timestamp no clock can hold": (item("u-bad", created_at_ms=10**30), "unreadable_created_at"),
}


@pytest.mark.parametrize("name", UNREADABLE)
async def test_a_usage_row_the_engine_cannot_read_is_set_aside_and_the_cursor_moves(
    make_engine: EngineFactory, store: SqlStore, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    bad, reason = UNREADABLE[name]
    source = batteries([item("u1")], [bad, item("u2")])  # the bad row is on the page each pass re-reads
    engine = await make_engine(supply=EngineSupply(usage_source=source))
    with caplog.at_level(logging.WARNING):
        report = await engine.costs.sync_once()
    assert (report.pages, report.rows_seen, report.rows_new, report.rows_set_aside) == (2, 3, 2, 1)
    assert await stored_ids(store) == ["u1", "u2"]
    checkpoint = await store.cost_checkpoint()
    assert checkpoint is not None and checkpoint.resume_cursor == "p1"
    assert await set_aside(store) == [("u-bad", reason, "0.5", "0.25")]
    health = engine.health()
    assert (health.cost_sync_error, health.cost_sync_set_aside) == (None, 1)
    # the raw content goes to the log, once
    [line] = logged(caplog.records)
    assert (line.row_id, line.reason, line.fee_eth, line.fee_usd) == ("u-bad", reason, "0.5", "0.25")  # type: ignore[attr-defined]
    assert json.loads(line.raw) == bad  # type: ignore[attr-defined]

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        again = await engine.costs.sync_once()  # re-reads the last page: the same bad row
    assert (again.rows_new, again.rows_set_aside) == (0, 0)
    assert await set_aside(store) == [("u-bad", reason, "0.5", "0.25")]
    assert engine.health().cost_sync_set_aside == 1
    assert logged(caplog.records) == []


@pytest.mark.parametrize("row_id", [..., None, "", ["u1"]], ids=["absent", "null", "empty", "a list"])
async def test_a_usage_row_with_no_id_is_set_aside_under_a_digest_of_its_content(
    make_engine: EngineFactory, store: SqlStore, row_id: Any
) -> None:
    bad = item("u-bad", id=row_id)
    engine = await make_engine(supply=EngineSupply(usage_source=batteries([bad, item("u1")])))
    first = await engine.costs.sync_once()
    again = await engine.costs.sync_once()
    assert (first.rows_new, first.rows_set_aside, again.rows_set_aside) == (1, 1, 0)
    assert await stored_ids(store) == ["u1"]
    [(key, reason, fee_eth, fee_usd)] = await set_aside(store)
    assert key.startswith("sha256:") and len(key) == 71
    assert (reason, fee_eth, fee_usd) == ("missing_id", "0.5", "0.25")


UNREADABLE_FEES = {
    "NaN": ({"computed_fee_usd": "NaN"}, ("0.5", None)),
    "Infinity": ({"computed_fee_eth": "Infinity"}, (None, "0.25")),
    "-Infinity": ({"computed_fee_eth": "-Infinity"}, (None, "0.25")),
    "a NaN literal": ({"computed_fee_usd": float("nan")}, ("0.5", None)),
    "sNaN": ({"computed_fee_usd": "sNaN"}, ("0.5", None)),
    "words": ({"computed_fee_eth": "abc"}, (None, "0.25")),
    "an object": ({"computed_fee_eth": {"wei": "5"}}, (None, "0.25")),
    "a decimal comma": ({"computed_fee_usd": "0,5"}, ("0.5", None)),
    "true": ({"computed_fee_eth": True}, (None, "0.25")),
    "both": ({"computed_fee_eth": "NaN", "computed_fee_usd": "abc"}, (None, None)),
}


@pytest.mark.parametrize("name", UNREADABLE_FEES)
async def test_a_usage_row_whose_fee_is_stated_and_is_no_finite_number_is_set_aside_and_the_cursor_moves(
    make_engine: EngineFactory, store: SqlStore, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    """``NaN`` and ``Infinity`` failed the page on every pass: neither a usage row nor a set-aside record
    takes one. A fee that is no number at all was stored as no fee, and an applied row then cost nothing."""
    fees, kept = UNREADABLE_FEES[name]
    source = batteries([item("u1")], [item("u-bad", **fees), item("u2")])
    engine = await make_engine(supply=EngineSupply(usage_source=source))
    with caplog.at_level(logging.WARNING):
        report = await engine.costs.sync_once()
    assert (report.pages, report.rows_seen, report.rows_new, report.rows_set_aside) == (2, 3, 2, 1)
    assert await stored_ids(store) == ["u1", "u2"]
    checkpoint = await store.cost_checkpoint()
    assert checkpoint is not None and checkpoint.resume_cursor == "p1"
    assert await set_aside(store) == [("u-bad", "unreadable_fee", *kept)]  # the fee it could read is kept
    [line] = logged(caplog.records)
    assert (line.row_id, line.reason, line.fee_eth, line.fee_usd) == ("u-bad", "unreadable_fee", *kept)  # type: ignore[attr-defined]
    assert '"id": "u-bad"' in line.raw  # type: ignore[attr-defined]
    again = await engine.costs.sync_once()
    assert (again.rows_new, again.rows_set_aside, engine.health().cost_sync_set_aside) == (0, 0, 1)


@pytest.mark.parametrize("fee", [..., None, "", "[error]"], ids=["absent", "null", "empty", "[error]"])
async def test_a_usage_row_that_states_no_fee_is_stored_with_no_fee(
    make_engine: EngineFactory, store: SqlStore, fee: Any
) -> None:
    """Batteries serves ``null``, ``""`` or ``"[error]"`` for a fee it has not: that is no fee, not a bad one."""
    quiet = item("u-quiet", status="ignored", computed_fee_eth=fee, computed_fee_usd=fee)
    engine = await make_engine(supply=EngineSupply(usage_source=batteries([quiet, item("u1")])))
    report = await engine.costs.sync_once()
    assert (report.rows_new, report.rows_set_aside) == (2, 0)
    columns = t.usage_rows.c
    query = select(columns.computed_fee_eth, columns.computed_fee_usd).where(columns.id == "u-quiet")
    async with store.engine.connect() as conn:
        assert [tuple(row) for row in await conn.execute(query)] == [(None, None)]


async def test_a_row_set_aside_for_another_reason_is_recorded_without_a_fee_that_is_no_finite_number(
    make_engine: EngineFactory, store: SqlStore
) -> None:
    """The record takes no ``NaN`` either: before, such a row could be neither stored nor set aside."""
    bad = item("u-bad", status="mystery", computed_fee_usd="sNaN", computed_fee_eth="Infinity")
    engine = await make_engine(supply=EngineSupply(usage_source=batteries([bad, item("u1")])))
    report = await engine.costs.sync_once()
    assert (report.rows_new, report.rows_set_aside) == (1, 1)
    assert await set_aside(store) == [("u-bad", "unknown_status", None, None)]


async def test_a_usage_row_the_checks_pass_and_the_row_type_refuses_is_set_aside(
    make_engine: EngineFactory, store: SqlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backstop: whatever a check misses, a row that is no ``UsageRow`` never fails its page."""
    monkeypatch.setattr("livepeer_builder.adapters.batteries._unreadable", lambda item: None)
    source = batteries([item("u1")], [item("u-bad", computed_fee_usd="NaN"), item("u2")])
    engine = await make_engine(supply=EngineSupply(usage_source=source))
    report = await engine.costs.sync_once()
    assert (report.pages, report.rows_new, report.rows_set_aside) == (2, 2, 1)
    assert await set_aside(store) == [("u-bad", "unreadable_row", "0.5", None)]


LONG = "x" * 201
UNSTORABLE = {
    "a manifest id longer than its column": {"manifest_id": LONG},
    "a NUL in the manifest id": {"manifest_id": "m\x00"},
    "a lone surrogate in the allocation id": {"allocation_id": "a\ud800"},
    "a payment session id longer than its column": {"payment_session_id": LONG},
    "an event id longer than its column": {"event_id": LONG},
    "a request id with a NUL": {"request_id": "r\x00"},
    "a pipeline longer than its column": {"pipeline": LONG},
    "a currency longer than its column": {"currency": "x" * 9},
    "a fee longer than its column": {"computed_fee_usd": Decimal("0." + "1" * 64)},
}


@pytest.mark.parametrize("name", UNSTORABLE)
async def test_a_usage_row_the_store_cannot_keep_is_set_aside_whatever_the_usage_source(
    make_engine: EngineFactory, store: SqlStore, usage: FakeUsageSource, name: str
) -> None:
    (field, value), = UNSTORABLE[name].items()  # fmt: skip
    usage.rows = [usage_row("u1"), usage_row("u-bad", **{field: value}), usage_row("u2"), usage_row("u3")]
    engine = await make_engine()
    report = await engine.costs.sync_once()
    assert (report.rows_seen, report.rows_new, report.rows_set_aside) == (4, 3, 1)
    assert await stored_ids(store) == ["u1", "u2", "u3"]
    fee_usd = None if field == "computed_fee_usd" else "0.25"  # a fee the table cannot keep is not recorded
    assert await set_aside(store) == [("u-bad", f"unstorable_{field}", "0.5", fee_usd)]
    assert engine.health().cost_sync_set_aside == 1


@pytest.mark.parametrize("row_id", [LONG, "u\x00", "u\ud800"], ids=["too long", "NUL", "lone surrogate"])
async def test_a_usage_row_whose_id_the_store_cannot_keep_is_set_aside_under_a_digest(
    make_engine: EngineFactory, store: SqlStore, usage: FakeUsageSource, row_id: str
) -> None:
    usage.rows = [usage_row(row_id), usage_row("u1")]
    engine = await make_engine()
    first = await engine.costs.sync_once()
    again = await engine.costs.sync_once()
    assert (first.rows_new, first.rows_set_aside, again.rows_set_aside) == (1, 1, 0)
    [(key, reason, fee_eth, _)] = await set_aside(store)
    assert key.startswith("sha256:") and (reason, fee_eth) == ("unstorable_id", "0.5")


async def test_the_rows_after_a_set_aside_one_still_arrive(
    make_engine: EngineFactory, store: SqlStore, usage: FakeUsageSource
) -> None:
    """Before, the page failed on every pass, so the good rows on it and after it never arrived."""
    usage.rows = [usage_row("u-bad", manifest_id="m\ud800"), usage_row("u1")]
    usage.page_size = 1
    engine = await make_engine()
    report = await engine.costs.sync_once()
    assert (report.pages, report.rows_new, report.rows_set_aside) == (2, 1, 1)
    assert await stored_ids(store) == ["u1"]


async def test_the_count_of_set_aside_rows_is_served_in_health_and_the_sync_report(
    make_engine: EngineFactory, usage: FakeUsageSource, admin: ActorContext
) -> None:
    usage.rows = [usage_row("u-bad", manifest_id=LONG), usage_row("u1")]
    engine: BuilderEngine = await make_engine()
    assert engine.health().cost_sync_set_aside is None  # no cost sync yet in this process
    key = await engine.keys.bootstrap_admin()
    app = create_app(engine, manage_lifecycle=False)
    headers = {"Authorization": f"Bearer {key.token}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        report = await client.post("/v1/admin/costs/sync", headers=headers)
        health = await client.get("/readyz")
    assert report.json() == {
        "pages": 1,
        "rows_seen": 2,
        "rows_new": 1,
        "jobs_updated": 0,
        "rows_set_aside": 1,
    }
    assert health.json()["cost_sync_set_aside"] == 1


class _NoSetAsideTable:
    """A supplied store from before #114: everything the SQL store does, without the two set-aside calls."""

    def __init__(self, inner: SqlStore) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        if name in ("set_aside_usage", "count_set_aside_usage"):
            raise AttributeError(name)
        return getattr(self._inner, name)


async def test_a_store_without_a_set_aside_table_still_syncs_and_the_row_is_logged_once(
    make_engine: EngineFactory, store: SqlStore, usage: FakeUsageSource, caplog: pytest.LogCaptureFixture
) -> None:
    """Recording set-aside rows is optional for a store (``UsageSetAsideRepo``). A supplied store that
    lacks it keeps syncing: the row is left out, the cursor moves, the row is logged once, and health
    reports no count."""
    usage.rows = [usage_row("ok"), usage_row("bad", manifest_id=LONG)]
    supplied = _NoSetAsideTable(store)
    engine: BuilderEngine = await make_engine(supply=EngineSupply(store=supplied))  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING, logger="livepeer_builder"):
        first = await engine.costs.sync_once()
        second = await engine.costs.sync_once()

    assert (first.rows_new, first.rows_set_aside) == (1, 1)
    assert second.rows_set_aside == 0  # the same row on the page read again: not counted or logged twice
    assert await stored_ids(store) == ["ok"]
    logged = [record for record in caplog.records if getattr(record, "event", "") == "costs.usage_set_aside"]
    assert len(logged) == 1
    assert engine.health().cost_sync_set_aside is None
