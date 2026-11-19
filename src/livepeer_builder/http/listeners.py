"""Which routes an engine serves, and where: by its role and by its listeners (D84). This module is the
one place that decides both, and which credentials each listener takes.

**The role** (``LPB_ROLE``, #169) chooses the half. Every group of routes belongs to one half of the
engine, the work half or the clearinghouse half, and ``ROUTE_GROUPS`` says which:

| ``LPB_ROLE`` | Serves |
| --- | --- |
| ``both``, the default | The groups of both halves: self-hosted payments |
| ``work`` | The work half's groups, and no clearinghouse route or signer path (404) |
| ``clearinghouse`` | The clearinghouse half's groups, and no job route or other work route (404) |

**The listeners** (#167) choose within what the role serves. A clearinghouse admin onboards tenants and
tops up grants, so a leaked clearinghouse admin key could spend the wallet. A deployment that sets
``LPB_HTTP_PRIVATE_PORT`` therefore serves two listeners, each with an app of its own over the one
engine:

- the **public listener** (``LPB_HTTP_HOST``, ``LPB_HTTP_PORT``) serves the groups of ``EVERY_LISTENER``
  and of ``PUBLIC_ONLY``, and no route of ``PRIVATE_ONLY``: those are 404 there, as any path the engine
  does not know, before a credential is read. It refuses a clearinghouse admin credential with 403;
- the **private listener** (``LPB_HTTP_PRIVATE_HOST``, ``LPB_HTTP_PRIVATE_PORT``) serves the groups of
  ``EVERY_LISTENER`` and of ``PRIVATE_ONLY``, and no route of ``PUBLIC_ONLY``, to a clearinghouse admin
  only: any other credential is 403 there.

``PUBLIC_ONLY`` is a tenant's own routes, which name no tenant and take a tenant admin or auditor
credential; ``PRIVATE_ONLY`` holds the same routes at paths that name the tenant, which take a
clearinghouse admin only (D87 point 3, as amended on 19 November 2026). Each listener serves the form
the credentials it takes can call.

Without ``LPB_HTTP_PRIVATE_PORT`` the engine has **one listener**, which serves every group of its role
to every kind of credential the group takes, as it did before #167. An engine in its ``clearinghouse``
role always has two: its settings refuse to load without the private port, and ``mount_listener_routes``
refuses to build it the app of one listener.

The rule about kinds is a rule about access credentials. The signer's two paths (``http/signing.py``,
#166) take a payment key, which is no access credential and has no kind: they are served on every
listener of a role that has them, a live payment key signs on each, and anything else is 401 there, a
clearinghouse admin credential included.

To serve a new group of routes, add a ``RouteGroup`` to ``ROUTE_GROUPS`` with its half, and
``served_on="private"`` when only a clearinghouse admin calls it, or ``served_on="public"`` when only a
tenant's own credentials do. A group that is not in the table is served by no role and on no listener
(tests/unit/test_listeners.py). ``mount_clearinghouse_routes`` still adds the clearinghouse half's whole
set to an app a caller builds for itself; no listener's app is built from it.

The credential rule is applied where every route authenticates (``http/auth.py``), from the listener
this module puts on ``app.state``, so a route added later is covered without a check of its own.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from fastapi import FastAPI
from fastapi.routing import APIRoute

from livepeer_builder.api import EngineApi
from livepeer_builder.contracts import ActorContext, Role
from livepeer_builder.errors import AccessDenied
from livepeer_builder.http.clearinghouse import (
    mount_clearinghouse_admin_routes,
    mount_named_tenant_routes,
    mount_runner_list_routes,
    mount_tenant_routes,
)
from livepeer_builder.http.signing import mount_signing_routes
from livepeer_builder.http.work import mount_work_routes

ListenerName = Literal["only", "public", "private"]
ServedOn = Literal["every", "public", "private"]  # of two listeners, which serves a group
Half = Literal["work", "clearinghouse"]
Mount = Callable[[FastAPI, EngineApi], None]  # each is typed against its own half of the facade (api.py)
WORK_HALF: Half = "work"
CLEARINGHOUSE_HALF: Half = "clearinghouse"


@dataclass(frozen=True)
class RouteGroup:
    """One group of routes: the function that mounts it, the half of the engine it belongs to, and which
    of two listeners serves it. One listener serves every group."""

    mount: Mount
    half: Half
    served_on: ServedOn = "every"

    def served_by(self, listener: ListenerName) -> bool:
        return listener == "only" or self.served_on in ("every", listener)


ROUTE_GROUPS: tuple[RouteGroup, ...] = (
    # the work half: jobs, the catalog, runners, costs, the allowance read and the engine's own keys
    RouteGroup(mount_work_routes, WORK_HALF),
    # a tenant's own routes, which name no tenant: a tenant admin's or an auditor's, never on the private one
    RouteGroup(mount_tenant_routes, CLEARINGHOUSE_HALF, served_on="public"),
    # the same routes with the tenant in the path: a clearinghouse admin's, never on the public one
    RouteGroup(mount_named_tenant_routes, CLEARINGHOUSE_HALF, served_on="private"),
    # the runner list a clearinghouse publishes, which any credential of a tenant reads (#166)
    RouteGroup(mount_runner_list_routes, CLEARINGHOUSE_HALF),
    # the signer's two paths, which an enterprise's engine calls with a payment key (#166)
    RouteGroup(mount_signing_routes, CLEARINGHOUSE_HALF),
    # what only a clearinghouse admin does: never served on the public listener
    RouteGroup(mount_clearinghouse_admin_routes, CLEARINGHOUSE_HALF, served_on="private"),
)
# The halves each role serves (D84): the guide's table, "The engine's two halves".
ROLE_HALVES: dict[Role, frozenset[Half]] = {
    "both": frozenset({WORK_HALF, CLEARINGHOUSE_HALF}),
    "work": frozenset({WORK_HALF}),
    "clearinghouse": frozenset({CLEARINGHOUSE_HALF}),
}
# Read from the table: served on every listener, never on the private one, and never on the public one.
EVERY_LISTENER: tuple[Mount, ...] = tuple(g.mount for g in ROUTE_GROUPS if g.served_on == "every")
PUBLIC_ONLY: tuple[Mount, ...] = tuple(g.mount for g in ROUTE_GROUPS if g.served_on == "public")
PRIVATE_ONLY: tuple[Mount, ...] = tuple(g.mount for g in ROUTE_GROUPS if g.served_on == "private")

PRIVATE_TAG = "private listener"
"""The OpenAPI tag on every route of ``PRIVATE_ONLY``, so a published schema says where it is served."""
PUBLIC_TAG = "public listener"
"""The OpenAPI tag on every route of ``PUBLIC_ONLY``."""
SIDE_TAGS: dict[ServedOn, str] = {"public": PUBLIC_TAG, "private": PRIVATE_TAG}
HALF_TAGS: dict[Half, str] = {WORK_HALF: "work half", CLEARINGHOUSE_HALF: "clearinghouse half"}
"""The OpenAPI tag on every route of a half, so a published schema says which roles serve it."""
_TAGS = [
    {
        "name": HALF_TAGS[WORK_HALF],
        "description": "Served by an engine in its `both` or its `work` role (`LPB_ROLE`): these routes "
        "are 404 on an engine in its `clearinghouse` role, which never runs jobs.",
    },
    {
        "name": HALF_TAGS[CLEARINGHOUSE_HALF],
        "description": "Served by an engine in its `both` or its `clearinghouse` role (`LPB_ROLE`): "
        "these routes are 404 on an engine in its `work` role, which pays through a payment "
        "clearinghouse and is not one.",
    },
    {
        "name": PUBLIC_TAG,
        "description": "Served on the public listener only, when the deployment has a private one "
        "(`LPB_HTTP_PRIVATE_PORT`): these routes are 404 on the private listener. A deployment with one "
        "listener serves them there. They are a tenant's own routes: they name no tenant, act on the "
        "tenant of the credential, and take a tenant admin or auditor credential only.",
    },
    {
        "name": PRIVATE_TAG,
        "description": "Served on the private listener only, when the deployment has one "
        "(`LPB_HTTP_PRIVATE_PORT`): these routes are 404 on the public listener. A deployment with one "
        "listener serves them there. An engine in its `clearinghouse` role always has a private "
        "listener.",
    },
]


@dataclass(frozen=True)
class Listener:
    """One listener's rule about the credentials it takes."""

    name: ListenerName

    def check(self, actor: ActorContext) -> None:
        """Refuse a credential this listener does not take: ``AccessDenied``, which is 403."""
        admin = actor.credential_kind == "clearinghouse_admin"
        if self.name == "public" and admin:
            raise AccessDenied("a clearinghouse admin credential is not accepted on the public listener")
        if self.name == "private" and not admin:
            raise AccessDenied("the private listener accepts a clearinghouse admin credential only")


