"""ISS-08 (D44): one answer rule on every job and cost route.

The operation's permission is checked first, before any lookup, so a missing permission is
``AccessDenied`` (403) and says nothing about any job. Ownership comes second: another actor's job, or a
missing one, is ``NotFound`` (404).
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from livepeer_builder import BuilderEngine
from livepeer_builder.contracts import ActorContext, JobRequest
from livepeer_builder.errors import AccessDenied, NotFound, NotImplementedInMilestone
from livepeer_builder.testing import ScriptedTransport
from tests.conftest import EngineFactory

APP = "example/echo"
EPOCH, LATER = datetime(2026, 1, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)
Call = Callable[[BuilderEngine, ActorContext, UUID], Awaitable[Any]]

# Each job or cost route as the engine call behind it, with the permission it needs.
ROUTES: dict[str, tuple[str, Call]] = {
    "get": ("jobs.read", lambda e, a, j: e.jobs.get(a, j)),
    "result": ("jobs.read", lambda e, a, j: e.jobs.result(a, j)),
    "report_usage": ("jobs.run", lambda e, a, j: e.jobs.report_usage(a, j, {"tokens": 1})),
    "cost": ("costs.read", lambda e, a, j: e.costs.for_job(a, j)),
}
LISTS: dict[str, tuple[str, Callable[[BuilderEngine, ActorContext], Awaitable[Any]]]] = {
    "list_jobs": ("jobs.read", lambda e, a: e.jobs.list_jobs(a)),
    "submit": ("jobs.run", lambda e, a: e.jobs.submit(a, JobRequest(app=APP, mode="async"))),
    "projections": ("costs.read", lambda e, a: e.costs.recent(a)),
    "summary": ("costs.read", lambda e, a: e.costs.summary(a, EPOCH, LATER)),
    "allowance": ("costs.read", lambda e, a: e.payments.allowance(a)),
}


def without(actor: ActorContext, permission: str) -> ActorContext:
    return actor.model_copy(update={"permissions": actor.permissions - {permission}})


async def engine_with_job(
    make_engine: EngineFactory, transport: ScriptedTransport, actor: ActorContext
) -> tuple[BuilderEngine, UUID]:
    engine = await make_engine(allocation_mode="per_application", shared_payment_key="lpg_shared")
    transport.steps = [ScriptedTransport.ok()]
    return engine, (await engine.jobs.run(actor, JobRequest(app=APP))).job.id


@pytest.mark.parametrize("route", ROUTES)
async def test_a_missing_permission_is_403_before_any_lookup(
    route: str, make_engine: EngineFactory, transport: ScriptedTransport, alice: ActorContext
) -> None:
    permission, call = ROUTES[route]
    engine, job_id = await engine_with_job(make_engine, transport, alice)
    for target in (job_id, uuid4()):  # the caller's own job, and no job at all: the same answer
        with pytest.raises(AccessDenied):
            await call(engine, without(alice, permission), target)


@pytest.mark.parametrize("route", LISTS)
async def test_a_missing_permission_is_403_on_the_list_routes(
    route: str, make_engine: EngineFactory, alice: ActorContext
) -> None:
    permission, call = LISTS[route]
    engine = await make_engine(allocation_mode="per_application", shared_payment_key="lpg_shared")
    with pytest.raises(AccessDenied):
        await call(engine, without(alice, permission))


@pytest.mark.parametrize("route", ROUTES)
async def test_another_actors_job_and_a_missing_one_are_404(
    route: str,
    make_engine: EngineFactory,
    transport: ScriptedTransport,
    alice: ActorContext,
    bob: ActorContext,
) -> None:
    _, call = ROUTES[route]
    engine, job_id = await engine_with_job(make_engine, transport, alice)
    for target in (job_id, uuid4()):
        with pytest.raises(NotFound):
            await call(engine, bob, target)


async def test_a_stored_result_is_501_only_for_the_owner(
    make_engine: EngineFactory, transport: ScriptedTransport, alice: ActorContext
) -> None:
    engine, job_id = await engine_with_job(make_engine, transport, alice)
    with pytest.raises(NotImplementedInMilestone):
        await engine.jobs.result(alice, job_id)


async def test_a_usage_report_needs_jobs_run_and_not_jobs_read(
    make_engine: EngineFactory, transport: ScriptedTransport, alice: ActorContext
) -> None:
    engine, job_id = await engine_with_job(make_engine, transport, alice)
    reported = await engine.jobs.report_usage(without(alice, "jobs.read"), job_id, {"tokens": 3})
    assert reported.usage is not None and reported.usage.quantities == {"tokens": 3}


async def test_an_admin_lists_every_actors_jobs_in_its_application(
    make_engine: EngineFactory,
    transport: ScriptedTransport,
    admin: ActorContext,
    alice: ActorContext,
    bob: ActorContext,
) -> None:
    engine, alices = await engine_with_job(make_engine, transport, alice)
    carol = bob.model_copy(update={"actor_id": "carol", "application_id": "app-2"})
    transport.steps = [ScriptedTransport.ok(), ScriptedTransport.ok()]
    bobs = (await engine.jobs.run(bob, JobRequest(app=APP))).job.id
    await engine.jobs.run(carol, JobRequest(app=APP))  # another application: not the admin's
    assert {job.id for job in await engine.jobs.list_jobs(admin)} == {alices, bobs}
    assert [job.id for job in await engine.jobs.list_jobs(alice)] == [alices]


# -- the auditor (#52, D46) --------------------------------------------------------------------------------


async def test_an_auditor_reads_every_actor_in_its_application_and_changes_nothing(
    make_engine: EngineFactory, transport: ScriptedTransport, alice: ActorContext, bob: ActorContext
) -> None:
    auditor = ActorContext(actor_id="dashboard", credential_kind="auditor")  # application "default"
    engine, alices = await engine_with_job(make_engine, transport, alice)
    assert [job.id for job in await engine.jobs.list_jobs(auditor)] == [alices]  # another actor's job
    assert (await engine.jobs.get(auditor, alices)).id == alices
    assert (await engine.costs.for_job(auditor, alices)).job_id == alices
    with pytest.raises(AccessDenied):
        await engine.costs.sync_now(auditor)
    with pytest.raises(AccessDenied):
        await engine.jobs.run(auditor, JobRequest(app=APP))
    with pytest.raises(AccessDenied, match="another application"):
        await engine.costs.reconcile(auditor, "other-app", None)
    other = bob.model_copy(update={"application_id": "other-app"})
    transport.steps = [ScriptedTransport.ok()]
    elsewhere = (await engine.jobs.run(other, JobRequest(app=APP))).job.id
    with pytest.raises(NotFound):
        await engine.jobs.get(auditor, elsewhere)  # another application's job reads as missing
