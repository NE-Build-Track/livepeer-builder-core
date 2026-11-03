"""``SqlStore``'s maintenance capability: retention purges and the Alembic schema revision."""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncEngine

from livepeer_builder.contracts import PurgeReport
from livepeer_builder.store.migrate import current_revision
from livepeer_builder.store.retention import purge


class SqlMaintenance:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def purge(self, now: datetime, jobs_days: int | None, usage_days: int | None) -> PurgeReport:
        return await purge(self._engine, now, jobs_days, usage_days)

    async def schema_revision(self) -> str | None:
        return await current_revision(self._engine)
