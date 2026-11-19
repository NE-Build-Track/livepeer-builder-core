"""The clearinghouse half's routes, under ``/v1/clearinghouse``: tenants and their grants, a tenant's actors
and their allocations (allot, top up, status, revoke, rotate the payment key), a tenant's access keys and
its usage, and the runner list the clearinghouse publishes (#165, #166).

One unit, mounted by itself or beside the work set (D84). It is typed against ``ClearinghouseApi``, so it
cannot run a job. Routes are thin: each authenticates, resolves the tenant it acts on, then makes one
engine call, and authorization lives in the services' AccessPolicy checks.

**A tenant's routes come in two forms** (D87 point 3, as Mike amended it on 19 November 2026), and both
call the same service with the tenant they resolved:

- **its own**, at paths that name no tenant (``GET /v1/clearinghouse/grant``,
  ``/v1/clearinghouse/actors/...``, ``/v1/clearinghouse/keys/...``, ``GET /v1/clearinghouse/usage``):
  the tenant is the credential's. A tenant admin or auditor credential identifies one tenant, so these
  take those two kinds only, and refuse a clearinghouse admin, which has no tenant of its own (403);
- **the one a clearinghouse admin names**, at the same paths under
  ``/v1/clearinghouse/tenants/{tenant_id}``.

The set is five groups, each one function, so a deployment can serve them on different listeners (#167):

- ``mount_tenant_routes``: a tenant's own routes, the tenant from the credential;
- ``mount_named_tenant_routes``: the same routes with the tenant in the path;
- ``mount_runner_list_routes``: the runner list, which any credential of a tenant reads;
- ``mount_clearinghouse_admin_routes``: what only a clearinghouse admin does (onboarding, the tenant
  list, a grant's top-up, a tenant's status and the wallet report);
- ``mount_signing_routes`` (``http/signing.py``): the signer's two paths, for a payment key.

``http/listeners.py`` says which listener serves which group.

A tenant's id is its grant's id (D83): no route or body takes a separate grant id.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, Query, Response
from fastapi.responses import JSONResponse

from livepeer_builder.api import ClearinghouseApi
from livepeer_builder.contracts import (
    AccessKey,
    ActorAllocation,
    ActorContext,
    AllocationRead,
    AllocationStatus,
    AllotRequest,
    CredentialKind,
    IssuedKey,
    KeyedAllocation,
    KnownActor,
    OnboardedTenant,
    TenantGrant,
    TenantState,
    TopUpRequest,
    UsagePage,
    WalletReport,
)
from livepeer_builder.errors import AccessDenied
from livepeer_builder.http.auth import actor_dependency
from livepeer_builder.http.bodies import (
    AllotBody,
    OnboardBody,
    RotateBody,
    RotateKeyBody,
    StatusBody,
    TenantKeyBody,
    TenantStatusBody,
)
from livepeer_builder.http.params import GrantTenantId, PathId, QueryId
from livepeer_builder.http.replies import replay, replayed
from livepeer_builder.http.signing import mount_signing_routes

CLEARINGHOUSE = "/v1/clearinghouse"
TENANTS = CLEARINGHOUSE + "/tenants"
TENANT = TENANTS + "/{tenant_id}"  # the tenant a clearinghouse admin names
DISCOVERY = CLEARINGHOUSE + "/discovery"
WALLET = CLEARINGHOUSE + "/wallet"
# Below a prefix: CLEARINGHOUSE for the caller's own tenant, TENANT for one a clearinghouse admin names.
GRANT = "/grant"
USAGE = "/usage"
ACTORS = "/actors"
ALLOCATION = ACTORS + "/{actor_id}/allocation"
KEYS = "/keys"
KEY = KEYS + "/{key_id}"
TENANT_KINDS: frozenset[CredentialKind] = frozenset({"tenant_admin", "auditor"})


@dataclass(frozen=True)
class TenantScope:
    """Who calls one of a tenant's routes, and the tenant the route acts on."""

    actor: ActorContext
    tenant_id: str


