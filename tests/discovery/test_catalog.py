"""The catalog: the directory and each orchestrator's last good inventory, refreshed on intervals (#45)."""

import asyncio
import json
from collections.abc import Sequence

import pytest

from livepeer_builder.discovery import Catalog, Orchestrator, Probe

A, B = Orchestrator(url="https://a.example"), Orchestrator(url="https://b.example")


class Listed:
    def __init__(self, *orchestrators: Orchestrator) -> None:
        self.orchestrators_now: tuple[Orchestrator, ...] | Exception = orchestrators

    async def orchestrators(self) -> tuple[Orchestrator, ...]:
        if isinstance(self.orchestrators_now, Exception):
            raise self.orchestrators_now
        return self.orchestrators_now


class Answers:
    """Each orchestrator answers with runners named after a round number, or fails."""

    def __init__(self) -> None:
        self.down: set[str] = set()
        self.round = 0
        self.probed: list[list[str]] = []

    async def probe_all(self, orchestrators: Sequence[Orchestrator]) -> list[Probe]:
        self.round += 1
        self.probed.append([o.url for o in orchestrators])
        return [
            Probe(orchestrator=o, failure="failed")
            if o.url in self.down
            else Probe(
                orchestrator=o, entry={"address": o.url, "runners": [json.dumps({"round": self.round})]}
            )
            for o in orchestrators
        ]


def rounds(catalog: Catalog) -> dict[str, int]:
    return {entry.address: json.loads(entry.runners[0])["round"] for entry in catalog.inventory()}


async def test_not_ready_until_a_directory_loads_and_a_probe_pass_finishes() -> None:
    catalog = Catalog(Listed(A, B), Answers())
    assert not catalog.status().ready and catalog.inventory() == []
    await catalog.load_directory()
    assert not catalog.status().ready
    await catalog.probe()
    assert catalog.status().ready and rounds(catalog) == {A.url: 1, B.url: 1}


async def test_a_failing_orchestrator_keeps_its_last_inventory_until_the_limit_then_is_evicted() -> None:
    answers = Answers()
    catalog = Catalog(Listed(A, B), answers, max_failed_refreshes=2)
    await catalog.load_directory()
    await catalog.probe()
    answers.down = {B.url}
    await catalog.probe()
    assert rounds(catalog) == {A.url: 2, B.url: 1}  # first failure: B's round-1 inventory is kept
    await catalog.probe()
    assert rounds(catalog) == {A.url: 3}  # second consecutive failure: evicted
    answers.down = set()
    await catalog.probe()
    assert rounds(catalog) == {A.url: 4, B.url: 4}  # recovered


async def test_zero_allowed_failures_evicts_on_the_first() -> None:
    answers = Answers()
    catalog = Catalog(Listed(A), answers, max_failed_refreshes=0)
    await catalog.load_directory()
    await catalog.probe()
    answers.down = {A.url}
    await catalog.probe()
    assert catalog.inventory() == []


async def test_a_success_resets_the_failure_count() -> None:
    answers = Answers()
    catalog = Catalog(Listed(A), answers, max_failed_refreshes=2)
    await catalog.load_directory()
    for down in (False, True, False, True):  # never two failures in a row
        answers.down = {A.url} if down else set()
        await catalog.probe()
    assert A.url in rounds(catalog)


async def test_a_failed_directory_load_keeps_the_previous_directory() -> None:
    directory, answers = Listed(A, B), Answers()
    catalog = Catalog(directory, answers)
    await catalog.load_directory()
    directory.orchestrators_now = RuntimeError("rpc down")
    await catalog.load_directory()
    await catalog.probe()
    assert set(rounds(catalog)) == {A.url, B.url}
    assert catalog.status().directory_error == "RuntimeError"  # its text is logged, not shown on /readyz


async def test_an_orchestrator_that_leaves_the_directory_drops_out_at_once() -> None:
    directory = Listed(A, B)
    catalog = Catalog(directory, Answers())
    await catalog.load_directory()
    await catalog.probe()
    directory.orchestrators_now = (A,)
    await catalog.load_directory()
    assert set(rounds(catalog)) == {A.url}


