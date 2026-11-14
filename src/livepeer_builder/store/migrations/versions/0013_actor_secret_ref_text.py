"""An actor's secret reference is unbounded text (#115).

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-13

``lpb_actors.secret_ref`` was ``varchar(200)``. A ``SecretRef`` is opaque: a supplied secret store (a
vault) may return a path longer than that. SQLite ignores the length, but PostgreSQL enforces it, and the
actor row is written after the provider key is funded, so a longer reference left a funded allocation
with no record. The column becomes ``text``. Existing rows keep their references.

PostgreSQL changes the type in place (``ALTER COLUMN ... TYPE TEXT``, no rewrite of the rows). SQLite has
no such statement: Alembic's batch mode copies the rows into a new table with the same key, constraints
and defaults. A downgrade puts ``varchar(200)`` back; on PostgreSQL it fails, changing nothing, while any
stored reference is longer than 200 characters.

SQLite creates that copy (``_alembic_tmp_lpb_actors``) outside the migration's transaction, so a run that
dies before its commit rolls back and still leaves the empty copy behind, and the next run failed with
"table _alembic_tmp_lpb_actors already exists". Each direction therefore drops a leftover copy first, and
only while ``lpb_actors`` itself exists: a copy without it would hold the only rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _drop_leftover_copy() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite" and sa.inspect(bind).has_table("lpb_actors"):
        op.execute("DROP TABLE IF EXISTS _alembic_tmp_lpb_actors")


def upgrade() -> None:
    _drop_leftover_copy()
    with op.batch_alter_table("lpb_actors") as batch:
        batch.alter_column(
            "secret_ref", existing_type=sa.String(200), type_=sa.Text(), existing_nullable=False
        )


def downgrade() -> None:
    _drop_leftover_copy()
    with op.batch_alter_table("lpb_actors") as batch:
        batch.alter_column(
            "secret_ref", existing_type=sa.Text(), type_=sa.String(200), existing_nullable=False
        )
