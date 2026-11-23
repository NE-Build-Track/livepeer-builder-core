"""Which routes an engine serves, and where: by its role and by its listeners (D84). This module is the
one place that decides both, and which credentials each listener takes.

**The role** (``LPB_ROLE``, #169) chooses the half. Every group of routes belongs to one half of the
engine, the work half or the clearinghouse half, and ``ROUTE_GROUPS`` says which:

| ``LPB_ROLE`` | Serves |
| --- | --- |
| ``both``, the default | The groups of both halves: self-hosted payments |
| ``work`` | The work half's groups, and no clearinghouse route (404) |
| ``clearinghouse`` | The clearinghouse half's groups, and no job route or other work route (404) |

**The listeners** (#167) choose within what the role serves. A clearinghouse admin onboards tenants and
tops up grants, so a leaked clearinghouse admin key could spend the wallet. A deployment that sets
``LPB_HTTP_PRIVATE_PORT`` therefore serves two listeners, each with an app of its own over the one
engine:

- the **public listener** (``LPB_HTTP_HOST``, ``LPB_HTTP_PORT``) serves the groups of ``EVERY_LISTENER``
  and no route of ``PRIVATE_ONLY``: those are 404 there, as any path the engine does not know, before a
  credential is read. It refuses a clearinghouse admin credential with 403;
- the **private listener** (``LPB_HTTP_PRIVATE_HOST``, ``LPB_HTTP_PRIVATE_PORT``) serves every group,
  to a clearinghouse admin only: any other credential is 403 there.

Without ``LPB_HTTP_PRIVATE_PORT`` the engine has **one listener**, which serves every group of its role
to every kind of credential, as it did before #167. An engine in its ``clearinghouse`` role always has
two: its settings refuse to load without the private port, and ``mount_listener_routes`` refuses to
build it the app of one listener.

Every app this module builds keeps each allocation's key with the engine
(``deliver_allocation_keys=False``), on one listener or two. No listener serves a signer path: no engine
signs for another.

To serve a new group of routes, add a ``RouteGroup`` to ``ROUTE_GROUPS`` with its half, and
``private=True`` when only a clearinghouse admin calls it. A group that is not in the table is served by
no role and on no listener (tests/unit/test_listeners.py). ``mount_clearinghouse_routes`` still adds the
clearinghouse half's whole set to an app a caller builds for itself; no listener's app is built from it.

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
from livepeer_builder.http.clearinghouse import mount_clearinghouse_admin_routes, mount_tenant_routes
from livepeer_builder.http.work import mount_work_routes

ListenerName = Literal["only", "public", "private"]
Half = Literal["work", "clearinghouse"]
# A group's mount function: each takes the app and the engine, typed against its own half of the facade
# (api.py, tests/structure/test_two_halves.py). ``mount_tenant_routes`` also takes
# ``deliver_allocation_keys``, which ``mount_listener_routes`` passes.
Mount = Callable[..., None]
WORK_HALF: Half = "work"
CLEARINGHOUSE_HALF: Half = "clearinghouse"


@dataclass(frozen=True)
class RouteGroup:
    """One group of routes: the function that mounts it, the half of the engine it belongs to, and
    whether it is served on the private listener alone."""

    mount: Mount
    half: Half
    private: bool = False


ROUTE_GROUPS: tuple[RouteGroup, ...] = (
    # the work half: jobs, the catalog, runners, costs, the allowance read and the engine's own keys
    RouteGroup(mount_work_routes, WORK_HALF),
    # what a tenant's own admin or auditor calls, and the runner list a clearinghouse publishes
    RouteGroup(mount_tenant_routes, CLEARINGHOUSE_HALF),
    # what only a clearinghouse admin does: never served on the public listener
    RouteGroup(mount_clearinghouse_admin_routes, CLEARINGHOUSE_HALF, private=True),
)
# The halves each role serves (D84): the guide's table, "The engine's two halves".
ROLE_HALVES: dict[Role, frozenset[Half]] = {
    "both": frozenset({WORK_HALF, CLEARINGHOUSE_HALF}),
    "work": frozenset({WORK_HALF}),
    "clearinghouse": frozenset({CLEARINGHOUSE_HALF}),
}
# Read from the table: served on every listener, and never served on the public one.
EVERY_LISTENER: tuple[Mount, ...] = tuple(group.mount for group in ROUTE_GROUPS if not group.private)
PRIVATE_ONLY: tuple[Mount, ...] = tuple(group.mount for group in ROUTE_GROUPS if group.private)


PRIVATE_TAG = "private listener"
"""The OpenAPI tag on every route of ``PRIVATE_ONLY``, so a published schema says where it is served."""
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
        "these routes are 404 on an engine in its `work` role, which uses a payment clearinghouse "
        "and is not one.",
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
        if group.half not in halves or (group.private and not listener.serves_private_routes):
            continue
        before = len(app.routes)
        if group.mount is mount_tenant_routes:  # the one group that allots and rotates
            mount_tenant_routes(app, engine, deliver_allocation_keys=False)
        else:
            group.mount(app, engine)
        # Tagged here, from the table, and not on each route: a route added to a group is tagged too.
        tags = [HALF_TAGS[group.half], *([PRIVATE_TAG] if group.private else [])]
        for route in app.routes[before:]:
            if isinstance(route, APIRoute):
                route.tags = [*route.tags, *tags]
                tagged.update(tags)
    app.openapi_tags = [*(app.openapi_tags or []), *(tag for tag in _TAGS if tag["name"] in tagged)]
