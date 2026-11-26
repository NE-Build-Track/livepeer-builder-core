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
from pathlib import Path
from typing import Any, get_args

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.adapters.batteries import BatteriesUsageSource
from livepeer_builder.contracts import ActorContext, SetAsideReason, SetAsideUsage, UsageRow
from livepeer_builder.errors import ProviderError
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
    "a timestamp in a string": (item("u-bad", created_at_ms=str(CREATED_MS)), "unreadable_created_at"),
    "a timestamp of true": (item("u-bad", created_at_ms=True), "unreadable_created_at"),
    "a manifest id that is a number": (item("u-bad", manifest_id=12), "unstorable_manifest_id"),
    "a manifest id that is an object": (item("u-bad", manifest_id={"x": "y"}), "unstorable_manifest_id"),
    "an allocation id that is a number": (item("u-bad", allocation_id=7), "unstorable_allocation_id"),
    "a session id that is a list": (item("u-bad", payment_session_id=["s"]), "unstorable_payment_session_id"),
    "an event id of true": (item("u-bad", event_id=True), "unstorable_event_id"),
    "a request id that is a fraction": (item("u-bad", request_id=1.5), "unstorable_request_id"),
    "a pipeline that is an object": (item("u-bad", pipeline={"a": 1}), "unstorable_pipeline"),
    "a currency that is a number": (item("u-bad", currency=840), "unstorable_currency"),
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


NO_ID = {"absent": ..., "null": None, "empty": "", "a list": ["u1"], "a fraction": 1.5, "true": True}


@pytest.mark.parametrize("row_id", NO_ID.values(), ids=NO_ID)
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
    monkeypatch.setattr("livepeer_builder.adapters.batteries_usage_rows._unreadable", lambda item: None)
    source = batteries([item("u1")], [item("u-bad", computed_fee_usd="NaN"), item("u2")])
    engine = await make_engine(supply=EngineSupply(usage_source=source))
    report = await engine.costs.sync_once()
    assert (report.pages, report.rows_new, report.rows_set_aside) == (2, 2, 1)
    assert await set_aside(store) == [("u-bad", "unreadable_row", "0.5", None)]


def test_a_set_aside_reason_is_one_of_the_codes_the_engine_produces() -> None:
    """The reason is a closed set (``SetAsideReason``), so a misspelt code is refused where it is written."""
    reason = SetAsideUsage.model_json_schema()["properties"]["reason"]
    assert set(reason["enum"]) == set(get_args(SetAsideReason))
    with pytest.raises(ValidationError):
        SetAsideUsage(id="u-1", reason="unknown_stauts")  # type: ignore[arg-type]


def test_the_contract_lists_every_set_aside_reason() -> None:
    """docs/contracts/payments.md is where an operator looks a reason up."""
    table = Path(__file__).parents[2].joinpath("docs/contracts/payments.md").read_text()
    reasons = {
        "unstorable_<field>" if code.startswith("unstorable_") else code for code in get_args(SetAsideReason)
    }
    missing = sorted(code for code in reasons if f"| `{code}` |" not in table)
    assert not missing, f"add a row for {missing} to the reasons table in docs/contracts/payments.md"


async def test_a_usage_row_with_a_whole_number_id_is_stored_under_its_digits(
    make_engine: EngineFactory, store: SqlStore
) -> None:
    engine = await make_engine(supply=EngineSupply(usage_source=batteries([item("u1", id=12345)])))
    report = await engine.costs.sync_once()
    assert (report.rows_new, report.rows_set_aside) == (1, 0)
    assert await stored_ids(store) == ["12345"]


BATTERIES_UNSTORABLE = {
    "a long manifest id": ({"manifest_id": "x" * 201}, "unstorable_manifest_id", "u-bad"),
    "a NUL in the pipeline": ({"pipeline": "p\x00"}, "unstorable_pipeline", "u-bad"),
    "a long id": ({"id": "i" * 201}, "unstorable_id", None),
    "a long fee": ({"computed_fee_usd": "0." + "1" * 64}, "unstorable_computed_fee_usd", "u-bad"),
}


