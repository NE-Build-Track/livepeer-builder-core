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
