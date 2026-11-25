"""ISS-26: migration 0016, which makes ``lpb_jobs`` a queue and adds the stored request and the stored
result of an async job. SQLite and PostgreSQL.

A downgrade also has to leave records the release at revision 0015 can read. Its contracts refuse an
unknown field and hold closed sets of values, so one job written since, left as it was, would fail to
load there, and with it every page of the job list that holds it.
"""

import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import insert, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from livepeer_builder.contracts import Failure, Job, MeasuredUsage, ResultRef
from livepeer_builder.store import SqlStore, create_engine, current_revision, head_revision, upgrade
from livepeer_builder.store import schema as t
from livepeer_builder.store.jobs import _job_values
from livepeer_builder.store.migrate import downgrade
from livepeer_builder.testing import FixedClock
from tests.store.conftest import make_attempt, make_job
from tests.store.test_domain_language_names import interrupted
from tests.store.test_job_queue import ALICE, REQUEST

NEW_TABLES = {"lpb_job_requests", "lpb_job_results"}


class _At0015(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")  # as ``Contract``: an unknown field is refused


class Failure0015(_At0015):
    """``Failure`` as the release at revision 0015 reads it, copied from its ``contracts/jobs.py``: that
    class cannot be imported beside this branch's. Twelve kinds, the four payment reasons, and a reason
    only on a payment failure."""

    kind: Literal[
        "no_offering",
        "no_capacity",
        "not_provisioned",
        "refused",
        "unreachable",
        "payment",
        "payment_unknown",
        "timeout",
        "runner_rejected",
        "runner_error",
        "interrupted",
        "abandoned",
    ]
    message: str
    status_code: int | None = None
    body: str | None = None
    payment_sent: bool = False
    reason: (
        Literal["not_authorized", "allowance_exhausted", "signer_unavailable", "signer_unknown"] | None
    ) = None

    @model_validator(mode="after")
    def _reason_fits_kind(self) -> "Failure0015":
        fits = "payment_unknown" if self.reason == "signer_unknown" else "payment"
        if self.reason is not None and self.kind != fits:
            raise ValueError(f"reason {self.reason!r} does not fit kind {self.kind!r}")
        return self


class ResultRef0015(_At0015):
    """``ResultRef`` at revision 0015: no ``expires_at``."""

    storage: Literal["inline", "stored"]
    content_type: str
    size_bytes: int
    sha256: str
    location: str | None = None


MODES_0015 = {"immediate", "async"}
STATES_0015 = {"queued", "running", "succeeded", "failed", "uncertain"}
ANSWER = b'{"image": "..."}'
MORE_THAN_A_PAGE = 1100  # the downgrade rewrites the documents 500 jobs at a time
TOO_LARGE = "the runner's reply is 99 bytes, over the 16 the engine stores"
NEW_COLUMNS = {"started_at", "claims", "claims_given_back"}


async def tables(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        return set(await conn.run_sync(lambda sync: inspect(sync).get_table_names()))


async def job_columns(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        found = await conn.run_sync(lambda sync: inspect(sync).get_columns("lpb_jobs"))
    return {column["name"] for column in found}


async def job_row(engine: AsyncEngine, job_id: str) -> dict[str, Any]:
    async with engine.connect() as conn:
        found = await conn.execute(select(t.jobs).where(t.jobs.c.id == job_id))
        return dict(found.mappings().one())


async def test_the_migration_keeps_every_job_and_a_downgrade_ends_a_queued_one(
    database_url: str, clock: FixedClock
) -> None:
    """A job written at revision 0015 reads back unclaimed. A downgrade drops what 0016 added; a job
    still queued then could never run, so it ends ``failed`` and gives its reference up for a repeat."""
    engine = create_engine(database_url)
    try:
        await upgrade(engine, "0015")
        assert not NEW_TABLES & await tables(engine) and not NEW_COLUMNS & await job_columns(engine)
        store = SqlStore(engine)
        before = make_job(clock, ref="order-1")
        async with engine.begin() as conn:  # a record written by revision 0015's schema
            await conn.execute(
                text(
                    "INSERT INTO lpb_jobs (id, tenant_id, actor_id, operation_ref, mode, app, state, "
                    "created_at, completed_at) VALUES "
                    "(:id, 'default', 'alice', 'order-1', 'immediate', 'a/b', 'succeeded', :now, :now)"
                ),
                {"id": str(before.id), "now": clock.now()},
            )

        await upgrade(engine)
        assert await current_revision(engine) == head_revision() == "0016"
        assert await tables(engine) >= NEW_TABLES and await job_columns(engine) >= NEW_COLUMNS
        kept = await store.get_job(before.id)
        assert kept is not None and (kept.state, kept.operation_ref, kept.started_at) == (
            "succeeded",
            "order-1",
            None,
        )
        assert (await job_row(engine, str(before.id)))["claims"] == 0

        waiting = make_job(clock, ref="order-2", state="queued").model_copy(update={"mode": "async"})
        async with engine.begin() as conn:
            await conn.execute(
                insert(t.jobs).values(
                    id=str(waiting.id),
                    tenant_id="default",
                    actor_id="alice",
                    operation_ref="order-2",
                    mode="async",
                    app="a/b",
                    state="queued",
                    created_at=clock.now(),
                )
            )
            await conn.execute(
                insert(t.job_requests).values(job_id=str(waiting.id), request={"app": "a/b"}, actor={})
            )

        await downgrade(engine, "0015")
        assert await current_revision(engine) == "0015"
        assert not NEW_TABLES & await tables(engine) and not NEW_COLUMNS & await job_columns(engine)
        async with engine.connect() as conn:
            ended = await conn.execute(
                text("SELECT state, failure, completed_at FROM lpb_jobs WHERE id = :id"),
                {"id": str(waiting.id)},
            )
            state, failure, completed_at = ended.one()
        assert state == "failed" and "interrupted" in str(failure) and completed_at is not None

        await upgrade(engine)
        again = await store.get_job(waiting.id)
        assert again is not None and again.failure is not None
        assert (again.state, again.failure.kind, again.failure.payment_sent) == (
            "failed",
            "interrupted",
            False,
        )
        assert await store.get_job(before.id) == kept
    finally:
        await engine.dispose()


async def test_an_interrupted_run_on_sqlite_changes_nothing(tmp_path: Path) -> None:
    """SQLite's driver commits an ``ALTER TABLE`` that runs before any row is written. A run that died
    after it added the claim columns would leave them under revision 0015, and the next ``lpb migrate``
    would fail on a column that is already there. The run is one transaction, up and down."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}")
    try:
        await upgrade(engine, "0015")
        at_0015 = await tables(engine), await job_columns(engine)
        await interrupted(engine, "up", "head")
        assert await current_revision(engine) == "0015"
        assert (await tables(engine), await job_columns(engine)) == at_0015

        await upgrade(engine)
        await written_at_head(SqlStore(engine), FixedClock())
        at_head = await tables(engine), await job_columns(engine), await documents(engine)
        await interrupted(engine, "down", "0015")
        assert await current_revision(engine) == head_revision()
        assert (await tables(engine), await job_columns(engine), await documents(engine)) == at_head
    finally:
        await engine.dispose()


def ref(storage: str, **fields: Any) -> ResultRef:
    shape = {
        "content_type": "image/png",
        "size_bytes": len(ANSWER),
        "sha256": hashlib.sha256(ANSWER).hexdigest(),
    }
    return ResultRef.model_validate({"storage": storage, **shape, **fields})


async def documents(engine: AsyncEngine) -> dict[str, tuple[str, str, Any, Any, Any]]:
    """Each job's mode, state and JSON documents as the database holds them, read without the contracts."""
    columns = (t.jobs.c.id, t.jobs.c.mode, t.jobs.c.state, t.jobs.c.failure, t.jobs.c.result, t.jobs.c.usage)
    async with engine.connect() as conn:
        return {row[0]: tuple(row[1:]) for row in await conn.execute(select(*columns))}  # type: ignore[misc]


async def written_at_head(store: SqlStore, clock: FixedClock) -> dict[str, Job]:
    """One job for each thing this release writes that revision 0015 has no word for, each written by
    the store's own writer, and beside them jobs that need no change."""
    now, later = clock.now(), clock.now() + timedelta(days=7)

    async def running(name: str) -> tuple[Job, int]:
        job = make_job(clock, ref=name, state="queued").model_copy(
            update={"mode": "async", "completed_at": None}
        )
        await store.enqueue_job(job, "fp", REQUEST, ALICE)
        taken = await store.claim_job(job.id, now)
        assert taken is not None
        return job, taken[3]

    jobs: dict[str, Job] = {}
    # an immediate job: its inline reference carries ``expires_at`` too, as a null
    jobs["inline"] = make_job(clock, ref="inline").model_copy(update={"result": ref("inline")})
    await store.create_job(jobs["inline"], "fp")
    await store.set_usage(
        jobs["inline"].id, MeasuredUsage(quantities={"tokens": 3}, source="app_reported", reported_at=now)
    )
    # a stored result: a location and a time it expires
    jobs["stored"], claim = await running("stored")
    kept = ref("stored", location=f"/v1/jobs/{jobs['stored'].id}/result", expires_at=later)
    assert await store.finish_job_with_result(jobs["stored"].id, kept, ANSWER, now, claim=claim)
    # one kept as long as its job: ``expires_at`` null
    jobs["kept"], claim = await running("kept")
    for_good = ref("stored", location=f"/v1/jobs/{jobs['kept'].id}/result")
    assert await store.finish_job_with_result(jobs["kept"].id, for_good, ANSWER, now, claim=claim)
    # a paid answer too large to store: a new kind, and a reference with no location
    jobs["not_stored"], claim = await running("not-stored")
    assert await store.start_attempt(
        make_attempt(jobs["not_stored"].id, clock, manifest_ids=("m1",)), claim=claim
    )
    too_large = Failure(kind="result_not_stored", message=TOO_LARGE, payment_sent=True)
    assert await store.finish_job(jobs["not_stored"].id, "failed", too_large, ref("stored"), now, claim=claim)
    # a cancel: a new reason, on a kind that takes none at 0015
    jobs["cancelled"] = make_job(clock, ref="cancelled", state="queued").model_copy(update={"mode": "async"})
    await store.enqueue_job(jobs["cancelled"], "fp", REQUEST, ALICE)
    cancel = Failure(kind="abandoned", message="cancelled before it ran", reason="cancelled")
    assert await store.cancel_queued(jobs["cancelled"].id, cancel, now)
    # what 0015 already knows stays as it is: a payment failure with its reason, and a queue expiry
    jobs["spent"], claim = await running("spent")
    spent = Failure(kind="payment", message="402", status_code=402, reason="allowance_exhausted")
    assert await store.finish_job(jobs["spent"].id, "failed", spent, None, now, claim=claim)
    jobs["expired"] = make_job(clock, ref="expired", state="queued").model_copy(update={"mode": "async"})
    await store.enqueue_job(jobs["expired"], "fp", REQUEST, ALICE)
    late = Failure(
        kind="no_capacity", message="the job was not dispatched within the queue's time limit (1 s)"
    )
    assert await store.expire_queued(now + timedelta(seconds=1), late, now) == 1
    # and the two a downgrade finds unfinished
    jobs["queued"] = make_job(clock, ref="queued", state="queued").model_copy(update={"mode": "async"})
    await store.enqueue_job(jobs["queued"], "fp", REQUEST, ALICE)
    jobs["running"], _ = await running("running")
    return jobs


async def test_a_downgrade_leaves_only_documents_the_earlier_release_reads(
    database_url: str, clock: FixedClock
) -> None:
    """Every job kept by the downgrade loads under revision 0015's models. The new words are mapped
    onto the nearest ones 0015 has, and each message is kept: ``result_not_stored`` is ``runner_error``,
    the kind this release itself gives a paid answer it could not store for another reason; a cancel
    keeps ``abandoned`` and loses the reason 0015 does not have; a stored result's bytes went with their
    table, so its reference keeps ``stored`` and names no location."""
    engine = create_engine(database_url)
    try:
        await upgrade(engine)
        store = SqlStore(engine)
        jobs = await written_at_head(store, clock)
        ids = {name: str(job.id) for name, job in jobs.items()}
        # and more jobs than the downgrade reads at a time, so none is passed over between two pages
        crowd = [
            make_job(clock).model_copy(update={"result": ref("inline")}) for _ in range(MORE_THAN_A_PAGE)
        ]
        async with engine.begin() as conn:
            await conn.execute(insert(t.jobs), [_job_values(job) for job in crowd])
        at_head = await documents(engine)
        assert at_head[ids["inline"]][3]["expires_at"] is None  # serialized for an inline result too
        assert at_head[ids["not_stored"]][2]["kind"] == "result_not_stored"
        assert at_head[ids["cancelled"]][2]["reason"] == "cancelled"

        await downgrade(engine, "0015")
        assert await current_revision(engine) == "0015"
        kept = await documents(engine)
        assert kept.keys() == at_head.keys()  # no job is lost
        for mode, state, failure, result, _ in kept.values():
            assert mode in MODES_0015 and state in STATES_0015
            assert failure is None or Failure0015.model_validate(failure)
            assert result is None or ResultRef0015.model_validate(result)

        def failure_of(name: str) -> Failure0015:
            return Failure0015.model_validate(kept[ids[name]][2])

        def result_of(name: str) -> ResultRef0015:
            return ResultRef0015.model_validate(kept[ids[name]][3])

        shape = {
            "content_type": "image/png",
            "size_bytes": len(ANSWER),
            "sha256": hashlib.sha256(ANSWER).hexdigest(),
        }
        assert kept[ids["inline"]] == (
            "immediate",
            "succeeded",
            None,
            {"storage": "inline", **shape, "location": None},
            at_head[ids["inline"]][4],  # its usage is as it was
        )
        for name in ("stored", "kept"):  # the bytes are gone: the reference says what the result was
            assert kept[ids[name]][1:4] == (
                "succeeded",
                None,
                {"storage": "stored", **shape, "location": None},
            )
        assert failure_of("not_stored") == Failure0015(
            kind="runner_error", message=TOO_LARGE, payment_sent=True
        )
        assert kept[ids["not_stored"]][1] == "failed" and result_of("not_stored") == result_of("stored")
        assert failure_of("cancelled") == Failure0015(kind="abandoned", message="cancelled before it ran")
        assert kept[ids["spent"]][2] == at_head[ids["spent"]][2]
        assert kept[ids["expired"]][2] == at_head[ids["expired"]][2]
        assert (kept[ids["queued"]][1], failure_of("queued").kind) == ("failed", "interrupted")
        assert kept[ids["running"]][1:4] == ("running", None, None)  # 0015's own start resolves it

        await upgrade(engine)  # and this release reads what the downgrade wrote
        assert len(await store.list_jobs("default", None, limit=2000)) == len(jobs) + len(crowd)
        assert await store.read_result(jobs["stored"].id, clock.now()) is None
    finally:
        await engine.dispose()
