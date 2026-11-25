"""Async jobs: the queue, the stored request of a waiting job, and stored results (D69, ISS-26).

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-19

``lpb_jobs`` is the queue: a job submitted with ``mode="async"`` is stored ``queued`` and a worker claims
it with a compare-and-set. Three columns record the claim: ``started_at``, when a worker took the job;
``claims``, how many times one did, which is also the number of the latest claim and only ever grows;
and ``claims_given_back``, how many of those claims a job that only had to wait gave back. The claims
that were not given back are the ones counted, which stops a job that brings the engine down before
its first attempt from being claimed for ever.

``lpb_job_requests`` holds what a waiting job runs with: its request and the actor it was authorized
for at submit. The row is deleted in the transaction that ends the job. ``lpb_job_results`` holds the
bytes a succeeded async job returned, written in that same transaction, until they expire. Neither
table is encrypted by the engine.

The revision changes no existing row: every job already stored has both counts 0 and no ``started_at``.

A downgrade drops both tables and the three columns. A job still ``queued`` then could never run,
because its request is gone and the earlier release has no worker: the downgrade ends it ``failed``,
``interrupted``, so its ``operation_ref`` is free to run again. Stored results are deleted with their
table; the jobs keep their record.

**The downgrade rewrites what the earlier release cannot read.** That release's contracts refuse an
unknown field and hold closed sets of values, so one job left as this release wrote it would fail to
load there, and so would every page of the job list that holds it. Each kept job's ``result`` and
``failure`` is therefore rewritten into revision 0015's words before anything is dropped:

- ``ResultRef.expires_at``, which this release writes on every reference, an immediate job's too, is
  removed: 0015 has no such field.
- A reference's ``location`` becomes null. The bytes of a stored result go with their table, and the
  path that served them is a route the earlier release answers 501. ``storage`` stays ``stored``:
  the result was kept by the engine and not returned inline, and a ``stored`` reference with no
  location is how this release, too, names a result whose size and hash are all that is left. A
  later upgrade reads it as a result that has expired.
- Failure kind ``result_not_stored`` becomes ``runner_error``, the kind this release itself gives a
  paid answer it could not store for another reason (the finishing write failed). Its message, which
  says what happened, and ``payment_sent`` are kept, and so is the job's reference with the answer's
  size and hash.
- Failure reason ``cancelled`` becomes null and the kind stays ``abandoned``, the caller giving the
  job up: 0015 allows a reason only on a payment failure. The message ("cancelled before it ran") is
  kept.

Everything else a job of this release holds is in 0015's words already: ``mode`` ``async`` and the
states are in its sets, and no other kind, reason or field is new. The documents are rewritten in
Python, page by page, so the statements are the same on SQLite and on PostgreSQL.

A run is one transaction, in both directions, as revision 0014's is: SQLite's driver opens a transaction
at the first ``INSERT``, ``UPDATE`` or ``DELETE`` and not at an ``ALTER TABLE``, so the upgrade starts
with a ``DELETE`` that matches no row, and the downgrade with the ``UPDATE`` that ends the queued jobs.
A run that dies before its commit then leaves no column and no table behind, and no document changed.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLAIM_COUNTERS = ("claims", "claims_given_back")  # on ``lpb_jobs``; 0 for every job already stored
_NEVER_RAN = {
    "kind": "interrupted",
    "message": "the database was downgraded while this job was queued; it never ran",
    "status_code": None,
    "body": None,
    "payment_sent": False,
    "reason": None,
}
# What this release writes into a job's documents that revision 0015 has no word for, and the word of
# 0015 that says it most nearly (the module's docstring says why each).
_KIND_AT_0015 = {"result_not_stored": "runner_error"}
_REASONS_SINCE_0015 = frozenset({"cancelled"})  # 0015 has a reason only on a payment failure: null there
_RESULT_FIELDS_SINCE_0015 = frozenset({"expires_at"})
_PAGE = 500  # jobs read at a time while their documents are rewritten


def _job_id() -> sa.Column[str]:
    return sa.Column(
        "job_id", sa.String(36), sa.ForeignKey("lpb_jobs.id", ondelete="CASCADE"), primary_key=True
    )


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":  # open the driver's transaction before the ALTER TABLE
        op.execute("DELETE FROM lpb_jobs WHERE 1 = 0")
    op.add_column("lpb_jobs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    for counter in _CLAIM_COUNTERS:
        op.add_column(
            "lpb_jobs", sa.Column(counter, sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
    op.create_table(
        "lpb_job_requests",
        _job_id(),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("raw_body", sa.LargeBinary(), nullable=True),
        sa.Column("actor", sa.JSON(), nullable=False),
    )
    op.create_table(
        "lpb_job_results",
        _job_id(),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("lpb_job_results_expires", "lpb_job_results", ["expires_at"])


def _failure_at_0015(failure: dict[str, Any]) -> dict[str, Any]:
    """The failure in 0015's words: its kind mapped, a reason 0015 does not have removed, the rest kept."""
    kind, reason = failure.get("kind"), failure.get("reason")
    return failure | {
        "kind": _KIND_AT_0015.get(str(kind), kind),
        "reason": None if reason in _REASONS_SINCE_0015 else reason,
    }


def _result_at_0015(result: dict[str, Any]) -> dict[str, Any]:
    """The reference as 0015 reads it: without the fields added since, and with no location, because
    no stored bytes are left to serve. Its storage, content type, size and hash say what the result was."""
    kept = {name: value for name, value in result.items() if name not in _RESULT_FIELDS_SINCE_0015}
    return kept | {"location": None}


def _documents_at_0015(failure: object, result: object) -> dict[str, Any]:
    """The documents of one job that have to change, by column. A null document is left as it is."""
    rewritten = {"failure": (failure, _failure_at_0015), "result": (result, _result_at_0015)}
    changed: dict[str, Any] = {}
    for column, (stored, at_0015) in rewritten.items():
        if isinstance(stored, dict) and at_0015(stored) != stored:
            changed[column] = at_0015(stored)
    return changed


def _rewrite_documents(jobs: sa.TableClause) -> None:
    """Rewrite every job's ``failure`` and ``result`` into revision 0015's words, a page at a time by
    id, so the whole table is never held and a row is written only when it changes."""
    bind = op.get_bind()
    page = sa.select(jobs.c.id, jobs.c.failure, jobs.c.result).order_by(jobs.c.id).limit(_PAGE)
    last = ""
    while True:
        rows = bind.execute(page.where(jobs.c.id > last)).all()
        if not rows:
            return
        for job_id, failure, result in rows:
            changed = _documents_at_0015(failure, result)
            if changed:
                bind.execute(sa.update(jobs).where(jobs.c.id == job_id).values(changed))
        last = rows[-1][0]


def downgrade() -> None:
    jobs = sa.table(
        "lpb_jobs",
        sa.column("id", sa.String),
        sa.column("state", sa.String),
        sa.column("failure", sa.JSON),
        sa.column("result", sa.JSON),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("completed_at", sa.DateTime(timezone=True)),
    )
    op.execute(  # first: on SQLite this write opens the transaction the whole run is in
        sa.update(jobs)
        .where(jobs.c.state == "queued")
        .values(state="failed", failure=_NEVER_RAN, completed_at=jobs.c.created_at)
    )
    _rewrite_documents(jobs)
    op.drop_index("lpb_job_results_expires", table_name="lpb_job_results")
    op.drop_table("lpb_job_results")
    op.drop_table("lpb_job_requests")
    for counter in reversed(_CLAIM_COUNTERS):
        op.drop_column("lpb_jobs", counter)
    op.drop_column("lpb_jobs", "started_at")
