"""The README's quick start, run (README.md#embed-it).

The region between the docs markers is the README's example; scripts/check_docs.py keeps them equal.
"""

# docs:start quick-start
from livepeer_builder import ActorContext, BuilderEngine, EngineSettings, EngineSupply, JobCost, JobRequest


async def quick_start(settings: EngineSettings, supply: EngineSupply | None = None) -> JobCost:
    """Run one paid job for a customer and read what it cost. ``supply`` replaces parts (tests use it)."""
    async with await BuilderEngine.create(settings, supply=supply) as engine:
        actor = ActorContext(
            actor_id="customer-42",
            permissions=frozenset({"discover", "jobs.run", "jobs.read", "costs.read"}),
        )
        job = JobRequest(app="livepeer-example/echo", path="/echo", payload={"x": 1})
        result = await engine.jobs.run(actor, job)
        return await engine.costs.for_job(actor, result.job.id)  # "pending" until Batteries reports it


# docs:end quick-start


async def test_the_quick_start_runs_a_job_and_reads_its_cost() -> None:
    from livepeer_builder.adapters.static import (
        StaticDiscoverySource,  # here, so the region stays the README's
    )
    from livepeer_builder.testing import FakeAllocationManager, ScriptedTransport, runner

    settings = EngineSettings.model_validate(
        {
            "database_url": "sqlite+aiosqlite:///:memory:",
            "allocation_mode": "per_application",
            "shared_payment_key": "lpg_example",
            "shared_allocation_id": "alloc_example",
            "log_json": False,
        }
    )
    offline = EngineSupply(
        discovery_source=StaticDiscoverySource([runner("livepeer-example/echo")]),
        transport=ScriptedTransport([ScriptedTransport.ok()]),
        allocation_manager=FakeAllocationManager(),
        # No usage source, so the cost-sync loop that `async with` starts never runs mid-job. Its first
        # sync could otherwise store a projection of the job while it was still in flight (a known race,
        # fixed by PR #40), and on in-memory SQLite its writes would share the job's one connection.
        usage_source=None,
    )
    cost = await quick_start(settings, offline)
    assert cost.status == "pending"
