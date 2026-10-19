"""Alembic environment. Migrations always run on a connection supplied by livepeer_builder.store.migrate."""

from alembic import context

from livepeer_builder.store.schema import VERSION_TABLE, metadata

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError(
        "run migrations with `lpb migrate` (livepeer_builder.store.migrate), not the alembic CLI"
    )

context.configure(
    connection=connection,
    target_metadata=metadata,
    version_table=VERSION_TABLE,
    render_as_batch=True,  # SQLite needs batch mode for ALTER TABLE
    compare_type=True,
)
with context.begin_transaction():
    context.run_migrations()
