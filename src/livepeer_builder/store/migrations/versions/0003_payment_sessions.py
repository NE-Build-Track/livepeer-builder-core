"""Replace attempt auth ids with the engine's own payment-session log.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-14

Each attempt records the allocation that paid and one row per session the signer signed.
Cost sync fills payment_session_id by matching (allocation_id, manifest_id) to Batteries usage.
No backfill: M2 has not been released.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("lpb_attempts", sa.Column("allocation_id", sa.String(200), nullable=True))
    op.drop_column("lpb_attempts", "manifest_id")
    op.drop_table("lpb_attempt_auth_ids")
    op.create_table(
        "lpb_payment_sessions",
        sa.Column("job_id", sa.String(36), primary_key=True),
        sa.Column("attempt_number", sa.Integer(), primary_key=True),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("manifest_id", sa.String(200), nullable=False),
        sa.Column("payment_session_id", sa.String(200), nullable=True),
        sa.Column("match_status", sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id", "attempt_number"],
            ["lpb_attempts.job_id", "lpb_attempts.number"],
            ondelete="CASCADE",
        ),
    )
    op.create_index("lpb_payment_sessions_manifest", "lpb_payment_sessions", ["manifest_id"])
    op.create_index("lpb_payment_sessions_session", "lpb_payment_sessions", ["payment_session_id"])


def downgrade() -> None:
    op.drop_index("lpb_payment_sessions_session", table_name="lpb_payment_sessions")
    op.drop_index("lpb_payment_sessions_manifest", table_name="lpb_payment_sessions")
    op.drop_table("lpb_payment_sessions")
    op.create_table(
        "lpb_attempt_auth_ids",
        sa.Column("auth_id", sa.String(200), primary_key=True),
        sa.Column("job_id", sa.String(36), primary_key=True),
        sa.Column("attempt_number", sa.Integer(), primary_key=True),
        sa.ForeignKeyConstraint(
            ["job_id", "attempt_number"],
            ["lpb_attempts.job_id", "lpb_attempts.number"],
            ondelete="CASCADE",
        ),
    )
    op.add_column("lpb_attempts", sa.Column("manifest_id", sa.String(200), nullable=True))
    op.drop_column("lpb_attempts", "allocation_id")
