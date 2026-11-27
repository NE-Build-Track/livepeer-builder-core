"""#158: a usage fee is measured by its plain decimal form, whatever its ``UsageSource``.

The store keeps a fee in a 64-character column, and cost sync set aside a row whose fee was longer. It
measured the fee as Python writes a decimal, and Python writes ``9E+999999`` in nine characters: two such
applied rows were stored, and their sum raised ``decimal.Overflow`` in the sync, in a job's cost, in the
summary and in reconciliation. The Batteries reader takes no exponent, so this came from another source:
a supplied one, or a clearinghouse's usage page read by an engine in its ``work`` role. The fee is now
measured as its digits written out, with no exponent, which is how Batteries writes one.
"""

import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from livepeer_builder.adapters.clearinghouse_tenant import USAGE_PATH
from livepeer_builder.contracts import ActorContext, UsagePage, UsageRow
from livepeer_builder.contracts.costs import plain_length, set_aside_record, storable_fee, unstorable_field
from livepeer_builder.services.usage_intake import sort_page
from livepeer_builder.store import SqlStore
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FakeUsageSource, FixedClock
from tests.conftest import EngineFactory
from tests.unit.payment_ports import open_ports
from tests.unit.test_costs import booked, ended_paid_job, row
from tests.unit.test_usage_set_aside import batteries, item, set_aside, stored_ids, usage_row

PAST_ANY_AMOUNT = "9E+999999"


@pytest.mark.parametrize("fee", ["computed_fee_eth", "computed_fee_usd"])
async def test_two_applied_rows_with_a_fee_past_any_amount_are_set_aside_and_no_sum_overflows(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, store: SqlStore, clock: FixedClock,
    usage: FakeUsageSource, fee: str,
) -> None:  # fmt: skip
    """From a supplied usage source, on one matched session of a paid job. The sync ends, both rows are
    set aside for the fee the store cannot keep, and every read of the job's cost answers."""
    engine, job, allocation = await ended_paid_job(make_engine, admin, store, clock)
    huge = {fee: Decimal(PAST_ANY_AMOUNT)}
    usage.rows = [
        row("u1", "sess-1", "0.5", allocation=allocation, manifest="m-1").model_copy(update=huge),
        row("u2", "sess-1", "0.5", allocation=allocation, manifest="m-1").model_copy(update=huge),
        row("u3", "sess-1", "0.25", usd="0.25", allocation=allocation, manifest="m-1"),
    ]

    report = await engine.costs.sync_once()

    assert (report.rows_seen, report.rows_new, report.rows_set_aside) == (3, 1, 2)
    assert engine.costs.last_error is None and await stored_ids(store) == ["u3"]
    assert [(key, reason) for key, reason, _, _ in await set_aside(store)] == [
        ("u1", f"unstorable_{fee}"),
        ("u2", f"unstorable_{fee}"),
    ]
    async with store.engine.connect() as conn:  # the record keeps no fee the table cannot hold
        kept = (await conn.execute(select(getattr(t.usage_set_aside.c, fee)))).scalars().all()
    assert kept == [None, None]
    assert (await engine.costs.for_job(alice, job.id)).fees.fee_eth == Decimal("0.25")
    summary = await engine.costs.summary(alice)
    assert summary.by_class["delivered"].fee_eth == Decimal("0.25")
    assert booked(await engine.costs.reconcile(admin, "default", "alice")) == Decimal("0.25")