Resolve = Callable[..., TenantScope]  # a FastAPI dependency


def mount_clearinghouse_routes(app: FastAPI, engine: ClearinghouseApi) -> None:
    """Add the clearinghouse half's routes to ``app``: every group, the signer's two paths included,
    which an enterprise's engine calls with a payment key (``http/signing.py``, #166)."""
    mount_tenant_routes(app, engine)
    mount_named_tenant_routes(app, engine)
    mount_runner_list_routes(app, engine)
    mount_clearinghouse_admin_routes(app, engine)
    mount_signing_routes(app, engine)


def mount_clearinghouse_admin_routes(app: FastAPI, engine: ClearinghouseApi) -> None:
    """Add what only a clearinghouse admin does: onboarding, the tenant list, a grant's top-up, a
    tenant's status and the wallet report. Onboarding, the list and the status need the tenant desk."""
    Actor = Annotated[ActorContext, Depends(actor_dependency(engine))]

    @app.post(TENANT + GRANT + "/top-up", status_code=204)
    async def top_up_grant(actor: Actor, tenant_id: GrantTenantId, body: TopUpRequest) -> Response:
        await engine.payments.top_up_grant(actor, tenant_id, body)
        return Response(status_code=204, headers=replayed())

    if engine.payments.wallet is not None:  # needs a provider that reports the escrow, and a signer to ask
        wallet = engine.payments.wallet

        @app.get(WALLET)
        async def wallet_report(actor: Actor) -> WalletReport:
            """The wallet's deposit and reserve as the payment provider has confirmed them, the total of
            unspent grants for each currency, and whether the signer answers. Nothing is converted
            between currencies and no margin is computed."""
            return await wallet.report(actor)

    if engine.payments.tenants is None:  # the tenant desk is off: none of its routes (decision 7)
        return
    desk = engine.payments.tenants

    @app.post(TENANTS, status_code=201)
    async def onboard_tenant(actor: Actor, body: OnboardBody, response: Response) -> OnboardedTenant:
        """Create the tenant's grant and its first tenant admin key, whose token is shown once."""
        onboarded = await desk.onboard(
            actor,
            body.name,
            body.amount,
            operation_ref=body.operation_ref,
            admin_actor_id=body.admin_actor_id,
        )
        return replay(response, onboarded)

    @app.get(TENANTS)
    async def list_tenants(actor: Actor) -> list[TenantGrant]:
        return await desk.list_tenants(actor)

    @app.post(TENANT + "/status")
    async def set_tenant_status(
        actor: Actor, tenant_id: GrantTenantId, body: TenantStatusBody, response: Response
    ) -> TenantState:
        """Pause or resume the tenant by pausing or resuming its grant."""
        state = await desk.set_status(actor, tenant_id, body.status, operation_ref=body.operation_ref)
        return replay(response, state)


def mount_runner_list_routes(app: FastAPI, engine: ClearinghouseApi) -> None:
    """Add the clearinghouse's runner list, which any credential of a tenant reads (#166)."""
    if engine.runner_list is None:  # needs a discovery source that holds the document it read
        return
    runner_list = engine.runner_list
    Actor = Annotated[ActorContext, Depends(actor_dependency(engine))]

    @app.get(
        DISCOVERY,
        responses={
            200: {"description": "The livepeer/node `/discovery` document the clearinghouse last read."},
            503: {"description": "The clearinghouse has read no runner list yet."},
        },
    )
    async def discovery(actor: Actor) -> Response:
        """The clearinghouse's runner list, in livepeer/node's discovery format: what an enterprise's
        engine reads as its ``LPB_DISCOVERY_URL``. The document is served as the clearinghouse read it.
        Any credential of a tenant may read it."""
        document = runner_list.document(actor)
        if document is None:  # never an empty list for an unknown one: a reader would empty its catalog
            unread = {"error": "the clearinghouse has read no runner list yet"}
            return JSONResponse(unread, status_code=503, headers={"Retry-After": "5"})
        return Response(content=document, media_type="application/json")


