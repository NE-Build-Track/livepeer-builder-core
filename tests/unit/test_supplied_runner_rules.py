"""#158: a supplied ``DiscoverySource``'s runners are held to the reader's rules, by the reader's functions.

A source the embedding application installs through ``decorate`` builds its ``Runner`` objects itself.
The engine held them to the text rule (#142) and to nothing else the built-in reader asks of a record: a
runner with a URL the engine cannot call, or an ``app`` no job can name, was kept and served. And an
answer whose every runner broke the text rule emptied the catalog, where the reader fails the refresh.

Decided by Mike on 26 November 2026. A supplied runner is held to three rules, each the one function the
reader calls: its text, a URL the engine can call (``url``, and ``orchestrator_url`` when it has one)
and an ``app`` of 1 to 200 characters. One that breaks a rule is skipped and counted, as the reader's
record is, and an answer whose runners were all skipped fails the refresh. Two of the reader's rules are
not asked of it: its ``observed_at`` (D62 (3): a missing or unusable stamp is timed by the poll) and the
match of ``orchestrator_url`` with the runner URL's origin.
"""

import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from livepeer_builder.adapters.discovery_inventory import read_inventory
from livepeer_builder.contracts import Runner
from livepeer_builder.testing import FakeEngine
from tests.unit.discovery_wire import ALICE, document, record, skipped
from tests.unit.test_supplied_discovery_source import KEPT, NEXT, OBSERVED, failures, supplied_engine

HOST = "https://orch.example"
NODE_DUMP = Path(__file__).with_name("node_discovery_eee09c4.json").read_bytes()  # the pinned node's own
UNCALLABLE = {
    "another scheme": "ftp://orch.example/app",
    "no scheme": "orch.example/app",
    "empty": "",
    "userinfo": "https://user:secret@orch.example/app",
    "a query": f"{HOST}/app?model=a",
    "a fragment": f"{HOST}/app#a",
    "a space": f"{HOST}/my app",
    "a port past 65535": "https://orch.example:99999/app",
    "port 0": "https://orch.example:0/app",
    "a port that is no number": "https://orch.example:abc/app",
    "a host that is not ASCII": "https://caf\xe9.example/app",
    "a host aiohttp folds": "https://\u246027.0.0.1/app",
    "a backslash in the host": "https://orch.example\\evil/app",
    "no host": "https:///app",
}
CALLABLE = {
    "the IDNA name of a host that is not ASCII": "https://xn--caf-dma.example/app",
    "a host in capitals": "https://ORCH.example/app",
    "an IPv6 host": "http://[::1]:8935/app",
    "the last port": "https://orch.example:65535/app",
    "a no-break space in the path": f"{HOST}/my\u00a0app",
}
BAD_APPS = {"empty": "", "201 characters": "x" * 201}


def supplied(**fields: Any) -> Runner:
    """A runner as a supplied source builds one, with no reader in between."""
    return Runner(**{"url": f"{HOST}/apps/other", "app": "other/app", "observed_at": OBSERVED, **fields})


BROKEN = {
    **{f"url: {name}": supplied(url=url) for name, url in UNCALLABLE.items()},
    **{f"orchestrator_url: {name}": supplied(orchestrator_url=url) for name, url in UNCALLABLE.items()},
    **{f"app: {name}": supplied(app=app) for name, app in BAD_APPS.items()},
}


