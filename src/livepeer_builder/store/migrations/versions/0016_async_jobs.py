"""Async jobs: the queue, the stored request of a waiting job, and stored results (D69, ISS-26).

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-19

``lpb_jobs`` is the queue: a job submitted with ``mode="async"`` is stored ``queued`` and a worker claims
it with a compare-and-set. Two columns record the claim: ``started_at``, when a worker took the job, and
``claims``, how many times one did, which stops a job that brings the engine down before its first
attempt from being claimed for ever.

``lpb_job_requests`` holds what a waiting job runs with: its request and the actor it was authorized
for at submit. The row is deleted in the transaction that ends the job. ``lpb_job_results`` holds the
bytes a succeeded async job returned, written in that same transaction, until they expire. Neither
table is encrypted by the engine.

The revision changes no existing row: every job already stored has ``claims`` 0 and no ``started_at``.
A downgrade drops both tables and both columns. A job still ``queued`` then could never run, because
its request is gone and the earlier release has no worker: the downgrade ends it ``failed``,
``interrupted``, so its ``operation_ref`` is free to run again. Stored results are deleted with their
table; the jobs keep their record.

A run is one transaction, in both directions, as revision 0014's is: SQLite's driver opens a transaction
at the first ``INSERT``, ``UPDATE`` or ``DELETE`` and not at an ``ALTER TABLE``, so the upgrade starts
with a ``DELETE`` that matches no row, and the downgrade with the ``UPDATE`` that ends the queued jobs.
A run that dies before its commit then leaves no column and no table behind.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEVER_RAN = {
    "kind": "interrupted",
    "message": "the database was downgraded while this job was queued; it never ran",
    "status_code": None,
    "body": None,
    "payment_sent": False,
    "reason": None,
}


def _job_id() -> sa.Column[str]:
    return sa.Column(
        "job_id", sa.String(36), sa.ForeignKey("lpb_jobs.id", ondelete="CASCADE"), primary_key=True
    )


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":  # open the driver's transaction before the ALTER TABLE
        op.execute("DELETE FROM lpb_jobs WHERE 1 = 0")
    op.add_column("lpb_jobs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("lpb_jobs", sa.Column("claims", sa.Integer(), nullable=False, server_default=sa.text("0")))
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


def downgrade() -> None:
    jobs = sa.table(
        "lpb_jobs",
        sa.column("state", sa.String),
        sa.column("failure", sa.JSON),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("completed_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        sa.update(jobs)
        .where(jobs.c.state == "queued")
        .values(state="failed", failure=_NEVER_RAN, completed_at=jobs.c.created_at)
    )
    op.drop_index("lpb_job_results_expires", table_name="lpb_job_results")
    op.drop_table("lpb_job_results")
    op.drop_table("lpb_job_requests")
    op.drop_column("lpb_jobs", "claims")
    op.drop_column("lpb_jobs", "started_at")
