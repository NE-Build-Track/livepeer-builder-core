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


@pytest.mark.parametrize(
    ("retention", "at_start"),
    [
        ({}, False),  # results retention alone, the default: the first purge waits an interval
        ({"retention_jobs_days": 30}, True),  # job retention purges at start, as before
        ({"retention_usage_days": 30}, True),
    ],
)
async def test_a_purge_runs_at_start_only_where_one_did_before(
    store: SqlStore,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
    retention: dict[str, int],
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
        await asyncio.sleep(0.05)
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
        await asyncio.sleep(0.15)  # about seven intervals
        await engine.stop()
    assert purges(caplog) >= 3
