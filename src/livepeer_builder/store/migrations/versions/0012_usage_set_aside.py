"""Usage rows cost sync set aside (#114, C13).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-13

One provider usage row the engine could not read (an unknown status, no timestamp) or store (a text
longer than its column, a NUL) failed its page on every pass, so the cursor never moved. Cost sync now
leaves such a row out and records it here: the provider's row id, the reason and the fee it stated, so
reconciliation can name what is missing. The row's content goes to the log, not to this table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from livepeer_builder.store.schema import UTCDateTime

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lpb_usage_set_aside",
        sa.Column("id", sa.String(200), primary_key=True),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("computed_fee_eth", sa.String(64), nullable=True),
        sa.Column("computed_fee_usd", sa.String(64), nullable=True),
        sa.Column("set_aside_at", UTCDateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("lpb_usage_set_aside")
