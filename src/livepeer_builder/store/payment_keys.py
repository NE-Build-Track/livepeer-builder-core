"""The payment keys a clearinghouse issues, in SQL (``lpb_payment_keys``): the token's hash is stored,
never the token (D87, #166)."""

from datetime import datetime

from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from livepeer_builder.contracts import ActorAllocation
from livepeer_builder.store import schema as t
from livepeer_builder.store.actors import actor_from_row


class SqlPaymentKeys:
    """``PaymentKeyRepo`` for ``SqlStore``: one row for each actor, so a new key replaces the old one in
    the statement that stores it."""

    engine: AsyncEngine

    async def put_payment_key(
        self,
        tenant_id: str,
        actor_id: str,
        allocation_id: str,
        token_hash: str,
        created_at: datetime,
        *,
        only_if_absent: bool = False,
    ) -> bool:
        c = t.payment_keys.c
        actor = and_(c.tenant_id == tenant_id, c.actor_id == actor_id)
        # a first key replaces only a key of an earlier allocation; any other put replaces whatever is there
        replaced = and_(actor, c.allocation_id != allocation_id) if only_if_absent else actor
        values = {"allocation_id": allocation_id, "token_hash": token_hash, "created_at": created_at}
        async with self.engine.begin() as conn:
            if (await conn.execute(update(t.payment_keys).where(replaced).values(values))).rowcount == 1:
                return True
            try:
                async with conn.begin_nested():
                    row = values | {"tenant_id": tenant_id, "actor_id": actor_id}
                    await conn.execute(insert(t.payment_keys).values(row))
                return True
            except IntegrityError:  # the actor has a row: this allocation's, or one written just now
                if only_if_absent:
                    return False
                await conn.execute(update(t.payment_keys).where(actor).values(values))
                return True

    async def delete_payment_key(self, tenant_id: str, actor_id: str, allocation_id: str) -> None:
        c = t.payment_keys.c
        issued_for = and_(c.tenant_id == tenant_id, c.actor_id == actor_id, c.allocation_id == allocation_id)
        async with self.engine.begin() as conn:
            await conn.execute(delete(t.payment_keys).where(issued_for))

    async def actor_by_payment_key(self, token_hash: str) -> ActorAllocation | None:
        """One query, so the key and the actor are read as one state: the actor that still holds the
        allocation the key was issued for, unless that allocation is revoked."""
        key, actor = t.payment_keys.c, t.actors.c
        holds = and_(
            actor.tenant_id == key.tenant_id,
            actor.actor_id == key.actor_id,
            actor.allocation_id == key.allocation_id,
            actor.status != "revoked",
        )
        query = select(t.actors).join(t.payment_keys, holds).where(key.token_hash == token_hash)
        async with self.engine.connect() as conn:
            row = (await conn.execute(query)).mappings().first()
        return actor_from_row(row) if row else None
