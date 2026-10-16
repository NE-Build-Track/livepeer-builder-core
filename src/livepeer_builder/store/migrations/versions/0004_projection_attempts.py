"""Persist attempt-level cost classifications on each projection.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-16

Cost reads use the stored projection. Revision 0003 already replaced attempt auth ids with the
session log; this revision only adds the attempt list.

Railway databases migrated by gateway-integration-lab builds ran an earlier 0003 that already added
the attempt list, so the column is only added when it is missing.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("lpb_cost_projections")}
    if "attempts" in columns:
        return
    op.add_column(
        "lpb_cost_projections",
        sa.Column("attempts", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )


def downgrade() -> None:
    op.drop_column("lpb_cost_projections", "attempts")