@pytest.mark.parametrize("name", BATTERIES_UNSTORABLE)
async def test_a_batteries_row_the_store_cannot_keep_is_logged_as_received(
    make_engine: EngineFactory, store: SqlStore, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    """The log line is what an operator takes to Batteries: the row as it was served, with the fields the
    engine keeps no column for. Before, it was the row as the engine had read it."""
    fields, reason, kept_id = BATTERIES_UNSTORABLE[name]
    bad = item("u-bad", topic="usage", partition=3, offset=41, error="", **fields)
    engine = await make_engine(supply=EngineSupply(usage_source=batteries([bad, item("u1")])))
    with caplog.at_level(logging.WARNING):
        report = await engine.costs.sync_once()
    assert (report.rows_new, report.rows_set_aside) == (1, 1)
    assert await stored_ids(store) == ["u1"]
    [line] = logged(caplog.records)
    assert json.loads(line.raw) == bad  # type: ignore[attr-defined]
    assert line.reason == reason  # type: ignore[attr-defined]
    [(key, _, fee_eth, _)] = await set_aside(store)
    assert (key == kept_id or key.startswith("sha256:")) and fee_eth == "0.5"


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
        report = await client.post("/v1/costs/sync", headers=headers)
        health = await client.get("/readyz")
    assert report.json() == {
        "pages": 1,
        "rows_seen": 2,
        "rows_new": 1,
        "jobs_updated": 0,
        "rows_set_aside": 1,
    }
    assert health.json()["cost_sync_set_aside"] == 1


async def test_health_counts_the_rows_a_failed_sync_set_aside(
    make_engine: EngineFactory, store: SqlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row is set aside on the first page and the next page fails on every pass. The table had the row
    and health said ``null``: the count was read only after a whole sync."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cursor") == "p1":
            return httpx.Response(503, json={"error": "overloaded"})
        return httpx.Response(200, json={"items": [item("u-bad", status="mystery")], "next_cursor": "p1"})

    client = httpx.AsyncClient(base_url="https://batteries.test", transport=httpx.MockTransport(handle))
    source = BatteriesUsageSource("https://batteries.test", "t", client=client)
    engine = await make_engine(supply=EngineSupply(usage_source=source))
    with pytest.raises(ProviderError):
        await engine.costs.sync_once()
    assert await set_aside(store) == [("u-bad", "unknown_status", "0.5", "0.25")]
    assert engine.health().cost_sync_set_aside == 1

    async def no_count() -> int:
        raise RuntimeError("the store is away")

    monkeypatch.setattr(store, "count_set_aside_usage", no_count)
    with pytest.raises(ProviderError):  # the sync's own failure is still the one raised
        await engine.costs.sync_once()
    assert engine.health().cost_sync_set_aside == 1


async def test_health_counts_the_rows_set_aside_when_a_later_step_of_the_sync_fails(
    make_engine: EngineFactory, store: SqlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_match(rows: Sequence[UsageRow]) -> set[Any]:
        raise RuntimeError("the store is away")

    monkeypatch.setattr(store, "match_sessions", no_match)
    source = batteries([item("u-bad", status="mystery"), item("u1")])
    engine = await make_engine(supply=EngineSupply(usage_source=source))
    with pytest.raises(RuntimeError):
        await engine.costs.sync_once()
    assert engine.health().cost_sync_set_aside == 1


async def test_a_count_that_cannot_be_read_fails_no_sync(
    make_engine: EngineFactory, store: SqlStore, usage: FakeUsageSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #144: the count is read for health alone, after the rows are stored and the cursor has
    moved. When that read failed the pass ended there: its rows were matched to no session and no job's
    cost was refreshed, and a later pass does not see those rows as new."""
    usage.rows = [usage_row("u1")]
    engine = await make_engine()
    matched: list[list[str]] = []
    match = store.match_sessions

    async def recording(rows: Sequence[UsageRow]) -> set[Any]:
        matched.append([row.id for row in rows])
        return await match(rows)

    async def no_count() -> int:
        raise RuntimeError("the store is away")

    monkeypatch.setattr(store, "match_sessions", recording)
    monkeypatch.setattr(store, "count_set_aside_usage", no_count)
    report = await engine.costs.sync_once()
    assert (report.rows_new, matched) == (1, [["u1"]])
    assert engine.costs.last_synced_at is not None and engine.costs.last_error is None
    assert engine.health().cost_sync_set_aside is None  # no count was read, and none is made up


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


# -- #158: an item the JSON decoder's own limits refuse ------------------------------------------------

DEEP = "[" * 100_000 + "]" * 100_000
PAST_THE_DECODER = {
    "a 5000-digit integer": '{"id": "u-bad", "status": "applied", "offset": ' + "9" * 5000 + "}",
    "a 5000-digit fee": '{"id": "u-bad", "status": "applied", "computed_fee_eth": ' + "9" * 5000 + "}",
    "a deeply nested field": '{"id": "u-bad", "status": "applied", "extra": ' + DEEP + "}",
    "deeply nested objects": '{"id": "u-bad", "a": ' + '{"a": ' * 50_000 + "1" + "}" * 50_000 + "}",
    "brackets in its text too": '{"id": "u-\\"}]{[", "note": "]}\\\\", "offset": ' + "9" * 5000 + "}",
}


def raw_batteries(*bodies: str) -> BatteriesUsageSource:
    """A usage feed that answers page ``n`` with the text ``bodies[n]``, as written: its cursor is ``p<n>``."""

    def handle(request: httpx.Request) -> httpx.Response:
        index = int(request.url.params.get("cursor", "p0")[1:])
        return httpx.Response(
            200, content=bodies[index].encode(), headers={"content-type": "application/json"}
        )

    client = httpx.AsyncClient(base_url="https://batteries.test", transport=httpx.MockTransport(handle))
    return BatteriesUsageSource("https://batteries.test", "t", client=client)


def page_text(*items: str | dict[str, Any], next_cursor: str = "") -> str:
    texts = [entry if isinstance(entry, str) else json.dumps(entry) for entry in items]
    return '{"items": [' + ", ".join(texts) + '], "next_cursor": ' + json.dumps(next_cursor) + "}"


@pytest.mark.parametrize("name", PAST_THE_DECODER)
async def test_a_usage_item_the_json_decoder_cannot_read_is_set_aside_and_the_rest_of_its_page_is_stored(
    make_engine: EngineFactory, store: SqlStore, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    """One integer past the decoder's digit limit, or one field nested past its depth, failed the whole
    page at ``response.json()``, before any row was read: no row could be set aside, the cursor never
    moved and no later cost arrived."""
    bad = PAST_THE_DECODER[name]
    source = raw_batteries(
        page_text(item("u1"), next_cursor="p1"), page_text(item("u2"), bad, item("u3"), next_cursor="p2"),
        page_text(item("u4")),
    )  # fmt: skip
    engine = await make_engine(supply=EngineSupply(usage_source=source))
    with caplog.at_level(logging.WARNING):
        report = await engine.costs.sync_once()
    assert (report.pages, report.rows_seen, report.rows_new, report.rows_set_aside) == (3, 5, 4, 1)
    assert await stored_ids(store) == ["u1", "u2", "u3", "u4"]
    checkpoint = await store.cost_checkpoint()
    assert checkpoint is not None and checkpoint.resume_cursor == "p2"
    [(key, reason, fee_eth, fee_usd)] = await set_aside(store)
    assert key.startswith("sha256:") and len(key) == 71  # nothing of it is read, its id included
    assert (reason, fee_eth, fee_usd) == ("unreadable_row", None, None)
    [line] = logged(caplog.records)
    assert (line.row_id, line.reason) == (key, "unreadable_row")  # type: ignore[attr-defined]
    assert line.raw == bad[:2000]  # type: ignore[attr-defined]  # the item's own text, as received
    health = engine.health()
    assert (health.cost_sync_error, health.cost_sync_set_aside) == (None, 1)

    again = await engine.costs.sync_once()  # re-reads the last page only
    assert (again.rows_new, again.rows_set_aside) == (0, 0)


async def test_two_items_the_decoder_cannot_read_on_one_page_are_each_set_aside_once() -> None:
    one, two = PAST_THE_DECODER["a 5000-digit integer"], PAST_THE_DECODER["a deeply nested field"]
    body = '{"next_cursor": "p1", "items": [' + ", ".join([one, json.dumps(item("u1")), two, one]) + "]}"
    page = await raw_batteries(body).page(None)
    assert ([row.id for row in page.items], page.next_cursor) == (["u1"], "p1")
    assert [row.reason for row in page.set_aside] == ["unreadable_row"] * 3
    assert page.set_aside[0].id == page.set_aside[2].id != page.set_aside[1].id  # a digest of its text


async def test_a_page_with_a_byte_order_mark_is_salvaged_as_the_decoder_reads_it() -> None:
    """The decoder takes a UTF-8 page that begins with a byte order mark. The salvage decoded the bytes
    itself and kept the mark, which the decoder then refused as text: a page the ordinary reader takes
    failed on every pass once it held one oversized item, and the cursor never moved (John, #189 review)."""
    plain = b"\xef\xbb\xbf" + page_text(item("u1"), next_cursor="p1").encode()
    marked = (
        b"\xef\xbb\xbf"
        + page_text(item("u1"), PAST_THE_DECODER["a 5000-digit integer"], next_cursor="p1").encode()
    )
    for body, set_aside_rows in ((plain, 0), (marked, 1)):
        client = httpx.AsyncClient(
            base_url="https://batteries.test",
            transport=httpx.MockTransport(lambda _, body=body: httpx.Response(200, content=body)),
        )
        page = await BatteriesUsageSource("https://batteries.test", "t", client=client).page(None)
        assert ([row.id for row in page.items], page.next_cursor) == (["u1"], "p1")
        assert len(page.set_aside) == set_aside_rows


async def test_a_page_that_is_not_text_is_no_page_whatever_its_items() -> None:
    """Bytes that are no UTF-8 are not a page the decoder reads, with an oversized item in it or not."""
    body = page_text(item("u1"), PAST_THE_DECODER["a 5000-digit integer"]).encode().replace(b"u1", b"u\xff")
    client = httpx.AsyncClient(
        base_url="https://batteries.test",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)),
    )
    with pytest.raises(ProviderError) as refused:
        await BatteriesUsageSource("https://batteries.test", "t", client=client).page(None)
    assert refused.value.status_code == 200


STILL_NO_PAGE = {
    "a page cut short": page_text(item("u1"), PAST_THE_DECODER["a deeply nested field"])[:-30],
    "an item that is not JSON": page_text(item("u1"), '{"id": "u-bad", "offset": 12x}'),
    "a deep item that is no object": page_text(item("u1"), DEEP),
    "a deep value beside the items": '{"items": [], "next_cursor": "", "extra": ' + DEEP + "}",
    "a deep object in another list": '{"items": [], "extra": [' + PAST_THE_DECODER["deeply nested objects"] + "]}",
    "a deep item on a page with text after its end": page_text({"id": "u1", "extra": [1]}).replace("[1]", DEEP) + "x",
    "a page that is a list": "[" + PAST_THE_DECODER["a 5000-digit integer"] + "]",
    "an item that is null beside one": page_text("null", PAST_THE_DECODER["a 5000-digit integer"]),
    "a cursor that is a 5000-digit integer": '{"items": [], "next_cursor": ' + "9" * 5000 + "}",
    # The decoder keeps the last of a repeated key: the first list's row was dropped, the item cut from the
    # second accounted for every marker, and the page's cursor was followed past the row (John, #189 review).
    "items named twice, with a row in the first list": '{"items": [' + json.dumps(item("u-good")) + '], "items": ['
    + PAST_THE_DECODER["a 5000-digit integer"] + '], "next_cursor": "p2"}',
    "a cursor named twice": '{"items": [' + PAST_THE_DECODER["a 5000-digit integer"] + '], "next_cursor": "p1", '
    + '"next_cursor": "p2"}',
    # The decoder stops at the first limit it meets and never reads on: an item that is not JSON after its
    # oversized integer, or after its nesting, was cut out as if it were one, and the page was checkpointed
    # (John, #189 review). What is left out must be JSON the decoder only refuses for its size.
    "an oversized integer that hides a syntax error after it": page_text(
        item("u1"), '{"id": "u-bad", "offset": ' + "9" * 5000 + ', "then": oops}'
    ),
    "an oversized integer in an item with a key that is no string": page_text(
        item("u1"), '{"id": "u-bad", "offset": ' + "9" * 5000 + ", 7: 1}"
    ),
    "deep nesting that hides a syntax error after it": page_text(
        item("u1"), '{"id": "u-bad", "extra": ' + DEEP + ', "then": oops}'
    ),
    "deep nesting whose brackets do not match": page_text(
        item("u1"), '{"id": "u-bad", "extra": ' + "[" * 100_000 + "}" * 100_000 + "}"
    ),
    "an oversized integer with a leading zero": page_text(item("u1"), '{"id": "u-bad", "offset": 0' + "9" * 5000 + "}"),
    # and with no item past the decoder, where the decoder took the page and the first list's row was gone
    "items named twice, and nothing past the decoder": '{"items": [' + json.dumps(item("u-good"))
    + '], "items": [], "next_cursor": "p2"}',
}  # fmt: skip


@pytest.mark.parametrize("name", STILL_NO_PAGE)
async def test_a_usage_page_that_is_malformed_still_fails_whatever_its_items(name: str) -> None:
    """Only an item the decoder's limits refuse is set aside. A page that is not JSON, or whose envelope
    the engine cannot read, is no page: nothing of it is stored and the cursor stays."""
    with pytest.raises(ProviderError) as refused:
        await raw_batteries(STILL_NO_PAGE[name]).page(None)
    assert refused.value.status_code == 200


# -- #158: a fee is the decimal string Batteries writes, and an id is not blank ------------------------

NOT_A_FEE_AS_BATTERIES_WRITES_ONE: dict[str, Any] = {
    "an exponent past any amount": "1e999999",
    "a small exponent": "3.9e-8",
    "a digit separator": "1_000",
    "full-width digits": "\uff11\uff12",
    "digits of another script": "\u0663",
    "padding": " 0.5 ",
    "a line break after it": "0.5\n",
    "a plus sign": "+0.5",
    "no digit before the point": ".5",
    "no digit after the point": "5.",
    "a JSON fraction": 0.1,
    "a JSON whole number": 5,
    "a JSON number past a float": 1e400,
}


@pytest.mark.parametrize("field", ["computed_fee_eth", "computed_fee_usd"])
@pytest.mark.parametrize("name", NOT_A_FEE_AS_BATTERIES_WRITES_ONE)
async def test_a_fee_that_is_not_a_plain_decimal_string_sets_the_row_aside_and_is_never_guessed_at(
    make_engine: EngineFactory, store: SqlStore, name: str, field: str
) -> None:
    """Python's ``Decimal`` reads more than Batteries writes: ``"1e999999"`` was stored as given (two such
    rows could not be added up), ``"1_000"`` and padded text were read as numbers, and a JSON number went
    through ``str()``. Batteries serves a fee as text: digits, a point, and a minus sign when it is negative."""
    bad = item("u-bad", **{field: NOT_A_FEE_AS_BATTERIES_WRITES_ONE[name]})
    engine = await make_engine(supply=EngineSupply(usage_source=batteries([item("u1"), bad, item("u2")])))
    report = await engine.costs.sync_once()
    assert (report.rows_new, report.rows_set_aside) == (2, 1)
    assert await stored_ids(store) == ["u1", "u2"]
    kept = (None, "0.25") if field == "computed_fee_eth" else ("0.5", None)  # the fee it could read
    assert await set_aside(store) == [("u-bad", "unreadable_fee", *kept)]


FEES_BATTERIES_WRITES = {
    "a whole number": ("7", Decimal(7)),
    "zero": ("0", Decimal(0)),
    "one wei": ("0.000000000000000001", Decimal("0.000000000000000001")),
    "eighteen decimal places": ("0.000000039028439697", Decimal("0.000000039028439697")),
    "a large fee": ("123456789012345678901234567890.5", Decimal("123456789012345678901234567890.5")),
    "a negative amount": ("-0.25", Decimal("-0.25")),
}


@pytest.mark.parametrize("name", FEES_BATTERIES_WRITES)
async def test_every_fee_batteries_writes_is_still_read_exactly(name: str) -> None:
    """``internal/units/eth.go`` (v0.0.3): digits, then a point and up to 18 more when the amount has a
    fraction, and a minus sign for a negative one. No exponent, whatever the size."""
    text, amount = FEES_BATTERIES_WRITES[name]
    page = await batteries([item("u1", computed_fee_eth=text, computed_fee_usd=text)]).page(None)
    [row] = page.items
    assert (row.computed_fee_eth, row.computed_fee_usd, page.set_aside) == (amount, amount, ())


BLANK_IDS = {"spaces": "  ", "a tab": "\t", "a line break": "\n", "a no-break space": "\u00a0"}


@pytest.mark.parametrize("row_id", BLANK_IDS.values(), ids=BLANK_IDS)
async def test_a_usage_row_whose_id_is_blank_has_no_id(
    make_engine: EngineFactory, store: SqlStore, row_id: str
) -> None:
    """An id of spaces was stored as the row's id. It is no id, as ``""`` is none."""
    engine = await make_engine(supply=EngineSupply(usage_source=batteries([item(row_id), item("u1")])))
    report = await engine.costs.sync_once()
    assert (report.rows_new, report.rows_set_aside) == (1, 1)
    assert await stored_ids(store) == ["u1"]
    [(key, reason, fee_eth, fee_usd)] = await set_aside(store)
    assert key.startswith("sha256:") and (reason, fee_eth, fee_usd) == ("missing_id", "0.5", "0.25")


async def test_an_id_with_text_in_it_is_stored_as_received() -> None:
    """Nothing is trimmed: the id stored is the id Batteries served."""
    page = await batteries([item(" u 1 "), item("AZqk3vX7cQK1m0Jf9d2hLw")]).page(None)
    assert ([row.id for row in page.items], page.set_aside) == ([" u 1 ", "AZqk3vX7cQK1m0Jf9d2hLw"], ())
