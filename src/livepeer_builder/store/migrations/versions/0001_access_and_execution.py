"""Access keys, jobs, attempts and attempt auth ids.

Revision ID: 0001
Revises:
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from livepeer_builder.store.schema import UTCDateTime

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lpb_access_keys",
        sa.Column("key_id", sa.String(64), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(200), nullable=False),
        sa.Column("application_id", sa.String(200), nullable=False),
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.Column("label", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("revoked_at", UTCDateTime(), nullable=True),
    )
    op.create_table(
        "lpb_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("application_id", sa.String(200), nullable=False),
        sa.Column("actor_id", sa.String(200), nullable=False),
        sa.Column("operation_ref", sa.String(200), nullable=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=True),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("app", sa.String(200), nullable=False),
        sa.Column("capability", sa.String(200), nullable=True),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("failure", sa.JSON(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("completed_at", UTCDateTime(), nullable=True),
    )
    op.create_index(
        "lpb_jobs_operation_ref",
        "lpb_jobs",
        ["application_id", "actor_id", "operation_ref"],
        unique=True,
        sqlite_where=sa.text("operation_ref IS NOT NULL"),
        postgresql_where=sa.text("operation_ref IS NOT NULL"),
    )
    op.create_index("lpb_jobs_owner_created", "lpb_jobs", ["application_id", "actor_id", "created_at"])
    op.create_index("lpb_jobs_state", "lpb_jobs", ["state"])
    op.create_table(
        "lpb_attempts",
        sa.Column(
            "job_id", sa.String(36), sa.ForeignKey("lpb_jobs.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("number", sa.Integer(), primary_key=True),
        sa.Column("runner_url", sa.Text(), nullable=True),
        sa.Column("orchestrator_url", sa.Text(), nullable=True),
        sa.Column("manifest_id", sa.String(200), nullable=True),
        sa.Column("payment_sent", sa.Boolean(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("started_at", UTCDateTime(), nullable=False),
        sa.Column("ended_at", UTCDateTime(), nullable=True),
    )
    op.create_table(
        "lpb_attempt_auth_ids",
        sa.Column("auth_id", sa.String(200), primary_key=True),
        sa.Column("job_id", sa.String(36), primary_key=True),
        sa.Column("attempt_number", sa.Integer(), primary_key=True),
        sa.ForeignKeyConstraint(
            ["job_id", "attempt_number"], ["lpb_attempts.job_id", "lpb_attempts.number"], ondelete="CASCADE"
        ),
    )


def downgrade() -> None:
    op.drop_table("lpb_attempt_auth_ids")
    op.drop_table("lpb_attempts")
    op.drop_index("lpb_jobs_state", table_name="lpb_jobs")
    op.drop_index("lpb_jobs_owner_created", table_name="lpb_jobs")
    op.drop_index("lpb_jobs_operation_ref", table_name="lpb_jobs")
    op.drop_table("lpb_jobs")
    op.drop_table("lpb_access_keys")
