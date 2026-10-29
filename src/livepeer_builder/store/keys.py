"""Engine-issued access keys in SQL (``lpb_access_keys``): the token's hash is stored, never the token."""

from datetime import datetime
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from livepeer_builder.contracts import AccessKey, OperationRecord
from livepeer_builder.store import schema as t
from livepeer_builder.store.operations import insert_operation, read_operation


class SqlKeys:
    """``KeyRepo`` for ``SqlStore``."""

    engine: AsyncEngine

    async def insert_access_key(
        self, key: AccessKey, token_hash: str, operation: OperationRecord | None = None
    ) -> bool:
        values = key.model_dump() | {"permissions": list(key.permissions), "token_hash": token_hash}
        try:
            async with self.engine.begin() as conn:
                if operation is not None:  # the key and its ledger record commit together (D57)
                    await insert_operation(conn, operation)
                await conn.execute(insert(t.access_keys).values(values))
        except IntegrityError:
            if operation is None or not await self._held(operation):
                raise
            return False
        return True

    async def _held(self, operation: OperationRecord) -> bool:
        async with self.engine.connect() as conn:
            found = await read_operation(
                conn, operation.application_id, operation.actor_id, operation.operation_ref
            )
        return found is not None

    async def access_key_by_hash(self, token_hash: str) -> AccessKey | None:
        query = select(t.access_keys).where(t.access_keys.c.token_hash == token_hash)
        async with self.engine.connect() as conn:
            row = (await conn.execute(query)).mappings().first()
        return _access_key(row) if row else None

    async def revoke_access_key(self, key_id: str, revoked_at: datetime) -> bool:
        query = (
            update(t.access_keys)
            .where(t.access_keys.c.key_id == key_id, t.access_keys.c.revoked_at.is_(None))
            .values(revoked_at=revoked_at)
        )
        async with self.engine.begin() as conn:
            return (await conn.execute(query)).rowcount > 0

    async def get_access_key(self, key_id: str) -> AccessKey | None:
        query = select(t.access_keys).where(t.access_keys.c.key_id == key_id)
        async with self.engine.connect() as conn:
            row = (await conn.execute(query)).mappings().first()
        return _access_key(row) if row else None

    async def rotate_access_key(
        self,
        old_key_id: str,
        new_key: AccessKey,
        new_token_hash: str,
        revoked_at: datetime,
        operation: OperationRecord | None = None,
    ) -> bool:
        revoke = (
            update(t.access_keys)
            .where(t.access_keys.c.key_id == old_key_id, t.access_keys.c.revoked_at.is_(None))
            .values(revoked_at=revoked_at)
        )
        values = new_key.model_dump() | {
            "permissions": list(new_key.permissions),
            "token_hash": new_token_hash,
        }
        try:
            async with self.engine.begin() as conn:
                if (await conn.execute(revoke)).rowcount == 0:
                    return False
                if operation is not None:  # one transaction: revoke, record, insert (D44, D57)
                    await insert_operation(conn, operation)
                await conn.execute(insert(t.access_keys).values(values))
        except IntegrityError:
            if operation is None or not await self._held(operation):
                raise
            return False
        return True

    async def list_access_keys(self, application_id: str | None = None) -> list[AccessKey]:
        query = select(t.access_keys).order_by(t.access_keys.c.created_at)
        if application_id is not None:
            query = query.where(t.access_keys.c.application_id == application_id)
        async with self.engine.connect() as conn:
            return [_access_key(row) for row in (await conn.execute(query)).mappings()]

    async def count_active_admin_keys(self) -> int:
        query = select(func.count()).where(
            t.access_keys.c.kind == "admin", t.access_keys.c.revoked_at.is_(None)
        )
        async with self.engine.connect() as conn:
            return int((await conn.execute(query)).scalar_one())


def _access_key(row: Any) -> AccessKey:
    data = {key: row[key] for key in AccessKey.model_fields}
    data["permissions"] = tuple(row["permissions"])
    return AccessKey.model_validate(data)
