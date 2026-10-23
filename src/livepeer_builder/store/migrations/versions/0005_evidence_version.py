"""Version each job's payment evidence, and record the version each cost projection was computed from.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-23

Cost sync commits usage rows, then session matches, then projections, one transaction each, and two
syncs can run at once (the loop and POST /v1/admin/costs/sync). Every change to a job's evidence (a new
row on a matched session, a changed match, a late session report) increments the job's
evidence_version in the same transaction. A projection stands for its job only while its
evidence_version is current, so a read never serves a projection that misses committed usage, and the
next sync stores the job again even after a crash or a concurrent save (#43 review).
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
        "lpb_jobs", sa.Column("evidence_version", sa.Integer(), nullable=False, server_default=sa.text("0"))
    )
    op.add_column(
        "lpb_cost_projections",
        sa.Column("evidence_version", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("lpb_cost_projections", "evidence_version")
    op.drop_column("lpb_jobs", "evidence_version")
