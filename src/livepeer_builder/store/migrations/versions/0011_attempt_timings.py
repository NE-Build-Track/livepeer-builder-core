"""Where a runner call's time went: the unpaid request, the signer, the answered request (#89).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-31

Three nullable columns on each attempt, in milliseconds. An attempt from before this has none.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("challenge_ms", "signing_ms", "response_ms")


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column("lpb_attempts", sa.Column(name, sa.Integer, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("lpb_attempts") as batch:
        for name in reversed(_COLUMNS):
            batch.drop_column(name)