def mount_tenant_routes(app: FastAPI, engine: ClearinghouseApi) -> None:
    """Add a tenant's own routes, at paths that name no tenant: the tenant is the credential's. A tenant
    admin or auditor credential is taken, and any other is refused (403) before the service is asked."""
    Actor = Annotated[ActorContext, Depends(actor_dependency(engine))]

    def own_tenant(actor: Actor) -> TenantScope:
        if actor.credential_kind == "clearinghouse_admin":
            raise AccessDenied(
                "a clearinghouse admin has no tenant of its own: it names the tenant, at "
                "/v1/clearinghouse/tenants/{tenant_id}"
            )
        if actor.credential_kind not in TENANT_KINDS:
            raise AccessDenied(
                f"a {actor.credential_kind} credential acts for no tenant: a tenant's own routes take a "
                "tenant admin or auditor credential"
            )
        return TenantScope(actor, actor.tenant_id)

    _mount_tenant_scoped(app, engine, CLEARINGHOUSE, own_tenant, own_tenant)


def mount_named_tenant_routes(app: FastAPI, engine: ClearinghouseApi) -> None:
    """Add a tenant's routes at paths that name the tenant, under ``/v1/clearinghouse/tenants/{tenant_id}``:
    what a clearinghouse admin calls for the tenant it chooses."""
    Actor = Annotated[ActorContext, Depends(actor_dependency(engine))]

    def named_tenant(actor: Actor, tenant_id: PathId) -> TenantScope:
        return TenantScope(actor, tenant_id)

    # A tenant whose grant the route reads: its id goes into the payment provider's URL path, so it is one
    # path segment (#113, C10), a 422 otherwise.
    def named_grant_tenant(actor: Actor, tenant_id: GrantTenantId) -> TenantScope:
        return TenantScope(actor, tenant_id)

    _mount_tenant_scoped(app, engine, TENANT, named_tenant, named_grant_tenant)


