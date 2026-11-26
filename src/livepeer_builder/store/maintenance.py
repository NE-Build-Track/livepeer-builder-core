"""``SqlStore``'s maintenance capability: retention purges and the Alembic schema revision."""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncEngine

from livepeer_builder.contracts import PurgeReport
from livepeer_builder.store.migrate import current_revision, schema_mismatch
from livepeer_builder.store.retention import purge


class SqlMaintenance:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def purge(self, now: datetime, jobs_days: int | None, usage_days: int | None) -> PurgeReport:
        return await purge(self._engine, now, jobs_days, usage_days)

    async def schema_revision(self) -> str | None:
        return await current_revision(self._engine)

    @property
    def database(self) -> str:
        """The database this store is on, for a message: its URL with the password hidden."""
        return self._engine.url.render_as_string(hide_password=True)

    async def schema_mismatch(self) -> str | None:
        """Why this database cannot run with this package's migrations, or None when it is current."""
        return schema_mismatch(await self.schema_revision(), self.database)
