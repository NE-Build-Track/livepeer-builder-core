"""#115 C20: two usage reports for one job at the same time store one.

``report_usage`` checked "already reported" on the job it had read, then ``set_usage`` updated
unconditionally: two reports that both read the job before either wrote both succeeded, and the second
write replaced the first. The store now writes usage only where none is stored and the job is
``succeeded``, in one statement. A report that loses, or simply repeats, is answered with the stored usage
when its quantities are the same, and refused as before when they differ.

The race is played in a fixed order: the second report runs to its end right after the first one read the
job, which is before the first one writes.
"""

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import delete

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.contracts import ActorContext, Job, JobRequest, Money, ProvisionRequest
from livepeer_builder.errors import InvalidRequest, NotFound
from livepeer_builder.http import create_app
from livepeer_builder.protocols import UsageOnceRepo
from livepeer_builder.store import SqlStore
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FixedClock, ScriptedTransport
from tests.conftest import EngineFactory

APP = "example/echo"


async def succeeded_job(
    engine: BuilderEngine, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport
) -> Job:
    await engine.payments.provision(
        admin,
        ProvisionRequest(
            operation_ref="provision:alice",
            application_id="default",
            actor_id="alice",
            amount=Money(amount=Decimal(1), currency="USD"),
            grant_id="grant_1",
        ),
    )
    transport.steps = [ScriptedTransport.ok()]
    return (await engine.jobs.run(alice, JobRequest(app=APP))).job


def after_the_next_read(
    store: Any, monkeypatch: pytest.MonkeyPatch, other: Callable[[], Awaitable[object]]
) -> None:
    """Run ``other`` to its end right after the next ``get_job``: the report making that read has seen the
    job without usage and has not written yet."""
    get_job = store.get_job

    async def read(job_id: UUID) -> Job | None:
        monkeypatch.setattr(store, "get_job", get_job)  # once
        job: Job | None = await get_job(job_id)
        await other()
        return job

    monkeypatch.setattr(store, "get_job", read)


