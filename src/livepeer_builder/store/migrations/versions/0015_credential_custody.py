"""Record who holds each actor's allocation credential (D85 point 1, D87 point 1).

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-23

``lpb_actors.credential_custody`` is ``secret_store`` (the configured store issued ``secret_ref``) or
``work_engine`` (the credential was delivered to the work engine once, and ``secret_ref`` is an opaque
receipt no store resolves). Every existing row was written through the configured store, so it becomes
``secret_store``. Nothing is inferred from the text of ``secret_ref``: a supplied store may issue any
handle, a ``delivered:`` one included (#186 review). A downgrade drops the column.

This revision replaces the stack's earlier 0015 (a ``lpb_payment_keys`` table for clearinghouse-issued
payment keys) and 0016 (which dropped it again). Neither was released: a database that ran them is at a
revision that no longer exists and is recreated.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _in_one_transaction() -> None:
    """Open the driver's transaction before the ``ALTER TABLE``, as 0014 does: SQLite's driver commits an
    ``ALTER TABLE`` that runs before any row is written, so a run that died later could not roll it back."""
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DELETE FROM lpb_operations WHERE 1 = 0")


def upgrade() -> None:
    _in_one_transaction()
    op.add_column(
        "lpb_actors",
        sa.Column("credential_custody", sa.String(16), nullable=False, server_default="secret_store"),
    )


def downgrade() -> None:
    _in_one_transaction()
    op.drop_column("lpb_actors", "credential_custody")
