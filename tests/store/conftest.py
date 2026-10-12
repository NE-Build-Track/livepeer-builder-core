"""Store fixtures: every store test runs on a SQLite file and, when configured, on PostgreSQL.

Set LPB_TEST_POSTGRES_URL to a *throwaway* database (CI uses a service container). Its public
schema is dropped and recreated for every test.
"""

import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from livepeer_builder.contracts import Attempt, Job
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


def make_job(
    clock: FixedClock, *, actor: str = "alice", ref: str | None = None, state: str = "succeeded"
) -> Job:
    return Job(
        id=uuid4(), actor_id=actor, application_id="default", operation_ref=ref, mode="immediate", app="a/b",
        capability="cap", model=None, state=state, attempts=(), failure=None, result=None, usage=None,  # type: ignore[arg-type]
        created_at=clock.now(), completed_at=clock.now() if state != "running" else None,
    )  # fmt: skip


def make_attempt(job_id: UUID, clock: FixedClock, number: int = 1, auth_ids: tuple[str, ...] = ()) -> Attempt:
    return Attempt(
        job_id=job_id, number=number, runner_url="https://r", orchestrator_url="https://o", auth_ids=auth_ids,
        manifest_id="m", payment_sent=bool(auth_ids), outcome="succeeded", status_code=200,
        started_at=clock.now(), ended_at=clock.now(),
    )  # fmt: skip
