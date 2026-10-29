"""Re-dispatch a job that never reached a runner under the same operation_ref (D57, ISS-14).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-29

A job that failed before any runner accepted the work (no capacity, every orchestrator refused, the
signer refused) may run again under its reference. The new job records the one it retries (retry_of);
the old one records its successor (superseded_by) and keeps its reference for history. The reference
stays unique among jobs that are not superseded, so the partial unique index gains that predicate.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HOLDER = "operation_ref IS NOT NULL AND superseded_by IS NULL"
_ANY = "operation_ref IS NOT NULL"
_COLUMNS = ["application_id", "actor_id", "operation_ref"]


def upgrade() -> None:
    op.add_column("lpb_jobs", sa.Column("retry_of", sa.String(36), nullable=True))
    op.add_column("lpb_jobs", sa.Column("superseded_by", sa.String(36), nullable=True))
    op.drop_index("lpb_jobs_operation_ref", table_name="lpb_jobs")
    op.create_index(
        "lpb_jobs_operation_ref",
        "lpb_jobs",
        _COLUMNS,
        unique=True,
        sqlite_where=sa.text(_HOLDER),
        postgresql_where=sa.text(_HOLDER),
    )


def downgrade() -> None:
    # The old index allows one job per reference: superseded jobs give theirs up.
    op.execute("UPDATE lpb_jobs SET operation_ref = NULL WHERE superseded_by IS NOT NULL")
    op.drop_index("lpb_jobs_operation_ref", table_name="lpb_jobs")
    op.create_index(
        "lpb_jobs_operation_ref",
        "lpb_jobs",
        _COLUMNS,
        unique=True,
        sqlite_where=sa.text(_ANY),
        postgresql_where=sa.text(_ANY),
    )
    op.drop_column("lpb_jobs", "superseded_by")
    op.drop_column("lpb_jobs", "retry_of")
