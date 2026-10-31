"""Store a payment intent before signing: a session's payment state (D54).

Rows written before this revision are signatures the signer confirmed, so they default to ``paid``.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-28
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