async def test_two_reports_at_once_with_other_quantities_store_the_first_and_refuse_the_second(
    make_engine: EngineFactory, store: SqlStore, admin: ActorContext, alice: ActorContext,
    transport: ScriptedTransport, monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """Before, both succeeded and the slower one replaced the usage the faster one was told was stored."""
    engine = await make_engine()
    job = await succeeded_job(engine, admin, alice, transport)
    answers: list[Job] = []

    async def faster() -> None:
        answers.append(await engine.jobs.report_usage(alice, job.id, {"tokens": 3}))

    after_the_next_read(store, monkeypatch, faster)
    with pytest.raises(InvalidRequest, match="usage was already reported for this job"):
        await engine.jobs.report_usage(alice, job.id, {"tokens": 9})

    stored = await engine.jobs.get(alice, job.id)
    assert stored.usage is not None and stored.usage.quantities == {"tokens": 3}
    assert answers == [stored]


async def test_two_reports_at_once_with_the_same_quantities_both_answer_with_the_stored_usage(
    make_engine: EngineFactory, store: SqlStore, admin: ActorContext, alice: ActorContext,
    transport: ScriptedTransport, clock: FixedClock, monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    engine = await make_engine()
    job = await succeeded_job(engine, admin, alice, transport)
    answers: list[Job] = []

    async def faster() -> None:
        answers.append(await engine.jobs.report_usage(alice, job.id, {"tokens": 3}))
        clock.advance(5)  # the slower report would be stamped later

    after_the_next_read(store, monkeypatch, faster)
    slower = await engine.jobs.report_usage(alice, job.id, {"tokens": 3})

    stored = await engine.jobs.get(alice, job.id)
    assert stored.usage is not None and stored.usage.reported_at == answers[0].usage.reported_at  # type: ignore[union-attr]
    assert slower == stored == answers[0]  # one usage, stored once: not the slower report's later copy


async def test_a_repeated_report_with_the_same_quantities_is_answered_with_the_stored_usage(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport,
    clock: FixedClock,
) -> None:  # fmt: skip
    """A retry after a lost answer. Before, it was refused: "usage was already reported for this job"."""
    engine = await make_engine()
    job = await succeeded_job(engine, admin, alice, transport)
    first = await engine.jobs.report_usage(alice, job.id, {"tokens": 3, "seconds": 2})
    clock.advance(60)
    again = await engine.jobs.report_usage(alice, job.id, {"seconds": 2, "tokens": 3})
    assert again == first and again.usage is not None and again.usage.reported_at == first.usage.reported_at  # type: ignore[union-attr]
    with pytest.raises(InvalidRequest, match="usage was already reported for this job"):
        await engine.jobs.report_usage(alice, job.id, {"tokens": 3})  # other quantities: refused as before
    assert await engine.jobs.get(alice, job.id) == first


async def test_the_rest_route_answers_an_identical_repeat_200_and_another_report_400(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, transport: ScriptedTransport
) -> None:
    engine = await make_engine()
    job = await succeeded_job(engine, admin, alice, transport)
    actor_key = await engine.keys.issue(
        admin, kind="actor", actor_id="alice", permissions=("jobs.run", "jobs.read"), operation_ref="key-1"
    )
    app = create_app(engine, manage_lifecycle=False)
    headers = {"Authorization": f"Bearer {actor_key.token}"}
    route = f"/v1/jobs/{job.id}/usage"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        first = await client.post(route, json={"quantities": {"tokens": 3}}, headers=headers)
        again = await client.post(route, json={"quantities": {"tokens": 3}}, headers=headers)
        other = await client.post(route, json={"quantities": {"tokens": 4}}, headers=headers)
    assert (first.status_code, again.status_code, other.status_code) == (200, 200, 400)
    assert again.json() == first.json() and first.json()["usage"]["quantities"] == {"tokens": 3}
    assert "usage was already reported for this job" in other.text


async def test_a_report_refused_by_the_store_for_a_job_purged_meanwhile_is_not_found(
    make_engine: EngineFactory, store: SqlStore, admin: ActorContext, alice: ActorContext,
    transport: ScriptedTransport, monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    engine = await make_engine()
    job = await succeeded_job(engine, admin, alice, transport)

    async def purged() -> None:
        async with store.engine.begin() as conn:
            await conn.execute(delete(t.jobs).where(t.jobs.c.id == str(job.id)))

    after_the_next_read(store, monkeypatch, purged)
    with pytest.raises(NotFound):
        await engine.jobs.report_usage(alice, job.id, {"tokens": 3})


# -- a supplied store without the conditional usage write --------------------------------------------------


class _UnconditionalUsage:
    """A supplied store from before #115: everything the SQL store does, without ``set_usage_once``."""

    def __init__(self, inner: SqlStore) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        if name == "set_usage_once":
            raise AttributeError(name)
        return getattr(self._inner, name)


async def test_a_store_without_the_conditional_write_still_takes_reports_and_answers_repeats(
    make_engine: EngineFactory, store: SqlStore, admin: ActorContext, alice: ActorContext,
    transport: ScriptedTransport,
) -> None:  # fmt: skip
    """The conditional write is optional for a store (``UsageOnceRepo``). Without it the engine checks the
    job it read and calls ``set_usage``, as before. A repeat still gets the stored usage or the refusal."""
    supplied = _UnconditionalUsage(store)
    assert isinstance(store, UsageOnceRepo) and not isinstance(supplied, UsageOnceRepo)
    engine = await make_engine(supply=EngineSupply(store=supplied))  # type: ignore[arg-type]
    job = await succeeded_job(engine, admin, alice, transport)
    first = await engine.jobs.report_usage(alice, job.id, {"tokens": 3})
    assert first.usage is not None and first.usage.quantities == {"tokens": 3}
    assert await engine.jobs.report_usage(alice, job.id, {"tokens": 3}) == first
    with pytest.raises(InvalidRequest, match="usage was already reported for this job"):
        await engine.jobs.report_usage(alice, job.id, {"tokens": 9})
    assert await engine.jobs.get(alice, job.id) == first


async def test_a_store_without_the_conditional_write_keeps_the_old_race(
    make_engine: EngineFactory, store: SqlStore, admin: ActorContext, alice: ActorContext,
    transport: ScriptedTransport, monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """What such a store gets, stated in the docs: two reports at the same moment both succeed and the
    slower one's usage is the one stored. Only a condition in the store's own write prevents it."""
    supplied = _UnconditionalUsage(store)
    engine = await make_engine(supply=EngineSupply(store=supplied))  # type: ignore[arg-type]
    job = await succeeded_job(engine, admin, alice, transport)

    async def faster() -> None:
        await engine.jobs.report_usage(alice, job.id, {"tokens": 3})

    after_the_next_read(store, monkeypatch, faster)
    slower = await engine.jobs.report_usage(alice, job.id, {"tokens": 9})
    stored = await engine.jobs.get(alice, job.id)
    assert slower.usage == stored.usage and stored.usage is not None
    assert stored.usage.quantities == {"tokens": 9}
