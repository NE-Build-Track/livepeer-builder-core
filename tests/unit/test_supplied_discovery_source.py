"""#158: an answer of a supplied ``DiscoverySource`` that is not runners is a failed refresh.

A source the embedding application installs through ``decorate`` builds its answer itself, and nothing
checked what it handed over. Anything but a collection of ``Runner`` objects raised out of
``DiscoveryService.refresh``, as an ``AttributeError`` or a ``TypeError`` of Python's own: an engine did
not start on such a first answer, and a later one ended the refresh loop, so the catalog aged with no
``discovery_error`` until a restart. Such an answer is now what a source that raises is: the last good
snapshot stays, ``discovery_error`` is ``invalid_answer`` and ``discovery.failed`` says what came.
"""

import asyncio
import dataclasses
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from livepeer_builder import BuilderEngine, EngineParts
from livepeer_builder.contracts import Runner
from livepeer_builder.services.discovery import DiscoveryService
from livepeer_builder.services.policy import DefaultPolicy
from livepeer_builder.testing import FixedClock, make_engine

OBSERVED = datetime(2026, 10, 22, 17, 5, tzinfo=UTC)
KEPT = Runner(url="https://orch.example/apps/good", app="good/app", observed_at=OBSERVED)
NEXT = Runner(url="https://orch.example/apps/next", app="next/app", observed_at=OBSERVED)
RECORD = {"url": "https://orch.example/apps/a?token=hunter2", "app": "a/b"}  # a node record, not a Runner


def _one_at_a_time() -> Iterator[Runner]:
    yield KEPT


NOT_RUNNERS: dict[str, Any] = {
    "None": None,
    "a number": 7,
    "one runner, in no list": KEPT,
    "a mapping": {"runners": [KEPT]},
    "an empty mapping": {},
    "text": "good/app",
    "empty text": "",
    "empty bytes": b"",
    "a generator": _one_at_a_time,  # called for each test: a generator is read once
    "a list of records": [RECORD],
    "a runner and a record": (KEPT, RECORD),
    "a runner and None": [KEPT, None],
    "a list of lists": [[KEPT]],
}


class Supplied:
    """A source of the application's own: each fetch hands over the next of ``answers``, and the last
    one repeats."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.at_the_last = asyncio.Event()  # set once the last of ``answers`` has been handed over

    async def fetch(self) -> Any:
        if len(self.answers) == 1:
            self.at_the_last.set()
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return answer() if answer is _one_at_a_time else answer


@asynccontextmanager
async def supplied_engine(*answers: Any) -> AsyncIterator[BuilderEngine]:
    """A real engine that has refreshed once from the first of ``answers``."""

    def supply(parts: EngineParts) -> EngineParts:
        return dataclasses.replace(parts, discovery_source=Supplied(*answers))

    async with make_engine(decorate=supply) as engine:
        yield engine


def failures(records: Any) -> list[tuple[str, str]]:
    return [(item.code, item.error) for item in records if getattr(item, "event", "") == "discovery.failed"]


@pytest.mark.parametrize("name", NOT_RUNNERS)
async def test_an_answer_that_is_not_runners_keeps_the_last_good_snapshot(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    async with supplied_engine((KEPT,), NOT_RUNNERS[name]) as engine:
        before = engine.discovery.snapshot
        with caplog.at_level(logging.WARNING):
            after = await engine.discovery.refresh()
        health = engine.health()
        catalog = engine.discovery.offerings()
    assert [(offering.app, offering.runner_count) for offering in catalog] == [(KEPT.app, 1)]
    assert (after.runners, after.observed_at) == ((KEPT,), before.observed_at)
    assert after.last_attempt_at is not None
    assert (health.discovery_error, health.discovery_observed_at) == ("invalid_answer", before.observed_at)
    [(code, error)] = failures(caplog.records)
    assert code == "invalid_answer"
    assert "hunter2" not in error  # the text names what kind of thing came, and quotes none of it


@pytest.mark.parametrize("name", NOT_RUNNERS)
async def test_an_engine_starts_when_its_source_s_first_answer_is_not_runners(name: str) -> None:
    """As it starts when its source raises: with no snapshot, and the code in health."""
    async with supplied_engine(NOT_RUNNERS[name]) as engine:
        health = engine.health()
        assert engine.discovery.offerings() == []
    assert (health.discovery_error, health.discovery_observed_at, health.discovery_fresh) == (
        "invalid_answer",
        None,
        False,
    )


async def test_a_readable_answer_after_one_that_is_not_runners_clears_the_error() -> None:
    async with supplied_engine((KEPT,), [RECORD], (NEXT,)) as engine:
        await engine.discovery.refresh()
        assert engine.health().discovery_error == "invalid_answer"
        await engine.discovery.refresh()
        assert engine.health().discovery_error is None
        assert engine.discovery.snapshot.runners == (NEXT,)


async def test_the_refresh_loop_goes_on_after_an_answer_that_is_not_runners() -> None:
    """The loop awaits ``refresh`` and nothing else: an answer that raised out of it ended the loop, and
    the engine read its source no more."""
    source = Supplied(None, [RECORD], (NEXT,))
    discovery = DiscoveryService(source, DefaultPolicy(), FixedClock(), interval_s=0.001)
    stop = asyncio.Event()
    loop = asyncio.create_task(discovery.run(stop))
    read = asyncio.create_task(source.at_the_last.wait())
    await asyncio.wait({loop, read}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
    stop.set()
    read.cancel()
    await asyncio.gather(loop, read, return_exceptions=True)
    assert loop.exception() is None
    assert source.at_the_last.is_set()
    assert (discovery.snapshot.runners, discovery.snapshot.last_error) == ((NEXT,), None)


@pytest.mark.parametrize("empty", [(), [], frozenset()], ids=["tuple", "list", "set"])
async def test_a_collection_with_no_runner_in_it_is_still_an_empty_answer(empty: Any) -> None:
    async with supplied_engine((KEPT,), empty) as engine:
        after = await engine.discovery.refresh()
        assert engine.discovery.offerings() == []
    assert (after.runners, after.last_error, after.observed_at) == ((), None, after.last_attempt_at)


@pytest.mark.parametrize("kind", [tuple, list], ids=["tuple", "list"])
async def test_a_tuple_or_a_list_of_runners_is_an_answer(kind: Any) -> None:
    async with supplied_engine(kind([KEPT, NEXT])) as engine:
        snapshot = engine.discovery.snapshot
    assert (snapshot.runners, snapshot.last_error) == ((KEPT, NEXT), None)