LISTENERS: dict[ListenerName, Listener] = {
    "only": Listener("only"),
    "public": Listener("public"),
    "private": Listener("private"),
}


def mount_listener_routes(app: FastAPI, engine: EngineApi, name: ListenerName = "only") -> None:
    """Add to ``app`` the groups of routes the engine's role serves on the listener ``name``, and put the
    listener on ``app.state.listener``, where authentication reads the credentials it takes."""
    halves = ROLE_HALVES[engine.role]
    if WORK_HALF not in halves and name == "only":  # the clearinghouse role
        raise ValueError(
            "an engine in its clearinghouse role serves two listeners, never one: what only a "
            "clearinghouse admin does is on the private listener alone. Run serve(), or build each app "
            'with create_app(engine, listener="public") and create_app(engine, listener="private")'
        )
    listener = LISTENERS[name]
    app.state.listener = listener
    tagged: set[str] = set()
    for group in ROUTE_GROUPS:
        if group.half not in halves or not group.served_by(name):
            continue
        before = len(app.routes)
        group.mount(app, engine)
        # Tagged here, from the table, and not on each route: a route added to a group is tagged too.
        side = SIDE_TAGS.get(group.served_on)
        tags = [HALF_TAGS[group.half], *([side] if side else [])]
        for route in app.routes[before:]:
            if isinstance(route, APIRoute):
                route.tags = [*route.tags, *tags]
                tagged.update(tags)
    app.openapi_tags = [*(app.openapi_tags or []), *(tag for tag in _TAGS if tag["name"] in tagged)]
