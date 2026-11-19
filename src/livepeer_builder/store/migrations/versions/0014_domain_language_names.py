"""Records are named in the domain language (D83, #163).

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-18

The account an enterprise app holds is a tenant, and the kind of credential that runs the deployment is
``clearinghouse_admin``. Records called them an application and ``admin``.

- ``application_id`` becomes ``tenant_id`` in ``lpb_access_keys``, ``lpb_jobs``, ``lpb_actors``,
  ``lpb_actor_sightings`` and ``lpb_operations``. The column is renamed in place (``ALTER TABLE ... RENAME``
  on SQLite and on PostgreSQL), so every row, primary key and index stays as it is. No index or constraint
  is named after the column.
- An access key of kind ``admin`` becomes ``clearinghouse_admin``, active or revoked. The new kind is 19
  characters and ``lpb_access_keys.kind`` held 16, so the column becomes ``varchar(32)`` first. PostgreSQL
  changes the type in place. SQLite has no such statement: Alembic's batch mode copies the rows into a new
  table with the same key, constraints and defaults, as revision 0013 did for ``lpb_actors``.
- The command a ledger record names in ``lpb_operations.command`` follows the command's new name:
  ``provision`` becomes ``allot``.
- The result an admin command recorded in ``lpb_operations.result`` is the answer a repeat gets back. It
  names the tenant, and for an issued or rotated access key the kind, in the same words, so both are
  renamed inside the stored JSON too.

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
ADMIN, CLEARINGHOUSE_ADMIN = "admin", "clearinghouse_admin"
KEY_COMMANDS = ("issue_key", "rotate_access_key")  # their recorded result is an access key
COMMANDS = {"provision": "allot"}


def _in_one_transaction() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DELETE FROM lpb_operations WHERE 1 = 0")


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _rename_column(table: str, old: str, new: str) -> None:
    columns = _columns(table)
    if old in columns and new not in columns:
        op.alter_column(table, old, new_column_name=new)


def _resize_kind(old: int, new: int) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":  # a copy an interrupted run of an older release left behind
        op.execute("DROP TABLE IF EXISTS _alembic_tmp_lpb_access_keys")
    with op.batch_alter_table("lpb_access_keys") as batch:
        batch.alter_column(
            "kind", existing_type=sa.String(old), type_=sa.String(new), existing_nullable=False
        )


def _rename_kind(old: str, new: str) -> None:
    keys = sa.table("lpb_access_keys", sa.column("kind", sa.String))
    op.execute(sa.update(keys).where(keys.c.kind == old).values(kind=new))


def _rename_commands(names: dict[str, str]) -> None:
    operations = sa.table("lpb_operations", sa.column("command", sa.String))
    for old, new in names.items():
        op.execute(sa.update(operations).where(operations.c.command == old).values(command=new))


def _rewrite_results(fields: dict[str, str], kinds: dict[str, str]) -> None:
    """Rename the fields of each recorded result (a JSON object, or null), and the kind of a recorded
    access key. Row by row: the JSON functions of SQLite and PostgreSQL differ, and the ledger holds one
    row for each admin command ever run."""
    scope = "tenant_id" if "tenant_id" in _columns("lpb_operations") else "application_id"
    operations = sa.table(
        "lpb_operations",
        sa.column(scope, sa.String),
        sa.column("actor_id", sa.String),
        sa.column("operation_ref", sa.String),
        sa.column("command", sa.String),
        sa.column("result", sa.JSON),
    )
    key = (operations.c[scope], operations.c.actor_id, operations.c.operation_ref)
    bind = op.get_bind()
    recorded = sa.select(*key, operations.c.command, operations.c.result)
    for *identity, command, result in bind.execute(recorded).all():
        if not isinstance(result, dict):
            continue
        renamed: dict[str, Any] = {fields.get(str(name), str(name)): value for name, value in result.items()}
        if command in KEY_COMMANDS and renamed.get("kind") in kinds:
            renamed["kind"] = kinds[renamed["kind"]]
        if renamed != result:
            where = sa.and_(*(column == value for column, value in zip(key, identity, strict=True)))
            bind.execute(sa.update(operations).where(where).values(result=renamed))


def upgrade() -> None:
    _in_one_transaction()
    for table in TENANT_TABLES:
        _rename_column(table, "application_id", "tenant_id")
    _resize_kind(16, 32)
    _rename_kind(ADMIN, CLEARINGHOUSE_ADMIN)
    _rename_commands(COMMANDS)
    _rewrite_results({"application_id": "tenant_id"}, {ADMIN: CLEARINGHOUSE_ADMIN})


def downgrade() -> None:
    _in_one_transaction()
    _rewrite_results({"tenant_id": "application_id"}, {CLEARINGHOUSE_ADMIN: ADMIN})
    _rename_commands({new: old for old, new in COMMANDS.items()})
    _rename_kind(CLEARINGHOUSE_ADMIN, ADMIN)
    _resize_kind(32, 16)
    for table in TENANT_TABLES:
        _rename_column(table, "tenant_id", "application_id")
