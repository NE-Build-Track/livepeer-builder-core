"""ISS-26: the retention purge and an async job's stored result, through the engine.

``LPB_RETENTION_RESULTS_DAYS`` (7 by default, D69 (6)) stamps when a stored result's bytes go; the purge
deletes them then, and the job keeps its record. The default turns the retention purge on in every engine
that runs jobs, so it must not add a write at start where none was before: with only results retention
on, the first purge runs one ``LPB_RETENTION_INTERVAL_S`` after start. An expired result is never served
in the meantime (``read_result`` reads ``expires_at``), so only its space waits.
"""

import asyncio
import logging

import pytest
from sqlalchemy import func, select

from livepeer_builder import BuilderEngine, EngineSettings, EngineSupply
from livepeer_builder.contracts import ActorContext
from livepeer_builder.errors import ResultExpired
from livepeer_builder.store import SqlStore
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FixedClock, ScriptedTransport
from tests.conftest import EngineFactory, allot
from tests.unit.test_async_results import PNG, finished, image

MEMORY = "sqlite+aiosqlite:///:memory:"


async def stored_results(engine: BuilderEngine) -> int:
    async with engine.store.engine.connect() as conn:  # type: ignore[attr-defined]
        return int((await conn.execute(select(func.count()).select_from(t.job_results))).scalar_one())


async def test_the_purge_deletes_an_expired_result_and_reports_it(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport, clock: FixedClock
) -> None:
    engine = await make_engine(retention_results_days=2)
    await allot(engine, alice)
    transport.steps = [image(), image()]
    expiring = await finished(engine, alice)
    clock.advance(86400)
    fresher = await finished(engine, alice)

    clock.advance(86400)  # the first result is two days old, the second one
    report = await engine.purge()
    assert await stored_results(engine) == 1
    assert report.results_deleted == 1
    assert await engine.store.read_result(fresher, clock.now()) == PNG
    with pytest.raises(ResultExpired):
        await engine.jobs.result(alice, expiring)
    assert (await engine.jobs.get(alice, expiring)).state == "succeeded"  # the job stays
    assert report.jobs_deleted == 0  # LPB_RETENTION_JOBS_DAYS is not set


def purges(caplog: pytest.LogCaptureFixture) -> int:
    return len([r for r in caplog.records if getattr(r, "event", None) == "store.purged"])


async def until_purged(caplog: pytest.LogCaptureFixture, count: int) -> None:
    """Wait for ``count`` purges: a condition, with a bound of five seconds, never a length of time."""
    for _ in range(500):
        if purges(caplog) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{purges(caplog)} purges, not {count}")


@pytest.mark.parametrize(
    ("retention", "at_start"),
    [
        ({}, False),  # results retention alone, the default: the first purge waits an interval
        ({"retention_results_days": None}, False),  # no retention setting at all: it waits one too
        ({"retention_jobs_days": 30}, True),  # job retention purges at start, as before
        ({"retention_usage_days": 30}, True),
    ],
)
async def test_a_purge_runs_at_start_only_where_one_did_before(
    store: SqlStore,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
    retention: dict[str, int | None],
    at_start: bool,
) -> None:
    settings = EngineSettings.model_validate(
        {
            "database_url": MEMORY,
            "retention_interval_s": 3600,
            "log_json": False,
            "discovery_url": "http://127.0.0.1:9/discovery",
            **retention,
        }
    )
    engine = await BuilderEngine.create(settings, supply=EngineSupply(store=store, clock=clock))
    with caplog.at_level(logging.INFO, logger="livepeer_builder.runtime.engine"):
        await engine.start()
        if at_start:
            await until_purged(caplog, 1)
        else:
            await asyncio.sleep(0.05)  # what a purge at start would have taken, many times over
        await engine.stop()
    assert purges(caplog) == (1 if at_start else 0)


async def test_results_retention_alone_purges_every_interval(
    store: SqlStore, clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    settings = EngineSettings(
        database_url=MEMORY,
        retention_interval_s=0.02,
        log_json=False,
        discovery_url="http://127.0.0.1:9/discovery",
    )
    assert settings.retention_results_days == 7 and settings.retention_jobs_days is None
    engine = await BuilderEngine.create(settings, supply=EngineSupply(store=store, clock=clock))
    with caplog.at_level(logging.INFO, logger="livepeer_builder.runtime.engine"):
        await engine.start()
        await until_purged(caplog, 3)
        await engine.stop()


async def test_a_result_that_has_its_time_is_deleted_when_no_retention_setting_is_on(
    make_engine: EngineFactory, alice: ActorContext, transport: ScriptedTransport, clock: FixedClock
) -> None:
    """The result was stored while results were kept for two days, so it says when it goes. Then every
    retention setting was turned off. The retention loop ended at once with no setting on, so nothing
    deleted the bytes once they were due, though no reader was served them any more. The loop runs in
    every engine that runs jobs: what already has its time is deleted when that time comes."""
    before = await make_engine(retention_results_days=2)
    await allot(before, alice)
    transport.steps = [image()]
    job_id = await finished(before, alice)
    assert await stored_results(before) == 1

    settings = {"retention_results_days": None, "retention_interval_s": 0.02}
    engine = await make_engine(**settings)  # on the same database, with no retention setting on
    assert (engine.settings.retention_jobs_days, engine.settings.retention_usage_days) == (None, None)
    clock.advance(2 * 86400)
    with pytest.raises(ResultExpired):  # past its time: served to nobody, and still stored
        await engine.jobs.result(alice, job_id)
    assert await stored_results(engine) == 1

    await engine.start()
    for _ in range(500):  # a condition, with a bound of five seconds
        if await stored_results(engine) == 0:
            break
        await asyncio.sleep(0.01)

    assert await stored_results(engine) == 0
    assert (await engine.jobs.get(alice, job_id)).state == "succeeded"  # the job keeps its record
