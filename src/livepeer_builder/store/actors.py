"""Actors and the people an identity provider signs in (D46, D48), on one connection."""

from typing import Any

from sqlalchemy import and_, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from livepeer_builder.contracts import ProvisionedActor, SecretRef, SeenActor
from livepeer_builder.store import schema as t


async def get_actor(conn: AsyncConnection, application_id: str, actor_id: str) -> ProvisionedActor | None:
    query = select(t.actors).where(
        t.actors.c.application_id == application_id, t.actors.c.actor_id == actor_id
    )
    row = (await conn.execute(query)).mappings().first()
    return _actor(row) if row else None


async def save_actor(conn: AsyncConnection, actor: ProvisionedActor) -> None:
    values = actor.model_dump(exclude={"secret_ref"}) | {"secret_ref": actor.secret_ref.ref}
    key = and_(t.actors.c.application_id == actor.application_id, t.actors.c.actor_id == actor.actor_id)
    result = await conn.execute(update(t.actors).where(key).values(values))
    if result.rowcount == 0:
        await conn.execute(insert(t.actors).values(values))


async def list_actors(conn: AsyncConnection, application_id: str | None) -> list[ProvisionedActor]:
    query = select(t.actors).order_by(t.actors.c.application_id, t.actors.c.actor_id)
    if application_id is not None:
        query = query.where(t.actors.c.application_id == application_id)
    return [_actor(row) for row in (await conn.execute(query)).mappings()]


async def save_sighting(conn: AsyncConnection, seen: SeenActor) -> None:
    key = and_(
        t.actor_sightings.c.application_id == seen.application_id,
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


async def get_sighting(conn: AsyncConnection, application_id: str, actor_id: str) -> SeenActor | None:
    query = select(t.actor_sightings).where(
        t.actor_sightings.c.application_id == application_id, t.actor_sightings.c.actor_id == actor_id
    )
    row = (await conn.execute(query)).mappings().first()
    return SeenActor.model_validate(dict(row)) if row else None


async def list_sightings(conn: AsyncConnection, application_id: str | None) -> list[SeenActor]:
    query = select(t.actor_sightings).order_by(
        t.actor_sightings.c.application_id, t.actor_sightings.c.actor_id
    )
    if application_id is not None:
        query = query.where(t.actor_sightings.c.application_id == application_id)
    return [SeenActor.model_validate(dict(row)) for row in (await conn.execute(query)).mappings()]


def _actor(row: Any) -> ProvisionedActor:
    data = {key: row[key] for key in ProvisionedActor.model_fields if key != "secret_ref"}
    return ProvisionedActor.model_validate(data | {"secret_ref": SecretRef(ref=row["secret_ref"])})
