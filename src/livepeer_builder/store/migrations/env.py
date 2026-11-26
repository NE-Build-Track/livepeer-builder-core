"""Alembic environment. Migrations always run on a connection supplied by livepeer_builder.store.migrate.

A run is one transaction on every database: its tables, its columns and its revision are committed
together, or none of them is. PostgreSQL does that by itself. SQLite's driver opens a transaction at the
first ``INSERT``, ``UPDATE`` or ``DELETE`` and not at a ``CREATE TABLE`` or an ``ALTER TABLE``, so each
statement of a migration that ran before any row was written was committed at once, and a run that died
left them under the old revision (#158). On SQLite the environment therefore opens the transaction itself,
before the revision is read. ``BEGIN IMMEDIATE`` takes the database's one write lock there, so the
revision is read under the lock the run writes under: a second migrate at the same moment waits for the
first, as long as the busy timeout, and goes on from the revision that one wrote. A database already at
the revision asked for is only read, as before: an engine that starts on a migrated database takes no lock.
"""

from alembic import context
from alembic.runtime.migration import MigrationContext

from livepeer_builder.store.schema import VERSION_TABLE, metadata

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError(
        "run migrations with `lpb migrate` (livepeer_builder.store.migrate), not the alembic CLI"
    )

if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
    stored = MigrationContext.configure(connection, opts={"version_table": VERSION_TABLE})
    if stored.get_current_revision() != context.get_revision_argument():
        connection.exec_driver_sql("BEGIN IMMEDIATE")
context.configure(
    connection=connection,
    target_metadata=metadata,
    version_table=VERSION_TABLE,
    render_as_batch=True,  # SQLite needs batch mode for ALTER TABLE
    compare_type=True,
)
with context.begin_transaction():
    context.run_migrations()
