"""The README's quick start, run (README.md#embed-it).

The region between the docs markers is the README's example; scripts/check_docs.py keeps them equal.
"""

from pathlib import Path

import pytest

# docs:start quick-start
from livepeer_builder import (
    ActorContext,
    BuilderEngine,
    Decorate,
    EngineSettings,
    EngineSupply,
    JobCost,
    JobRequest,
)


async def quick_start(
    settings: EngineSettings, supply: EngineSupply | None = None, decorate: Decorate | None = None
) -> JobCost:
    """Run one paid job for a customer and read what it cost. ``supply`` replaces parts and ``decorate`` wraps them."""
    settings.require_discovery_url()  # an engine does not start without it: refuse before the database is opened
    async with await BuilderEngine.create(settings, supply=supply, decorate=decorate) as engine:
        actor = ActorContext(
            actor_id="customer-42",
            permissions=frozenset({"discover", "jobs.run", "jobs.read", "costs.read"}),
        )
        job = JobRequest(app="livepeer-example/echo", path="/echo", payload={"x": 1})
        result = await engine.jobs.run(actor, job)
        return await engine.costs.for_job(actor, result.job.id)  # "pending" until Batteries reports it


# docs:end quick-start


async def test_the_quick_start_runs_a_job_and_reads_its_cost(tmp_path: Path) -> None:
    from livepeer_builder import generate_secret_key
    from livepeer_builder.testing import FakeAllocationManager, FakeUsageSource, ScriptedTransport, runner
    from livepeer_builder.testing.discovery import serve
    from tests.conftest import allot

    settings = EngineSettings.model_validate(
        {
            # A file, as a deployment runs it. A started engine on in-memory SQLite shares one connection
            # between the job and the background loops, which is not safe under concurrent writes.
            "database_url": f"sqlite+aiosqlite:///{tmp_path}/quick-start.db",
            "secret_key": generate_secret_key(),
            "log_json": False,
            "discovery_url": "http://discovery.test/discovery",
        }
    )
    install, client = serve((runner("livepeer-example/echo"),))
    offline = EngineSupply(
        transport=ScriptedTransport([ScriptedTransport.ok()]),
        allocation_manager=FakeAllocationManager(),
        usage_source=FakeUsageSource(),  # the cost loop runs: its first sync must not leave this job none (#33)
    )
    try:
        # An admin allots the customer once, before its first job: nothing else pays for it.
        desk = await BuilderEngine.create(settings, supply=offline, decorate=install)
        try:
            await allot(desk, ActorContext(actor_id="customer-42"))
        finally:
            await desk.stop()
        cost = await quick_start(settings, offline, install)
    finally:
        await client.aclose()
    assert cost.status == "pending"


async def test_the_quick_start_refuses_without_the_address_before_it_opens_the_database(
    tmp_path: Path,
) -> None:
    """``create`` opens the database and migrates it, and ``start()`` refuses after that. The quick start
    asks the settings first, so a deployment with no address fails with its database untouched (#154)."""
    database = tmp_path / "quick-start.db"
    settings = EngineSettings(database_url=f"sqlite+aiosqlite:///{database}", log_json=False)
    with pytest.raises(ValueError, match="LPB_DISCOVERY_URL is required to start the engine"):
        await quick_start(settings)
    assert not database.exists()