async def test_a_clearinghouse_s_usage_page_with_such_a_fee_is_read_and_its_rows_set_aside(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An engine in its ``work`` role reads its usage from the clearinghouse, whose page is the port's
    own (``UsagePage``): a fee there is a decimal in any form the type reads. No engine of this
    repository serves one with an exponent; another clearinghouse can."""
    async with open_ports("remote") as ports:
        assert ports.transport is not None
        carried = ports.transport.handle_async_request
        served = usage_row("u1").model_dump(mode="json")
        page = {
            "items": [served | {"computed_fee_eth": PAST_ANY_AMOUNT}, served | {"id": "u2"}],
            "next_cursor": "",
            "set_aside": [],
        }

        async def its_usage(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path.endswith(USAGE_PATH):
                return httpx.Response(200, content=json.dumps(page).encode(), request=request)
            return await carried(request)

        monkeypatch.setattr(ports.transport, "handle_async_request", its_usage)

        report = await ports.engine.costs.sync_once()

        assert (report.rows_new, report.rows_set_aside) == (1, 1)
        store = ports.engine.store
        assert isinstance(store, SqlStore) and await stored_ids(store) == ["u2"]
        assert await set_aside(store) == [("u1", "unstorable_computed_fee_eth", None, "0.25")]


WRITTEN_OUT = {  # a fee, and how many characters its digits take when written out with no exponent
    "0.000000039": 11,
    "3.9E-8": 11,  # the same amount: how Python writes it is not how it is measured
    "1E-18": 20,
    "7": 1,
    "-0.25": 5,
    "1.50": 4,
    "0": 1,
    "0E+5": 1,
    "-0": 2,
    "1E+5": 6,
    "12345678901234567890E+1": 21,
    "0E-3": 5,
    PAST_ANY_AMOUNT: 1_000_000,
    "1E-999999": 1_000_001,
    "9E+999999999999999999": 1_000_000_000_000_000_000,  # counted, never written out
}


@pytest.mark.parametrize("fee", WRITTEN_OUT)
def test_a_fee_s_plain_length_is_that_of_its_digits_written_out(fee: str) -> None:
    assert plain_length(Decimal(fee)) == WRITTEN_OUT[fee]
    if WRITTEN_OUT[fee] < 100:
        assert plain_length(Decimal(fee)) == len(format(Decimal(fee), "f"))


KEPT = {
    "a small fee Python writes with an exponent": Decimal("0.000000039"),
    "one wei": Decimal("0.000000000000000001"),
    "64 digits": Decimal("1" * 64),
    "64 characters with a fraction": Decimal("0." + "1" * 62),
    "a one and 63 zeros, from a source that wrote an exponent": Decimal("1E+63"),
    "62 decimal places": Decimal("1E-62"),
}
SET_ASIDE = {
    "an exponent past any amount": Decimal(PAST_ANY_AMOUNT),
    "65 digits": Decimal("1" * 65),
    "a one and 64 zeros": Decimal("1E+64"),
    "63 decimal places": Decimal("1E-63"),
    "a fraction past any amount": Decimal("1E-999999"),
    # its digits fit, and the form the store writes (1.11...E+63) does not
    "64 digits the store writes with an exponent": Decimal("1" * 63 + "E+1"),
}


@pytest.mark.parametrize("name", KEPT)
def test_a_fee_whose_digits_fit_the_column_is_kept(name: str) -> None:
    kept = usage_row("u1", computed_fee_eth=KEPT[name], computed_fee_usd=KEPT[name])
    assert unstorable_field(kept) is None
    assert sort_page(UsagePage(items=(kept,))) == ([kept], [])
    record = set_aside_record("unknown_status", {}, row_id="u1", fee_eth=KEPT[name], fee_usd=None)
    assert record.computed_fee_eth == KEPT[name]


@pytest.mark.parametrize("name", SET_ASIDE)
def test_a_fee_whose_digits_do_not_fit_the_column_sets_its_row_aside(name: str) -> None:
    eth, usd = (
        usage_row("u1", computed_fee_eth=SET_ASIDE[name]),
        usage_row("u2", computed_fee_usd=SET_ASIDE[name]),
    )
    assert unstorable_field(eth) == "computed_fee_eth" and unstorable_field(usd) == "computed_fee_usd"
    rows, aside = sort_page(UsagePage(items=(eth, usd)))
    assert rows == [] and [record.reason for record in aside] == [
        "unstorable_computed_fee_eth",
        "unstorable_computed_fee_usd",
    ]
    assert (aside[0].computed_fee_eth, aside[0].computed_fee_usd) == (None, Decimal("0.25"))


@pytest.mark.parametrize(
    ("fee", "applied"),
    [("0.000000039", True), ("0.000000000000000001", True), ("1" * 64, True), ("0." + "1" * 62, True),
     ("1" * 65, False), ("0." + "1" * 63, False)],
)  # fmt: skip
async def test_what_batteries_sends_is_applied_as_before(
    make_engine: EngineFactory, store: SqlStore, fee: str, applied: bool
) -> None:
    """Batteries writes a fee as digits, with no exponent (the reader takes no other), so its plain form
    is the text it sent: a fee of up to 64 characters is applied, and a longer one was set aside before."""
    from livepeer_builder import EngineSupply

    source = batteries([item("u1", computed_fee_eth=fee), item("u2")])
    engine = await make_engine(supply=EngineSupply(usage_source=source))
    report = await engine.costs.sync_once()
    assert (report.rows_new, report.rows_set_aside) == ((2, 0) if applied else (1, 1))
    assert await stored_ids(store) == (["u1", "u2"] if applied else ["u2"])
    async with store.engine.connect() as conn:
        fees = dict((await conn.execute(select(t.usage_rows.c.id, t.usage_rows.c.computed_fee_eth))).all())
    assert Decimal(fees["u1"]) == Decimal(fee) if applied else "u1" not in fees


def test_a_usage_row_takes_no_fee_that_is_no_number() -> None:
    """``NaN`` and ``Infinity`` never reach the measure: the row's own type refuses them. They have no
    digits to count, and no record keeps one."""
    for no_number in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError, match="finite"):
            plain_length(Decimal(no_number))
        assert not storable_fee(Decimal(no_number))
        with pytest.raises(ValueError, match="finite"):
            UsageRow(**(usage_row("u1").model_dump() | {"computed_fee_eth": Decimal(no_number)}))
