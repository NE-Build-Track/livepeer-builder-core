"""Provisioned actors, encrypted secrets, cached usage rows, the cost cursor and cost projections.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from livepeer_builder.store.schema import UTCDateTime

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lpb_actors",
        sa.Column("application_id", sa.String(200), primary_key=True),
        sa.Column("actor_id", sa.String(200), primary_key=True),
        sa.Column("grant_id", sa.String(200), nullable=False),
        sa.Column("allocation_id", sa.String(200), nullable=False, unique=True),
        sa.Column("api_key_id", sa.String(200), nullable=False),
        sa.Column("secret_ref", sa.String(200), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
    )
    op.create_table(
        "lpb_secrets",
        sa.Column("ref", sa.String(64), primary_key=True),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
    )
    op.create_table(
        "lpb_usage_rows",
        sa.Column("id", sa.String(200), primary_key=True),
        sa.Column("event_id", sa.String(200), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("manifest_id", sa.String(200), nullable=True),
        sa.Column("allocation_id", sa.String(200), nullable=True),
        sa.Column("payment_session_id", sa.String(200), nullable=True),
        sa.Column("request_id", sa.String(200), nullable=True),
        sa.Column("pipeline", sa.String(200), nullable=True),
        sa.Column("computed_fee_eth", sa.String(64), nullable=True),
        sa.Column("computed_fee_usd", sa.String(64), nullable=True),
        sa.Column("currency", sa.String(8), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("synced_at", UTCDateTime(), nullable=False),
    )
    op.create_index("lpb_usage_rows_allocation", "lpb_usage_rows", ["allocation_id"])
    op.create_index("lpb_usage_rows_session", "lpb_usage_rows", ["payment_session_id"])
    op.create_table(
        "lpb_cost_cursor",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("resume_cursor", sa.Text(), nullable=True),
        sa.Column("filters", sa.JSON(), nullable=False),
    )
    op.create_table(
        "lpb_cost_projections",
        sa.Column(
            "job_id", sa.String(36), sa.ForeignKey("lpb_jobs.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("fee_eth", sa.String(64), nullable=False),
        sa.Column("fee_usd", sa.String(64), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("first_observed_at", UTCDateTime(), nullable=True),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("lpb_cost_projections")
    op.drop_table("lpb_cost_cursor")
    op.drop_index("lpb_usage_rows_session", table_name="lpb_usage_rows")
    op.drop_index("lpb_usage_rows_allocation", table_name="lpb_usage_rows")
    op.drop_table("lpb_usage_rows")
    op.drop_table("lpb_secrets")
    op.drop_table("lpb_actors")
