"""Alembic migrations shipped inside the package, run on the engine's own connection.

``lpb migrate`` and ``SqlStore.connect(migrate=True)`` both land here. The migrations directory is
package data, so installed wheels migrate exactly like a source checkout (the livepeer-core
prototype lost its migrations when installed; tests/store/test_packaging.py guards this).
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from livepeer_builder.store.schema import VERSION_TABLE

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config(connection: Connection | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.attributes["connection"] = connection
    return config


def head_revision() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    if head is None:
        raise RuntimeError(f"no migrations found in {MIGRATIONS_DIR}")
    return head


def schema_mismatch(database_revision: str | None, database: str) -> str | None:
    """Why ``database`` cannot run with this package, or None when its schema is at this package's head.

    The message names the database that was checked: ``lpb migrate`` migrates ``LPB_DATABASE_URL``, and a
    supplied store may be another one. It says to migrate only when that can work: a revision this
    package does not know was made by another release, and ``lpb migrate`` from here cannot change it."""
    package = head_revision()
    if database_revision == package:
        return None
    here = f"the database {database} is at schema revision {database_revision or 'none'}"
    if database_revision is not None and not _known(database_revision):
        return (
            f"{here}, which this package (at {package}) does not know: another release migrated it. "
            "Start that release instead; this package cannot migrate it (LPB_AUTO_MIGRATE is false)"
        )
    return (
        f"{here} and this package needs {package}: run `lpb migrate` with this package, with "
        "LPB_DATABASE_URL pointing at that database, before starting (LPB_AUTO_MIGRATE is false)"
    )


def _known(revision: str) -> bool:
    try:
        return ScriptDirectory.from_config(alembic_config()).get_revision(revision) is not None
    except CommandError:
        return False


async def upgrade(engine: AsyncEngine, revision: str = "head") -> None:
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade, revision)


async def downgrade(engine: AsyncEngine, revision: str) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(_downgrade, revision)


async def current_revision(engine: AsyncEngine) -> str | None:
    async with engine.connect() as conn:
        return await conn.run_sync(_current)


def _upgrade(connection: Connection, revision: str) -> None:
    command.upgrade(alembic_config(connection), revision)


def _downgrade(connection: Connection, revision: str) -> None:
    command.downgrade(alembic_config(connection), revision)


def _current(connection: Connection) -> str | None:
    return MigrationContext.configure(
        connection, opts={"version_table": VERSION_TABLE}
    ).get_current_revision()