def _mount_tenant_scoped(
    app: FastAPI, engine: ClearinghouseApi, prefix: str, tenant: Resolve, grant_tenant: Resolve
) -> None:
    """A tenant's routes under ``prefix``: its grant, its usage, its actors and their allocations, and its
    access keys. ``tenant`` resolves the tenant a route acts on, and ``grant_tenant`` the tenant of a
    route that reads its grant at the payment provider."""
    Scope = Annotated[TenantScope, Depends(tenant)]
    GrantScope = Annotated[TenantScope, Depends(grant_tenant)]

    if engine.payments.tenants is not None:  # reading a grant needs the tenant desk
        desk = engine.payments.tenants

        @app.get(prefix + GRANT)
        async def get_grant(scope: GrantScope) -> TenantGrant:
            """The grant's total, its unallocated balance and its status."""
            return await desk.grant(scope.actor, scope.tenant_id)

    if engine.payments.usage is not None:  # needs a source that reads one tenant's usage
        usage = engine.payments.usage

        @app.get(prefix + USAGE)
        async def tenant_usage(
            scope: GrantScope,
            cursor: QueryId = None,
            limit: Annotated[int, Query(ge=1, le=1000)] = 1000,
        ) -> UsagePage:
            """One page of the tenant's payment records, oldest first: the payment provider's usage rows
            for the tenant's grant, and none of another tenant's. ``cursor`` is the last page's
            ``next_cursor``, which is empty on the last page."""
            return await usage.page(scope.actor, scope.tenant_id, cursor, limit)

    @app.get(prefix + ACTORS)
    async def list_actors(
        scope: Scope,
        status: AllocationStatus | None = None,
        allotted: bool | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    ) -> list[KnownActor]:
        """The tenant's allotted actors and the people an identity provider signed in (D46, D48)."""
        return await engine.payments.list_actors(
            scope.actor, tenant_id=scope.tenant_id, status=status, allotted=allotted, limit=limit
        )

    @app.get(prefix + ALLOCATION)
    async def get_actor(scope: Scope, actor_id: PathId) -> AllocationRead:
        """The actor's allocation: its record, and as `allowance` what the payment provider reports for
        it now (allotted, spent, available, status). `allowance` is `null` when the provider could not
        be read."""
        return await engine.payments.get_allocation(scope.actor, scope.tenant_id, actor_id)

    @app.post(prefix + ALLOCATION, status_code=201)
    async def allot(scope: Scope, actor_id: PathId, body: AllotBody, response: Response) -> KeyedAllocation:
        """Allot the actor's allocation from the tenant's grant. The answer carries the actor's payment
        key, whose token is shown once."""
        request = AllotRequest(
            tenant_id=scope.tenant_id,
            actor_id=actor_id,
            amount=body.amount,
            label=body.label,
            operation_ref=body.operation_ref,
        )
        return replay(response, await engine.payments.allot(scope.actor, request))

    @app.post(prefix + ALLOCATION + "/top-up")
    async def top_up(
        scope: Scope, actor_id: PathId, body: TopUpRequest, response: Response
    ) -> ActorAllocation:
        return replay(response, await engine.payments.top_up(scope.actor, scope.tenant_id, actor_id, body))

    @app.post(prefix + ALLOCATION + "/status")
    async def set_status(scope: Scope, actor_id: PathId, body: StatusBody) -> ActorAllocation:
        return await engine.payments.set_status(scope.actor, scope.tenant_id, actor_id, body.status)

    @app.post(prefix + ALLOCATION + "/revoke")
    async def revoke(scope: Scope, actor_id: PathId) -> ActorAllocation:
        return await engine.payments.revoke(scope.actor, scope.tenant_id, actor_id)

    @app.post(prefix + ALLOCATION + "/rotate-payment-key")
    async def rotate_payment_key(
        scope: Scope, actor_id: PathId, body: RotateBody, response: Response
    ) -> KeyedAllocation:
        """Replace the actor's payment key, and the Batteries key behind it. The old one stops at once."""
        rotated = await engine.payments.rotate_payment_key(
            scope.actor, scope.tenant_id, actor_id, body.operation_ref
        )
        return replay(response, rotated)

    if engine.keys is None:  # API keys off: a tenant has no access keys to manage (decision 2)
        return
    keys = engine.keys

    @app.post(prefix + KEYS, status_code=201)
    async def issue_tenant_key(scope: Scope, body: TenantKeyBody, response: Response) -> IssuedKey:
        """Issue an auditor key for the tenant, or, as a clearinghouse admin, another tenant admin key."""
        issued = await keys.issue(
            scope.actor,
            kind=body.kind,
            actor_id=body.actor_id,
            tenant_id=scope.tenant_id,
            label=body.label,
            operation_ref=body.operation_ref,
        )
        return replay(response, issued)

    @app.get(prefix + KEYS)
    async def list_tenant_keys(scope: Scope) -> list[AccessKey]:
        return await keys.list_keys(scope.actor, scope.tenant_id)

    @app.get(prefix + KEY)
    async def get_tenant_key(scope: Scope, key_id: PathId) -> AccessKey:
        return await keys.get(scope.actor, key_id, scope.tenant_id)

    @app.post(prefix + KEY + "/rotate", status_code=201)
    async def rotate_tenant_key(
        scope: Scope, key_id: PathId, body: RotateKeyBody, response: Response
    ) -> IssuedKey:
        rotated = await keys.rotate(
            scope.actor, key_id, label=body.label, operation_ref=body.operation_ref, tenant_id=scope.tenant_id
        )
        return replay(response, rotated)

    @app.post(prefix + KEY + "/revoke", status_code=204)
    async def revoke_tenant_key(scope: Scope, key_id: PathId) -> Response:
        await keys.revoke(scope.actor, key_id, scope.tenant_id)
        return Response(status_code=204)
