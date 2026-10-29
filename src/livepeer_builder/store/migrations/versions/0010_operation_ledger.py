"""The operation ledger: repeat-safe admin commands (D57, ISS-11).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-29

Provision, fund, fund grant, create grant, rotate the payment key, and issue or rotate an access key
each take a caller-chosen operation_ref. The engine records it here, unique per caller scope, before
the first provider call (started) and with the public result after the last step (done), so a repeat
is answered from this record. The record never holds a secret.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from livepeer_builder.store.schema import UTCDateTime

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lpb_operations",
        sa.Column("application_id", sa.String(200), primary_key=True),
        sa.Column("actor_id", sa.String(200), primary_key=True),
        sa.Column("operation_ref", sa.String(200), primary_key=True),
        sa.Column("command", sa.String(32), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("fencing_token", sa.Integer, nullable=False),
        sa.Column("context", sa.JSON, nullable=False),
        sa.Column("result", sa.JSON, nullable=True),
        sa.Column("started_at", UTCDateTime(), nullable=False),
        sa.Column("completed_at", UTCDateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("lpb_operations")
