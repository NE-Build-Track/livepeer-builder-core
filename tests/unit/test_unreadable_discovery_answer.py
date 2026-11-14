"""#114 C28: a discovery answer with records and no readable runner is a failed refresh.

It read as an empty inventory and replaced the last good snapshot, so one bad publish emptied the catalog
for every application. Now the snapshot stays, ``discovery_error`` is ``invalid_answer`` and the counts
are logged. A truly empty answer still replaces the snapshot, and so does one with any readable runner:
there is no threshold on the share of unreadable records.

These tests carry into ISS-48 (see ``discovery_wire``).
"""

import json
import logging
from typing import Any

import pytest

from livepeer_builder import EngineSupply
from livepeer_builder.testing import FixedClock
from tests.unit.discovery_wire import ORCHESTRATOR, document, record, skipped, wire_engine

GOOD = document(record())
NO_STAMP = {"url": f"{ORCHESTRATOR}/apps/a", "app": "a/b"}
UNREADABLE = {
    "every runner unreadable": document(NO_STAMP, record(app="")),
    "one record, not a runner": document(7),
    "every entry unreadable": json.dumps([None, {"address": "ftp://orch.example", "runners": [record()]}]),
    "runners is not a list": json.dumps([{"address": ORCHESTRATOR, "runners": {"url": "x"}}]),
    # ``nodes`` and no ``address``, as a metadata entry has, but with records no address can read
    "nodes with runners": json.dumps([{"nodes": [], "runners": [NO_STAMP, record()]}]),
    "nodes with runners, not a list": json.dumps([{"nodes": [], "runners": {"url": "x"}}]),
}
COUNTS = {
    "every runner unreadable": (0, 2),
    "one record, not a runner": (0, 1),
    "every entry unreadable": (2, 0),
    "runners is not a list": (1, 0),
    "nodes with runners": (1, 0),
    "nodes with runners, not a list": (1, 0),
}
EMPTY = {
    "an empty list": b"[]",
    "a metadata entry": json.dumps(
        [{"nodes": [{"url": ORCHESTRATOR, "queried_at": "2026-10-22T17:05:00Z"}]}]
    ),
    "an orchestrator with no runners": document(),
    "a metadata entry with an empty runners list": json.dumps([{"nodes": [], "runners": []}]),
}


def _bytes(body: str | bytes) -> bytes:
    return body if isinstance(body, bytes) else body.encode()


def _failures(records: Any) -> list[tuple[str, str]]:
    return [(item.code, item.error) for item in records if getattr(item, "event", "") == "discovery.failed"]


@pytest.mark.parametrize("name", UNREADABLE)
async def test_an_answer_with_records_and_no_readable_runner_keeps_the_last_good_snapshot(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    async with wire_engine(GOOD, _bytes(UNREADABLE[name])) as engine:
        before = engine.discovery.snapshot
        with caplog.at_level(logging.WARNING):
            await engine.discovery.refresh()
        after = engine.discovery.snapshot
        health = engine.health()
        catalog = engine.discovery.offerings()
    assert [offering.app for offering in catalog] == ["good/app"]
    assert (after.runners, after.observed_at) == (before.runners, before.observed_at)
    assert (health.discovery_error, health.discovery_observed_at) == ("invalid_answer", before.observed_at)
    # the counts are logged: with the skip, and in the failure's own text
    entries, runners = COUNTS[name]
    assert skipped(caplog.records) == [(entries, runners)]
    [(code, error)] = _failures(caplog.records)
    assert code == "invalid_answer"
    assert (f"{entries} entries" in error, f"{runners} runners" in error) == (True, True)


@pytest.mark.parametrize("name", EMPTY)
async def test_a_truly_empty_answer_still_replaces_the_snapshot(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    async with wire_engine(GOOD, _bytes(EMPTY[name])) as engine:
        assert [offering.app for offering in engine.discovery.offerings()] == ["good/app"]
        with caplog.at_level(logging.WARNING):
            await engine.discovery.refresh()
        after = engine.discovery.snapshot
        assert engine.discovery.offerings() == []
    assert (after.runners, after.last_error, after.observed_at) == ((), None, after.last_attempt_at)
    assert _failures(caplog.records) == []


async def test_one_readable_runner_among_unreadable_records_replaces_the_snapshot() -> None:
    """No threshold: 1 readable runner in 50 records is an answer."""
    mostly_bad = document(*([NO_STAMP] * 49), record(app="other/app"))
    async with wire_engine(GOOD, mostly_bad) as engine:
        await engine.discovery.refresh()
        assert [offering.app for offering in engine.discovery.offerings()] == ["other/app"]
        assert engine.health().discovery_error is None


async def test_the_first_answer_being_unreadable_leaves_no_snapshot() -> None:
    async with wire_engine(_bytes(UNREADABLE["every runner unreadable"])) as engine:
        health = engine.health()
        assert engine.discovery.offerings() == []
    assert (health.discovery_error, health.discovery_observed_at, health.discovery_fresh) == (
        "invalid_answer",
        None,
        False,
    )


async def test_the_kept_snapshot_ages_as_for_any_failed_refresh() -> None:
    clock = FixedClock()
    unreadable = _bytes(UNREADABLE["every runner unreadable"])
    async with wire_engine(GOOD, unreadable, supply=EngineSupply(clock=clock)) as engine:
        interval = engine.settings.discovery_interval_s
        clock.advance(interval)
        await engine.discovery.refresh()
        (offering,) = engine.discovery.offerings()
        assert (engine.health().discovery_fresh, offering.stale) == (True, False)
        clock.advance(interval * 2 + 1)  # past three intervals since the last good answer
        await engine.discovery.refresh()
        (offering,) = engine.discovery.offerings()
        assert (engine.health().discovery_fresh, offering.stale) == (False, True)
        assert engine.health().discovery_error == "invalid_answer"


async def test_a_readable_answer_after_an_unreadable_one_clears_the_error() -> None:
    unreadable = _bytes(UNREADABLE["every runner unreadable"])
    async with wire_engine(GOOD, unreadable, document(record(app="next/app"))) as engine:
        await engine.discovery.refresh()
        assert engine.health().discovery_error == "invalid_answer"
        await engine.discovery.refresh()
        assert engine.health().discovery_error is None
        assert [offering.app for offering in engine.discovery.offerings()] == ["next/app"]
