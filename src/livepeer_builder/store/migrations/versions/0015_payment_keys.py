"""The payment keys a clearinghouse issues (D87, #166).

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-18

A payment key is what an enterprise's engine presents when it asks a clearinghouse to sign a payment for
one actor. The clearinghouse issues it when it allots the actor's allocation, replaces it when the payment
key is rotated and removes it when the allocation is revoked. ``lpb_payment_keys`` keeps the SHA-256 hash
of the token, never the token, as ``lpb_access_keys`` does: one row for each actor, with the allocation
the key was issued for.

The revision adds that table and changes no other: every existing row stays as it is. An allocation
allotted before this revision has no payment key until its payment key is rotated. A downgrade drops the
table, and the payment keys with it; the allocations and the Batteries keys they pay with are untouched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lpb_payment_keys",
        sa.Column("tenant_id", sa.String(200), primary_key=True),
        sa.Column("actor_id", sa.String(200), primary_key=True),
        sa.Column("allocation_id", sa.String(200), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("lpb_payment_keys")
