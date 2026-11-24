"""Store fixtures: every store test runs on a SQLite file and, when configured, on PostgreSQL.

Set LPB_TEST_POSTGRES_URL to a *throwaway* database (CI uses a service container). Its public
schema is dropped and recreated for every test.
"""

import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Table, column, table, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.sql.expression import TableClause

from livepeer_builder.contracts import Attempt, Job, SignedSession
from livepeer_builder.testing import FixedClock


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
async def database_url(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[str]:
    if request.param == "sqlite":
        yield f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}"
        return
    url = os.environ.get("LPB_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("LPB_TEST_POSTGRES_URL is not set")
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()
    yield url


# Columns a revision after 0014 added, which neither 0014 nor any revision before it has: 0015 records an
# actor's credential custody (#186 review), and 0016 an async job's claim (ISS-26).
ADDED_SINCE_0014 = {"lpb_actors": {"credential_custody"}, "lpb_jobs": {"started_at", "claims"}}


def as_before_0014(current: Table) -> TableClause:
    """``current`` under the column names it had at every revision before 0014, where a tenant was an
    application (#163), and without the columns added since: for a row a test writes while the database
    is at one of those revisions."""
    old = {"tenant_id": "application_id"}
    added = ADDED_SINCE_0014.get(current.name, set())
    kept = [c for c in current.columns if c.name not in added]
    return table(current.name, *(column(old.get(c.name, c.name), c.type) for c in kept))


def make_job(
    clock: FixedClock, *, actor: str = "alice", ref: str | None = None, state: str = "succeeded"
) -> Job:
    return Job(
        id=uuid4(), actor_id=actor, tenant_id="default", operation_ref=ref, mode="immediate", app="a/b",
        capability="cap", model=None, state=state, attempts=(), failure=None, result=None, usage=None,  # type: ignore[arg-type]
        created_at=clock.now(), completed_at=clock.now() if state != "running" else None,
    )  # fmt: skip


def make_attempt(
    job_id: UUID,
    clock: FixedClock,
    number: int = 1,
    manifest_ids: tuple[str, ...] = (),
    allocation_id: str | None = None,
) -> Attempt:
    return Attempt(
        job_id=job_id,
        number=number,
        runner_url="https://r",
        orchestrator_url="https://o",
        allocation_id=allocation_id,
        sessions=tuple(SignedSession(manifest_id=manifest_id) for manifest_id in manifest_ids),
        payment_sent=bool(manifest_ids),
        outcome="succeeded",
        status_code=200,
        started_at=clock.now(),
        ended_at=clock.now(),
    )