async def test_run_probes_on_its_interval_and_retries_a_directory_it_never_had() -> None:
    directory, answers = Listed(A), Answers()
    directory.orchestrators_now = RuntimeError("not yet")
    catalog = Catalog(directory, answers)
    stop = asyncio.Event()
    task = asyncio.create_task(catalog.run(stop, directory_interval_s=3600, probe_interval_s=0.01))
    await asyncio.sleep(0.05)
    directory.orchestrators_now = (A,)  # the first load failed; it must not wait an hour to retry
    async with asyncio.timeout(2):
        while not catalog.status().ready:  # noqa: ASYNC110 - polls the catalog, which has no event to await
            await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert answers.round >= 1


def test_the_failure_limit_cannot_be_negative() -> None:
    with pytest.raises(ValueError, match="max_failed_refreshes"):
        Catalog(Listed(A), Answers(), max_failed_refreshes=-1)


async def test_an_orchestrator_that_rejoins_does_not_bring_back_an_old_inventory() -> None:
    directory, answers = Listed(A, B), Answers()
    catalog = Catalog(directory, answers)
    await catalog.load_directory()
    await catalog.probe()
    directory.orchestrators_now = (A,)
    await catalog.load_directory()
    directory.orchestrators_now = (A, B)
    await catalog.load_directory()
    answers.down = {B.url}
    await catalog.probe()
    assert set(rounds(catalog)) == {A.url}  # B is back in the directory but has not answered since


# -- PR #46 review ---------------------------------------------------------------------------------------


async def test_an_error_in_a_pass_is_logged_and_the_loop_keeps_ticking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Flaky(Answers):
        def __init__(self) -> None:
            super().__init__()
            self.broken = True

        async def probe_all(self, orchestrators: Sequence[Orchestrator]) -> list[Probe]:
            if self.broken:
                self.broken = False
                raise RuntimeError("a bug in a probe")
            return await super().probe_all(orchestrators)

    answers = Flaky()
    catalog = Catalog(Listed(A), answers)
    stop = asyncio.Event()
    task = asyncio.create_task(catalog.run(stop, directory_interval_s=3600, probe_interval_s=0.01))
    async with asyncio.timeout(2):
        while not catalog.status().ready:  # noqa: ASYNC110 - polls the catalog, which has no event to await
            await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert not task.cancelled() and task.exception() is None
    assert "discovery.refresh_failed" in {getattr(r, "event", None) for r in caplog.records}


async def test_a_directory_that_never_answers_times_out_and_probing_goes_on() -> None:
    class Stuck:
        async def orchestrators(self) -> tuple[Orchestrator, ...]:
            await asyncio.sleep(3600)
            return ()

    catalog = Catalog(Listed(A), Answers(), directory_timeout_s=0.05)
    await catalog.load_directory()
    catalog._directory = Stuck()  # the directory hangs from now on
    async with asyncio.timeout(2):
        await catalog.load_directory()
    assert catalog.status().directory_error == "TimeoutError" and catalog.status().orchestrators == 1


@pytest.mark.parametrize(
    ("error", "shown"),
    [
        (RuntimeError("https://rpc.example/v2/SECRET refused"), "RuntimeError"),
        (
            ValueError("not an orchestrator base URL: 'ftp://x'"),
            "ValueError: not an orchestrator base URL: 'ftp://x'",
        ),
    ],
    ids=["unknown error: its type only", "our own message"],
)
async def test_the_directory_error_shown_on_readyz_is_a_stable_reason(error: Exception, shown: str) -> None:
    """/readyz is unauthenticated (and public on Railway): no provider text, which may echo a key."""
    directory = Listed(A)
    directory.orchestrators_now = error
    catalog = Catalog(directory, Answers())
    await catalog.load_directory()
    assert catalog.status().directory_error == shown
