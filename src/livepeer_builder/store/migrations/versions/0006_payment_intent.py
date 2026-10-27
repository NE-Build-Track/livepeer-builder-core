"""Distinguish payment intent from confirmed signing in the session log.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "lpb_payment_sessions",
        sa.Column("payment_state", sa.String(16), nullable=False, server_default="paid"),
    )


def downgrade() -> None:
    op.drop_column("lpb_payment_sessions", "payment_state")
