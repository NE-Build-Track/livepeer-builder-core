"""The operation ledger in SQL (D57, ISS-11): ``lpb_operations``, one row per caller scope and reference.

The primary key is the unique constraint the reference is stored under, so two concurrent ``begin``s
for one reference cannot both win. Every later write is a compare-and-set on the row's
``fencing_token``: a run that a resume took over writes nothing.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from livepeer_builder.contracts import OperationRecord
from livepeer_builder.store import schema as t


def scope(application_id: str, actor_id: str, operation_ref: str) -> ColumnElement[bool]:
    c = t.operations.c
    return and_(c.application_id == application_id, c.actor_id == actor_id, c.operation_ref == operation_ref)


async def insert_operation(conn: AsyncConnection, record: OperationRecord) -> None:
    """Insert on ``conn``; a held reference raises IntegrityError and the caller's transaction rolls back."""
    await conn.execute(insert(t.operations).values(record.model_dump()))


async def read_operation(
    conn: AsyncConnection, application_id: str, actor_id: str, operation_ref: str
) -> OperationRecord | None:
    query = select(t.operations).where(scope(application_id, actor_id, operation_ref))
    row = (await conn.execute(query)).mappings().first()
    return _record(row) if row else None


class SqlOperations:
    """``OperationRepo`` for ``SqlStore``."""

    engine: AsyncEngine

    async def begin_operation(self, record: OperationRecord) -> OperationRecord | None:
        try:
            async with self.engine.begin() as conn:
                await insert_operation(conn, record)
            return None
        except IntegrityError:
            held = await self.get_operation(record.application_id, record.actor_id, record.operation_ref)
            if held is None:  # deleted (abandoned) between the insert and the read: try once more
                return await self.begin_operation(record)
            return held

    async def resume_operation(self, record: OperationRecord, started_at: datetime) -> OperationRecord | None:
        taken = record.model_copy(
            update={"fencing_token": record.fencing_token + 1, "started_at": started_at}
        )
        values = {"fencing_token": taken.fencing_token, "started_at": started_at}
        async with self.engine.begin() as conn:
            moved = await conn.execute(update(t.operations).where(fenced(record)).values(values))
        return taken if moved.rowcount == 1 else None

    async def remember_operation(self, record: OperationRecord) -> bool:
        async with self.engine.begin() as conn:
            changed = await conn.execute(
                update(t.operations).where(fenced(record)).values(context=record.context)
            )
        return changed.rowcount == 1

    async def complete_operation(self, record: OperationRecord, result: Any, completed_at: datetime) -> bool:
        done = {"state": "done", "result": result, "completed_at": completed_at}
        async with self.engine.begin() as conn:
            return (await conn.execute(update(t.operations).where(fenced(record)).values(done))).rowcount == 1

    async def abandon_operation(self, record: OperationRecord) -> bool:
        async with self.engine.begin() as conn:
            return (await conn.execute(delete(t.operations).where(fenced(record)))).rowcount == 1

    async def get_operation(
        self, application_id: str, actor_id: str, operation_ref: str
    ) -> OperationRecord | None:
        async with self.engine.connect() as conn:
            return await read_operation(conn, application_id, actor_id, operation_ref)


def fenced(record: OperationRecord) -> ColumnElement[bool]:
    """The row is still the started run ``record`` belongs to: no resume took it over, nothing ended it."""
    c = t.operations.c
    return and_(
        scope(record.application_id, record.actor_id, record.operation_ref),
        c.state == "started",
        c.fencing_token == record.fencing_token,
    )


def _record(row: Any) -> OperationRecord:
    return OperationRecord.model_validate(dict(row))
