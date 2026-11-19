"""The clearinghouse half's routes, all under ``/v1/clearinghouse``: the actor list, an actor's
allocation and its commands (allot, top up, status, revoke, rotate the payment key), the grant desk and
the grant top-up.

One unit, mounted by itself or beside the work set (D84). It is typed against ``ClearinghouseApi``, so it
cannot run a job. Routes are thin: each authenticates, then makes one engine call, and authorization
lives in the services' AccessPolicy checks.
"""

from typing import Annotated

from fastapi import Depends, FastAPI, Path, Query, Response

from livepeer_builder.api import ClearinghouseApi
from livepeer_builder.contracts import (
    ActorAllocation,
    ActorContext,
    AllocationStatus,
    AllotRequest,
    GrantRecord,
    KnownActor,
    TopUpRequest,
)
from livepeer_builder.contracts.common import StoredText
from livepeer_builder.http.auth import actor_dependency
from livepeer_builder.http.bodies import AllotBody, GrantBody, RotateBody, StatusBody
from livepeer_builder.http.params import PathId, QueryId
from livepeer_builder.http.replies import replay


def mount_clearinghouse_routes(app: FastAPI, engine: ClearinghouseApi) -> None:
    """Add the clearinghouse half's routes to ``app``."""
    Actor = Annotated[ActorContext, Depends(actor_dependency(engine))]

    @app.get("/v1/clearinghouse/actors", response_model=list[KnownActor])
    async def list_actors(
        actor: Actor,
        tenant_id: QueryId = None,
        status: AllocationStatus | None = None,
        allotted: bool | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    ) -> list[KnownActor]:
        """Allotted actors and people an identity provider signed in (D46, D48)."""
        return await engine.payments.list_actors(
            actor, tenant_id=tenant_id, status=status, allotted=allotted, limit=limit
        )

    base = "/v1/clearinghouse/tenants/{tenant_id}/actors/{actor_id}/allocation"

    @app.get(base, response_model=ActorAllocation)
    async def get_actor(actor: Actor, tenant_id: PathId, actor_id: PathId) -> ActorAllocation:
        return await engine.payments.get_actor(actor, tenant_id, actor_id)

    @app.post(base, response_model=ActorAllocation, status_code=201)
    async def allot(
        actor: Actor,
        tenant_id: Annotated[StoredText, Path()],  # as stored: a 422 here, before money moves
        actor_id: Annotated[StoredText, Path()],
        body: AllotBody,
        response: Response,
    ) -> ActorAllocation:
        request = AllotRequest(
            tenant_id=tenant_id,
            actor_id=actor_id,
            amount=body.amount,
            label=body.label,
            grant_id=body.grant_id,
            operation_ref=body.operation_ref,
        )
        return replay(response, await engine.payments.allot(actor, request))

    @app.post(base + "/top-up", response_model=ActorAllocation)
    async def top_up(
        actor: Actor, tenant_id: PathId, actor_id: PathId, body: TopUpRequest, response: Response
    ) -> ActorAllocation:
        return replay(response, await engine.payments.top_up(actor, tenant_id, actor_id, body))

    if engine.payments.grants is not None:  # the grant desk is off: no list or create routes (decision 7)
        desk = engine.payments.grants

        @app.get("/v1/clearinghouse/grants", response_model=list[GrantRecord])
        async def list_grants(actor: Actor) -> list[GrantRecord]:
            return await desk.list_grants(actor)

        @app.post("/v1/clearinghouse/grants", response_model=GrantRecord, status_code=201)
        async def create_grant(actor: Actor, body: GrantBody, response: Response) -> GrantRecord:
            created = await desk.create_grant(actor, body.name, body.amount, operation_ref=body.operation_ref)
            return replay(response, created)

    @app.post("/v1/clearinghouse/grant/top-up", status_code=204)
    async def top_up_grant(actor: Actor, body: TopUpRequest) -> Response:
        await engine.payments.top_up_grant(actor, body)
        response = Response(status_code=204)
        return replay(response, response)

    @app.post(base + "/status", response_model=ActorAllocation)
    async def set_status(
        actor: Actor, tenant_id: PathId, actor_id: PathId, body: StatusBody
    ) -> ActorAllocation:
        return await engine.payments.set_status(actor, tenant_id, actor_id, body.status)

    @app.post(base + "/revoke", response_model=ActorAllocation)
    async def revoke(actor: Actor, tenant_id: PathId, actor_id: PathId) -> ActorAllocation:
        return await engine.payments.revoke(actor, tenant_id, actor_id)

    @app.post(base + "/rotate-payment-key", response_model=ActorAllocation)
    async def rotate_payment_key(
        actor: Actor, tenant_id: PathId, actor_id: PathId, body: RotateBody, response: Response
    ) -> ActorAllocation:
        rotated = await engine.payments.rotate_payment_key(actor, tenant_id, actor_id, body.operation_ref)
        return replay(response, rotated)
