"""Mark a cost projection stale when sync changes its evidence before storing it again.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-23

Cost sync commits usage rows, then session matches, then projections, one transaction each. A new row
on a matched session, or a changed match, marks the job's projection stale in the same transaction,
so a cost read never serves a projection that misses committed usage, and the next sync stores the job
again even after a crash in between (#43 review).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "lpb_cost_projections",
        sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("lpb_cost_projections", "stale")
