"""Actors and the people an identity provider signs in (D46, D48), on one connection."""

from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from livepeer_builder.contracts import ActorAllocation, AllocationStatus, SecretRef, SeenActor
from livepeer_builder.store import schema as t


async def get_actor(conn: AsyncConnection, tenant_id: str, actor_id: str) -> ActorAllocation | None:
    query = select(t.actors).where(t.actors.c.tenant_id == tenant_id, t.actors.c.actor_id == actor_id)
    row = (await conn.execute(query)).mappings().first()
    return _actor(row) if row else None


async def save_actor(conn: AsyncConnection, actor: ActorAllocation) -> None:
    values = _values(actor)
    key = and_(t.actors.c.tenant_id == actor.tenant_id, t.actors.c.actor_id == actor.actor_id)
    result = await conn.execute(update(t.actors).where(key).values(values))
    if result.rowcount == 0:
        await conn.execute(insert(t.actors).values(values))


class SqlActorCommands:
    """``ActorCommandRepo`` for ``SqlStore`` (#115, C4): each admin command's write names its own columns
    and carries its condition in the ``WHERE``, so it cannot overwrite a command that finished after it
    read the actor. The row is read back in the same transaction as the write. Allotment's whole-row
    write is an insert, which the primary key refuses when the actor has a row, or an update of the
    revoked row it read."""

    engine: AsyncEngine

    async def set_actor_status(
        self,
        tenant_id: str,
        actor_id: str,
        allocation_id: str,
        status: AllocationStatus,
        updated_at: datetime,
        *,
        only_from: AllocationStatus | None = None,
    ) -> ActorAllocation | None:
        held = _held(tenant_id, actor_id, allocation_id)
        if only_from is not None:
            held = and_(held, t.actors.c.status == only_from)
        return await self._write(held, tenant_id, actor_id, {"status": status, "updated_at": updated_at})

    async def set_actor_key(
        self,
        tenant_id: str,
        actor_id: str,
        allocation_id: str,
        api_key_id: str,
        secret_ref: SecretRef,
        updated_at: datetime,
        only_from: str,
    ) -> ActorAllocation | None:
        # the key the rotation began from, or its own key already: any other is a finished rotation's
        held = and_(
            _held(tenant_id, actor_id, allocation_id), t.actors.c.api_key_id.in_((only_from, api_key_id))
        )
        values = {"api_key_id": api_key_id, "secret_ref": secret_ref.ref, "updated_at": updated_at}
        return await self._write(held, tenant_id, actor_id, values)

    async def allot_actor(self, actor: ActorAllocation, *, replacing: str | None) -> bool:
        c = t.actors.c
        if replacing is None:
            try:
                async with self.engine.begin() as conn:
                    await conn.execute(insert(t.actors).values(_values(actor)))
                return True
            except IntegrityError:
                async with self.engine.connect() as conn:
                    if await get_actor(conn, actor.tenant_id, actor.actor_id) is None:
                        raise  # not this actor's row: the allocation is stored as another actor's
                return False  # another allotment inserted the row first
        read = and_(
            c.tenant_id == actor.tenant_id,
            c.actor_id == actor.actor_id,
            c.allocation_id == replacing,
            c.status == "revoked",
        )
        async with self.engine.begin() as conn:
            return (await conn.execute(update(t.actors).where(read).values(_values(actor)))).rowcount == 1

    async def _write(
        self, held: ColumnElement[bool], tenant_id: str, actor_id: str, values: dict[str, Any]
    ) -> ActorAllocation | None:
        async with self.engine.begin() as conn:
            written = await conn.execute(update(t.actors).where(held).values(values))
            return await get_actor(conn, tenant_id, actor_id) if written.rowcount == 1 else None


def _held(tenant_id: str, actor_id: str, allocation_id: str) -> ColumnElement[bool]:
    """The actor still holds the allocation the command acted on and is not revoked: no command moves an
    actor out of ``revoked``, and none writes onto an allocation allotted after a revoke."""
    c = t.actors.c
    return and_(
        c.tenant_id == tenant_id,
        c.actor_id == actor_id,
        c.allocation_id == allocation_id,
        c.status != "revoked",
    )


async def list_actors(conn: AsyncConnection, tenant_id: str | None) -> list[ActorAllocation]:
    query = select(t.actors).order_by(t.actors.c.tenant_id, t.actors.c.actor_id)
    if tenant_id is not None:
        query = query.where(t.actors.c.tenant_id == tenant_id)
    return [_actor(row) for row in (await conn.execute(query)).mappings()]


async def save_sighting(conn: AsyncConnection, seen: SeenActor) -> None:
    key = and_(
        t.actor_sightings.c.tenant_id == seen.tenant_id,
        t.actor_sightings.c.actor_id == seen.actor_id,
    )
    # the first sighting's time is kept; an empty name (the claim switched off) keeps the last one
    changes: dict[str, Any] = {"last_seen_at": seen.last_seen_at} | ({"name": seen.name} if seen.name else {})
    changed = await conn.execute(update(t.actor_sightings).where(key).values(changes))
    if changed.rowcount == 0:
        try:
            async with conn.begin_nested():
                await conn.execute(insert(t.actor_sightings).values(seen.model_dump()))
        except IntegrityError:  # another request recorded the first sighting just now
            await conn.execute(update(t.actor_sightings).where(key).values(changes))


async def get_sighting(conn: AsyncConnection, tenant_id: str, actor_id: str) -> SeenActor | None:
    query = select(t.actor_sightings).where(
        t.actor_sightings.c.tenant_id == tenant_id, t.actor_sightings.c.actor_id == actor_id
    )
    row = (await conn.execute(query)).mappings().first()
    return SeenActor.model_validate(dict(row)) if row else None


async def list_sightings(conn: AsyncConnection, tenant_id: str | None) -> list[SeenActor]:
    query = select(t.actor_sightings).order_by(t.actor_sightings.c.tenant_id, t.actor_sightings.c.actor_id)
    if tenant_id is not None:
        query = query.where(t.actor_sightings.c.tenant_id == tenant_id)
    return [SeenActor.model_validate(dict(row)) for row in (await conn.execute(query)).mappings()]


def _values(actor: ActorAllocation) -> dict[str, Any]:
    return actor.model_dump(exclude={"secret_ref"}) | {"secret_ref": actor.secret_ref.ref}


def _actor(row: Any) -> ActorAllocation:
    data = {key: row[key] for key in ActorAllocation.model_fields if key != "secret_ref"}
    return ActorAllocation.model_validate(data | {"secret_ref": SecretRef(ref=row["secret_ref"])})
