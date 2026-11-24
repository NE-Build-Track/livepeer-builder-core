"""The engine's listeners: which one serves which group of routes, and which credentials each takes
(D84, #167). This module is the one place that decides both.

A clearinghouse admin onboards tenants and tops up grants, so a leaked clearinghouse admin key could
spend the wallet. A deployment that sets ``LPB_HTTP_PRIVATE_PORT`` therefore serves two listeners, each
with an app of its own over the one engine:

- the **public listener** (``LPB_HTTP_HOST``, ``LPB_HTTP_PORT``) serves the groups in
  ``EVERY_LISTENER`` and no route of ``PRIVATE_ONLY``: those are 404 there, as any path the engine does
  not know, before a credential is read. It refuses a clearinghouse admin credential with 403;
- the **private listener** (``LPB_HTTP_PRIVATE_HOST``, ``LPB_HTTP_PRIVATE_PORT``) serves every group,
  to a clearinghouse admin only: any other credential is 403 there.

Without ``LPB_HTTP_PRIVATE_PORT`` the engine has **one listener**, which serves every group to every
kind of credential, as it did before #167.

Every app this module builds keeps each allocation's key with the engine
(``deliver_allocation_keys=False``): the engine behind a listener runs jobs, on one listener or two. A
standalone clearinghouse, which hands each key to the caller and stores none, is the ``clearinghouse``
role's (#169).

To serve a new group of routes, add its mount function to ``EVERY_LISTENER`` or to ``PRIVATE_ONLY``. A
group in neither is served nowhere (tests/unit/test_listeners.py). ``mount_clearinghouse_routes`` still
adds the clearinghouse half's whole set to an app a caller builds for itself; no listener's app is built
from it.

The credential rule is applied where every route authenticates (``http/auth.py``), from the listener
this module puts on ``app.state``, so a route added later is covered without a check of its own.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from fastapi import FastAPI
from fastapi.routing import APIRoute

from livepeer_builder.api import EngineApi
from livepeer_builder.contracts import ActorContext
from livepeer_builder.errors import AccessDenied
from livepeer_builder.http.clearinghouse import mount_clearinghouse_admin_routes, mount_tenant_routes
from livepeer_builder.http.work import mount_work_routes

ListenerName = Literal["only", "public", "private"]
# A group's mount function: each takes the app and the engine, and ``mount_tenant_routes`` also takes
# ``deliver_allocation_keys``, which ``mount_listener_routes`` passes.
Mount = Callable[..., None]

# Served on every listener: the work half's routes, and what a tenant's own admin or auditor calls.
EVERY_LISTENER: tuple[Mount, ...] = (mount_work_routes, mount_tenant_routes)
# Never served on the public listener: what only a clearinghouse admin does.
PRIVATE_ONLY: tuple[Mount, ...] = (mount_clearinghouse_admin_routes,)

PRIVATE_TAG = "private listener"
"""The OpenAPI tag on every route of ``PRIVATE_ONLY``, so a published schema says where it is served."""
_TAGS = [
    {
        "name": PRIVATE_TAG,
        "description": "Served on the private listener only, when the deployment has one "
        "(`LPB_HTTP_PRIVATE_PORT`): these routes are 404 on the public listener. A deployment with one "
        "listener serves them there.",
    }
]


@dataclass(frozen=True)
class Listener:
    """One listener's rules: whether it serves the private group, and the credentials it takes."""

    name: ListenerName
    serves_private_routes: bool

    def check(self, actor: ActorContext) -> None:
        """Refuse a credential this listener does not take: ``AccessDenied``, which is 403."""
        admin = actor.credential_kind == "clearinghouse_admin"
        if self.name == "public" and admin:
            raise AccessDenied("a clearinghouse admin credential is not accepted on the public listener")
        if self.name == "private" and not admin:
            raise AccessDenied("the private listener accepts a clearinghouse admin credential only")


LISTENERS: dict[ListenerName, Listener] = {
    "only": Listener("only", serves_private_routes=True),
    "public": Listener("public", serves_private_routes=False),
    "private": Listener("private", serves_private_routes=True),
}


def mount_listener_routes(app: FastAPI, engine: EngineApi, name: ListenerName = "only") -> None:
    """Add to ``app`` the groups of routes the listener ``name`` serves, and put the listener on
    ``app.state.listener``, where authentication reads the credentials it takes."""
    listener = LISTENERS[name]
    app.state.listener = listener
    for mount in EVERY_LISTENER:
        if mount is mount_tenant_routes:
            # False on every listener: this engine runs jobs, so it stores each allocation's key for
            # them and returns none. Handing keys to the caller is the clearinghouse role's (#169).
            mount_tenant_routes(app, engine, deliver_allocation_keys=False)
        else:
            mount(app, engine)
    if not listener.serves_private_routes:
        return
    public = len(app.routes)
    for mount in PRIVATE_ONLY:
        mount(app, engine)
    # Tagged here, from the table, and not on each route: a route added to a private group is tagged too.
    app.openapi_tags = [*(app.openapi_tags or []), *_TAGS]
    for route in app.routes[public:]:
        if isinstance(route, APIRoute):
            route.tags = [*route.tags, PRIVATE_TAG]