@pytest.mark.parametrize("name", BROKEN)
async def test_a_supplied_runner_that_breaks_a_rule_is_skipped_and_counted(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        async with supplied_engine((BROKEN[name], KEPT)) as engine:
            snapshot = engine.discovery.snapshot
            catalog = engine.discovery.offerings()
            error = engine.health().discovery_error
    assert (snapshot.runners, error) == ((KEPT,), None)
    assert [(offering.app, offering.runner_count) for offering in catalog] == [(KEPT.app, 1)]
    assert skipped(caplog.records) == [(0, 1)]
    assert failures(caplog.records) == []


@pytest.mark.parametrize("name", CALLABLE)
async def test_a_supplied_runner_with_a_url_the_engine_can_call_is_kept_as_written(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    written = supplied(url=CALLABLE[name], orchestrator_url=CALLABLE[name])
    with caplog.at_level(logging.WARNING):
        async with supplied_engine((written,)) as engine:
            assert engine.discovery.snapshot.runners == (written,)
    assert skipped(caplog.records) == []


async def test_a_supplied_runner_s_app_of_200_characters_is_kept() -> None:
    longest = supplied(app="x" * 200)
    async with supplied_engine((longest,)) as engine:
        assert engine.discovery.runners(ALICE, "x" * 200) == [longest]


async def test_a_supplied_runner_s_orchestrator_url_may_be_on_another_origin() -> None:
    """The reader keeps an entry's ``address`` only when its origin is the runner URL's: the address is a
    claim read off the network. A source of the application's own is believed."""
    elsewhere = supplied(orchestrator_url="https://payments.example:8935")
    async with supplied_engine((elsewhere,)) as engine:
        assert engine.discovery.snapshot.runners == (elsewhere,)


@pytest.mark.parametrize(
    "stamp",
    [None, OBSERVED.replace(tzinfo=None), OBSERVED + timedelta(days=365)],
    ids=["no stamp", "a stamp with no offset", "a stamp from next year"],
)
async def test_a_supplied_runner_s_stamp_is_not_checked(stamp: Any, caplog: pytest.LogCaptureFixture) -> None:
    """D62 (3) stands: the catalog times a rate whose stamp it cannot use by the poll that read it."""
    written = supplied(observed_at=stamp)
    with caplog.at_level(logging.WARNING):
        async with supplied_engine((written,)) as engine:
            assert engine.discovery.snapshot.runners == (written,)
    assert skipped(caplog.records) == []


# -- every runner skipped: an answer the engine could not read, as the reader's is (#114, C28) ---------

NONE_KEPT = {
    "one rule each": (supplied(url="ftp://orch.example/app"), supplied(app=""), supplied(mode="a\ud800")),
    "the text rule alone (#142)": (supplied(app="a\ud800b"), supplied(metadata="a\x00b")),
    "one runner": (supplied(app="x" * 201),),
}


@pytest.mark.parametrize("name", NONE_KEPT)
async def test_an_answer_whose_every_runner_was_skipped_keeps_the_last_good_snapshot(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    dropped = len(NONE_KEPT[name])
    async with supplied_engine((KEPT,), NONE_KEPT[name], (NEXT,)) as engine:
        before = engine.discovery.snapshot
        with caplog.at_level(logging.WARNING):
            after = await engine.discovery.refresh()
        health = engine.health()
        catalog = engine.discovery.offerings()
        await engine.discovery.refresh()
        assert (engine.discovery.snapshot.runners, engine.health().discovery_error) == ((NEXT,), None)
    assert [offering.app for offering in catalog] == [KEPT.app]
    assert (after.runners, after.observed_at) == ((KEPT,), before.observed_at)
    assert (health.discovery_error, health.discovery_observed_at) == ("invalid_answer", before.observed_at)
    assert skipped(caplog.records) == [(0, dropped)]
    [(code, error)] = failures(caplog.records)
    assert code == "invalid_answer"
    assert ("0 entries" in error, f"{dropped} runners" in error) == (True, True)  # the reader's words


async def test_a_first_answer_whose_every_runner_was_skipped_leaves_no_snapshot() -> None:
    async with supplied_engine(NONE_KEPT["one rule each"]) as engine:
        health = engine.health()
        assert engine.discovery.offerings() == []
    assert (health.discovery_error, health.discovery_observed_at) == ("invalid_answer", None)


# -- one rule, two callers -----------------------------------------------------------------------------


def read_by_the_reader(url: str, app: str) -> bool:
    """Whether the built-in reader keeps a record with this ``url`` and ``app``."""
    body = document(record(url=url, app=app))
    try:
        return len(read_inventory(body)) == 1
    except ValueError:  # its only record was skipped
        return False


async def kept_from_a_supplied_source(url: str, app: str) -> bool:
    async with supplied_engine((supplied(url=url, app=app),)) as engine:
        return len(engine.discovery.snapshot.runners) == 1


@pytest.mark.parametrize("url", [*UNCALLABLE.values(), *CALLABLE.values()], ids=[*UNCALLABLE, *CALLABLE])
async def test_the_reader_and_a_supplied_source_agree_on_every_url(url: str) -> None:
    assert await kept_from_a_supplied_source(url, "other/app") is read_by_the_reader(url, "other/app")


@pytest.mark.parametrize(
    "app", ["", "x", "x" * 200, "x" * 201, "a\ud800b", "a\nb", "mod\xe8le/\u56fe\u50cf v2"]
)
async def test_the_reader_and_a_supplied_source_agree_on_every_app(app: str) -> None:
    url = f"{HOST}/apps/other"
    assert await kept_from_a_supplied_source(url, app) is read_by_the_reader(url, app)


async def test_what_the_reader_read_passes_through_a_supplied_source_whole(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A wrapper around the built-in reader is a supplied source too: every runner the reader keeps is
    kept again, so the pinned node document reads the same through one."""
    read = read_inventory(NODE_DUMP)
    assert read
    with caplog.at_level(logging.WARNING):
        async with supplied_engine(read) as engine:
            assert engine.discovery.snapshot.runners == read
    assert skipped(caplog.records) == []


# -- FakeEngine: the runners an application's own tests see are the ones the engine would keep ---------


@pytest.mark.parametrize("name", ["url: another scheme", "orchestrator_url: a query", "app: 201 characters"])
async def test_the_fake_engine_lists_the_runners_the_engine_keeps(name: str) -> None:
    broken = BROKEN[name].model_copy(update={"app": BROKEN[name].app or KEPT.app})
    given = (broken, KEPT, supplied(app=KEPT.app, mode="a\ud800"))
    fake = FakeEngine(runners=given)
    async with supplied_engine(given) as engine:
        for app in {runner.app for runner in given}:
            assert fake.discovery.candidates(app) == engine.discovery.candidates(app)
    assert fake.discovery.candidates(KEPT.app) == [KEPT]


def test_the_document_helper_writes_what_the_reader_reads() -> None:
    assert json.loads(document(record()))[0]["runners"][0]["app"] == "good/app"
    assert read_by_the_reader(f"{HOST}/apps/other", "other/app")
