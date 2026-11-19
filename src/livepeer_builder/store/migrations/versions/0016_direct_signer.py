"""Remove obsolete clearinghouse payment tokens; allocation credentials belong to work engines.

Downgrade restores an empty legacy table, never invalidated credentials.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def restore_legacy_table() -> None:
    op.create_table(
        "lpb_payment_keys",
        sa.Column("tenant_id", sa.String(200), primary_key=True),
        sa.Column("actor_id", sa.String(200), primary_key=True),
        sa.Column("allocation_id", sa.String(200), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    op.execute("DELETE FROM lpb_operations WHERE 1 = 0")
    op.drop_table("lpb_payment_keys")


def downgrade() -> None:
    op.execute("DELETE FROM lpb_operations WHERE 1 = 0")
    restore_legacy_table()
