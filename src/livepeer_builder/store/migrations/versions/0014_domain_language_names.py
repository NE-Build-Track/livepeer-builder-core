"""Records are named in the domain language (D83, #163).

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-18

The account an enterprise app holds is a tenant. Records called it an application.

- ``application_id`` becomes ``tenant_id`` in ``lpb_access_keys``, ``lpb_jobs``, ``lpb_actors``,
  ``lpb_actor_sightings`` and ``lpb_operations``. The column is renamed in place (``ALTER TABLE ... RENAME``
  on SQLite and on PostgreSQL), so every row, primary key and index stays as it is. No index or constraint
  is named after the column.
- The result an admin command recorded in ``lpb_operations.result`` is the answer a repeat gets back. It
  names the tenant in the same field, so that key is renamed inside the stored JSON too.

Every row is kept, and so is every value except the names above. ``lpb_operations.fingerprint`` is not
touched: the ledger still hashes a request as it was first written (``services/ledger.py``), so the repeat
of a command recorded before this revision matches its record.

A run is one transaction, in both directions: one that dies before its commit changes nothing. SQLite's
driver opens a transaction at the first ``INSERT``, ``UPDATE`` or ``DELETE`` and not at an ``ALTER TABLE``,
so a rename that ran before any of those was committed at once, and a run that died later left renamed
columns under the old revision. Each direction therefore starts with a ``DELETE`` that matches no row. Each
step also looks at the database first and does only what is left to do. A downgrade puts the old names
back.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = ("lpb_access_keys", "lpb_jobs", "lpb_actors", "lpb_actor_sightings", "lpb_operations")


def _in_one_transaction() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DELETE FROM lpb_operations WHERE 1 = 0")


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _rename_column(table: str, old: str, new: str) -> None:
    columns = _columns(table)
    if old in columns and new not in columns:
        op.alter_column(table, old, new_column_name=new)


def _rewrite_results(rewrite: dict[str, str]) -> None:
    """Rename the keys of each recorded result (a JSON object, or null), row by row: the JSON functions of
    SQLite and PostgreSQL differ, and the ledger holds one row for each admin command ever run."""
    scope = "tenant_id" if "tenant_id" in _columns("lpb_operations") else "application_id"
    operations = sa.table(
        "lpb_operations",
        sa.column(scope, sa.String),
        sa.column("actor_id", sa.String),
        sa.column("operation_ref", sa.String),
        sa.column("result", sa.JSON),
    )
    key = (operations.c[scope], operations.c.actor_id, operations.c.operation_ref)
    bind = op.get_bind()
    for *identity, result in bind.execute(sa.select(*key, operations.c.result)).all():
        if not isinstance(result, dict) or not rewrite.keys() & result.keys():
            continue
        renamed: dict[str, Any] = {rewrite.get(str(name), str(name)): value for name, value in result.items()}
        where = sa.and_(*(column == value for column, value in zip(key, identity, strict=True)))
        bind.execute(sa.update(operations).where(where).values(result=renamed))


def upgrade() -> None:
    _in_one_transaction()
    for table in TENANT_TABLES:
        _rename_column(table, "application_id", "tenant_id")
    _rewrite_results({"application_id": "tenant_id"})


def downgrade() -> None:
    _in_one_transaction()
    _rewrite_results({"tenant_id": "application_id"})
    for table in TENANT_TABLES:
        _rename_column(table, "tenant_id", "application_id")
