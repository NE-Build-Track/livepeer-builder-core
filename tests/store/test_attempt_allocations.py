"""#115 C9: the cost snapshot says which allocation paid each attempt (``AllocationCostSnapshot``), on
SQLite and PostgreSQL. Reconciliation uses it to count one allocation at a time."""

from collections.abc import AsyncIterator

import pytest

from livepeer_builder.protocols import AllocationCostSnapshot
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FixedClock
from tests.store.conftest import make_attempt, make_job


@pytest.fixture
async def sql(database_url: str) -> AsyncIterator[SqlStore]:
    store = await SqlStore.connect(database_url)
    yield store
    await store.close()


async def test_the_snapshot_reads_the_allocation_of_each_attempt_of_the_actors_jobs(
    sql: SqlStore, clock: FixedClock
) -> None:
    first, second, unpaid, bobs = (
        make_job(clock, actor=actor) for actor in ("alice", "alice", "alice", "bob")
    )
    other_app = make_job(clock).model_copy(update={"application_id": "other"})
    for job in (first, second, unpaid, bobs, other_app):
        await sql.create_job(job, None)
    await sql.start_attempt(make_attempt(first.id, clock, 1, ("m-1",), allocation_id="a1"))
    await sql.start_attempt(make_attempt(first.id, clock, 2, ("m-2",), allocation_id="a1"))
    await sql.start_attempt(make_attempt(second.id, clock, 1, ("m-3",), allocation_id="a2"))
    await sql.start_attempt(make_attempt(unpaid.id, clock, 1))  # records no allocation: left out
    await sql.start_attempt(make_attempt(bobs.id, clock, 1, ("m-4",), allocation_id="b1"))
    await sql.start_attempt(make_attempt(other_app.id, clock, 1, ("m-5",), allocation_id="o1"))

    async with sql.cost_snapshot() as reads:
        assert isinstance(reads, AllocationCostSnapshot)
        alices = await reads.attempt_allocations("default", "alice")
        application = await reads.attempt_allocations("default", None)
        nobody = await reads.attempt_allocations("default", "nobody")
    assert alices == {(first.id, 1): "a1", (first.id, 2): "a1", (second.id, 1): "a2"}
    assert application == alices | {(bobs.id, 1): "b1"}
    assert nobody == {}
