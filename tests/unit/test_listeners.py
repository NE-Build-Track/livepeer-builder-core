"""A public and a private listener (D84, #167): what each one serves, and which credentials each takes.

A clearinghouse admin onboards tenants and tops up grants, so a leaked clearinghouse admin key could
spend the wallet. A deployment that sets ``LPB_HTTP_PRIVATE_PORT`` serves two listeners:

- the **public listener** serves no route of the private group (404, as for any path the engine does
  not know, with a credential or without one), and refuses a clearinghouse admin credential with 403 on
  every route that takes a credential;
- the **private listener** serves every route, to a clearinghouse admin only.

With one listener, which is the default, nothing changes: every route, and every kind of credential.

The signer's two paths (#166) are served on every listener and take a payment key, which is not an
access key of any kind. So the listener's rule does not reach them: an access key is 401 there on every
listener, a clearinghouse admin's included, and a live payment key signs on both.

The routes are read from each app, so a route added later is swept without editing this file: a new
route of the private group must be 404 on the public listener, and a new route of any group must refuse
a clearinghouse admin there. A route that takes no credential is named in ``NO_CREDENTIAL``, and one that takes a
payment key in ``PAYMENT_KEY``, each with its own expected answers, so adding one is a decision somebody
made.
"""

import importlib
import pkgutil
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.contracts import ActorContext, AllotRequest, AuthRequest, Money
from livepeer_builder.http import actor_dependency, create_app, listeners
from livepeer_builder.http.app import shared_app
from livepeer_builder.http.clearinghouse import mount_clearinghouse_routes
from livepeer_builder.http.work import mount_work_routes
from livepeer_builder.testing import FakeAllocationManager, FakeSigningTransport
from tests.conftest import EngineFactory
from tests.unit.test_admin_route_sweep import SAMPLE_BODIES, everything_stored, filled

TENANTS = "/v1/clearinghouse/tenants"
TENANT = TENANTS + "/{tenant_id}"
# The guide's private table (docs/design-docs/m4-guides/iss-31-payment-configurations.md, "Routes").
PRIVATE = {
    ("POST", TENANTS),
    ("GET", TENANTS),
    ("POST", TENANT + "/grant/top-up"),
    ("POST", TENANT + "/status"),
    ("GET", "/v1/clearinghouse/wallet"),
}
# A valid body for each body model, so an answer is the listener's and never a 422: the admin routes'
# samples, and the work routes'.
BODIES: dict[str, dict[str, Any]] = {
    **SAMPLE_BODIES,
    "JobRequest": {"app": "example/echo"},
    "UsageBody": {"quantities": {"tokens": 1}},
    "SessionRequest": {"app": "example/echo"},
}


def body_for(route: APIRoute) -> dict[str, Any] | None:
    params = route.dependant.body_params
    if not params:
        return None
    name = params[0].field_info.annotation.__name__  # type: ignore[union-attr]
    assert name in BODIES, f"add a sample {name} to BODIES for {route.path}"
    return BODIES[name]


def served(app: FastAPI) -> dict[tuple[str, str], APIRoute]:
    return {
        (method, route.path): route
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }


def client_of(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine")


def bearer(token: str | None) -> dict[str, str]:
    assert token is not None
    return {"Authorization": f"Bearer {token}"}


@dataclass
class Clearinghouse:
    """One engine with two listeners' apps, real ids for every path, and a key of every kind."""

    engine: BuilderEngine
    public: FastAPI
    private: FastAPI
    params: dict[str, str]
    clearinghouse_admin: dict[str, str]
    tenant_admin: dict[str, str]
    auditor: dict[str, str]
    actor: dict[str, str]
    signer: FakeSigningTransport  # the signer behind the engine: it records every call handed to it
    payment_key: dict[str, str]  # alice's payment key, as the SDK sends a signer its credential
    batteries_key: str  # the Batteries key of alice's allocation, which never leaves the engine

    @property
    def access_keys(self) -> dict[str, dict[str, str]]:
        """An access key of every kind the engine issues."""
        return {
            "clearinghouse_admin": self.clearinghouse_admin,
            "tenant_admin": self.tenant_admin,
            "auditor": self.auditor,
            "actor": self.actor,
        }

    def listeners(self) -> dict[str, FastAPI]:
        """The app of each listener, and of a deployment with one."""
        only = create_app(self.engine, manage_lifecycle=False)
        return {"only": only, "public": self.public, "private": self.private}


@pytest.fixture
async def clearinghouse(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> Clearinghouse:
    """The tenant ``default`` has a grant, an actor with an allocation and a key of each kind, so nothing
    below is refused for being missing. The engine's transport forwards signer calls, as the packaged
    one with a signer does, so the signer's two paths are served."""
    signer = FakeSigningTransport()
    engine = await make_engine(supply=EngineSupply(transport=signer))
    assert engine.keys is not None
    request = AllotRequest(
        operation_ref="allot:default:alice",
        tenant_id="default",
        actor_id="alice",
        amount=Money.model_validate({"amount": "5", "currency": "USD"}),
    )
    allotted = await engine.payments.allot(admin, request)
    assert allotted.payment_key is not None
    issue = engine.keys.issue
    alice = await issue(admin, kind="actor", actor_id="alice", operation_ref="listeners-alice")
    tenant_admin = await issue(admin, kind="tenant_admin", actor_id="desk", operation_ref="listeners-desk")
    auditor = await issue(admin, kind="auditor", actor_id="audit", operation_ref="listeners-audit")
    return Clearinghouse(
        engine=engine,
        public=create_app(engine, manage_lifecycle=False, listener="public"),
        private=create_app(engine, manage_lifecycle=False, listener="private"),
        params={
            "tenant_id": "default",
            "actor_id": "alice",
            "key_id": alice.key.key_id,
            "job_id": str(uuid4()),
        },
        clearinghouse_admin=bearer((await engine.keys.bootstrap_admin()).token),
        tenant_admin=bearer(tenant_admin.token),
        auditor=bearer(auditor.token),
        actor=bearer(alice.token),
        signer=signer,
        payment_key=bearer(allotted.payment_key.token),
        batteries_key=manager.keys[allotted.api_key_id][1],
    )


async def test_the_private_routes_are_404_on_the_public_listener_with_a_credential_or_without(
    clearinghouse: Clearinghouse, manager: FakeAllocationManager
) -> None:
    """The ticket's "Done when". The private group is whatever the private listener serves and the
    public one does not, read from the two apps: it holds the guide's routes, and each answers as a path
    the engine does not know, whoever asks."""
    house = clearinghouse
    private_only = served(house.private).keys() - served(house.public).keys()
    assert private_only >= PRIVATE
    stored_before, calls_before = await everything_stored(house.engine), list(manager.calls)
    wrong: list[str] = []
    async with client_of(house.public) as client:
        unknown = await client.get("/v1/clearinghouse/no-such-route", headers=house.clearinghouse_admin)
        assert (unknown.status_code, unknown.json()) == (404, {"detail": "Not Found"})
        for method, path in sorted(private_only):
            route = served(house.private)[method, path]
            callers = (
                ("no credential", {}),
                ("a clearinghouse admin", house.clearinghouse_admin),
                ("a tenant admin", house.tenant_admin),
            )
            for who, headers in callers:
                answer = await client.request(
                    method, filled(path, house.params), json=body_for(route), headers=headers
                )
                if (answer.status_code, answer.content) != (404, unknown.content):
                    wrong.append(f"{method} {path} as {who}: {answer.status_code} {answer.text[:80]}")
    assert not wrong, "the public listener answered a private route:\n" + "\n".join(wrong)
    assert manager.calls == calls_before  # the payment provider was never called
    assert await everything_stored(house.engine) == stored_before


# Routes that take no credential, on any listener: health, and the public catalog (#5).
NO_CREDENTIAL = {("GET", "/livez"), ("GET", "/readyz"), ("GET", "/v1/offerings")}
# Routes that take a payment key and nothing else, on any listener: the signer's two paths (#166). An
# access key of any kind is no credential there, so their answer to one is 401, never the listener's 403.
PAYMENT_KEY = {("POST", "/sign-orchestrator-info"), ("POST", "/generate-live-payment")}


async def refused(
    app: FastAPI, params: dict[str, str], headers: dict[str, str], reason: str
) -> tuple[list[str], int]:
    """Call every route of ``app`` that takes an access key with ``headers``. What did not answer 403
    with ``reason``, and how many routes were called. The routes that take a payment key have their own
    expected answer, below."""
    wrong: list[str] = []
    routes = {key: route for key, route in served(app).items() if key not in NO_CREDENTIAL | PAYMENT_KEY}
    async with client_of(app) as client:
        for (method, path), route in sorted(routes.items()):
            answer = await client.request(method, filled(path, params), json=body_for(route), headers=headers)
            if answer.status_code != 403 or reason not in answer.text:
                wrong.append(f"{method} {path}: {answer.status_code} {answer.text[:120]}")
    return wrong, len(routes)


async def test_a_clearinghouse_admin_key_is_403_on_every_route_of_the_public_listener(
    clearinghouse: Clearinghouse, manager: FakeAllocationManager
) -> None:
    """The ticket's "a clearinghouse admin key does nothing there". Every route of the public listener
    that takes an access key refuses it, work routes included, before the provider or the store hears of
    the request. Every route of the public listener is in one of three sets: these, the ones that take
    no credential, and the ones that take a payment key, where the key is 401."""
    house = clearinghouse
    stored_before, calls_before = await everything_stored(house.engine), list(manager.calls)
    wrong, swept = await refused(
        house.public, house.params, house.clearinghouse_admin, "not accepted on the public listener"
    )
    assert swept >= 30, f"the sweep found too few routes to be meaningful: {swept}"
    assert swept == len(served(house.public)) - len(NO_CREDENTIAL) - len(PAYMENT_KEY)
    assert not wrong, "the public listener took a clearinghouse admin key:\n" + "\n".join(wrong)
    async with client_of(house.public) as client:
        for method, path in sorted(PAYMENT_KEY):
            answer = await client.request(method, path, content=b"{}", headers=house.clearinghouse_admin)
            assert answer.status_code == 401, f"{method} {path}: {answer.status_code}"
    assert not house.signer.forwarded  # and the signer was handed nothing
    assert manager.calls == calls_before  # the payment provider was never called
    assert await everything_stored(house.engine) == stored_before


async def test_the_routes_that_take_no_credential_are_the_ones_named_here(
    clearinghouse: Clearinghouse,
) -> None:
    """A route missing from ``NO_CREDENTIAL`` is swept above, and one named there answers without a
    credential. So a route that takes no credential cannot be added to a listener unnoticed."""
    for app in (clearinghouse.public, clearinghouse.private):
        assert served(app).keys() >= NO_CREDENTIAL | PAYMENT_KEY
        async with client_of(app) as client:
            for method, path in sorted(NO_CREDENTIAL):
                assert (await client.request(method, path)).status_code == 200, (method, path)
            for (method, path), route in sorted(served(app).items()):
                if (method, path) in NO_CREDENTIAL:
                    continue
                url = filled(path, clearinghouse.params)
                answer = await client.request(method, url, json=body_for(route))
                assert answer.status_code == 401, (
                    f"{method} {path} takes no credential: add it to NO_CREDENTIAL"
                )


async def test_the_private_listener_serves_every_route_to_a_clearinghouse_admin(
    clearinghouse: Clearinghouse,
) -> None:
    """Every public route is also served on the private listener, for any tenant, and the private group
    with them: one listener for everything a clearinghouse admin does."""
    house = clearinghouse
    assert served(house.private).keys() == served(create_app(house.engine, manage_lifecycle=False)).keys()
    assert served(house.private).keys() > served(house.public).keys()
    admin = house.clearinghouse_admin
    body = {"name": "acme", "amount": {"amount": "5", "currency": "USD"}, "operation_ref": "onboard-acme"}
    async with client_of(house.private) as client:
        onboarded = await client.post(TENANTS, json=body, headers=admin)
        assert onboarded.status_code == 201, onboarded.text
        tenant = onboarded.json()["tenant_id"]
        assert tenant in {row["tenant_id"] for row in (await client.get(TENANTS, headers=admin)).json()}
        top_up = {"amount": {"amount": "1", "currency": "USD"}, "operation_ref": "top-up-acme"}
        assert (
            await client.post(f"{TENANTS}/{tenant}/grant/top-up", json=top_up, headers=admin)
        ).status_code == 204
        paused = {"status": "paused", "operation_ref": "pause-acme"}
        assert (
            await client.post(f"{TENANTS}/{tenant}/status", json=paused, headers=admin)
        ).status_code == 200
        # a public route, for any tenant
        assert (await client.get(f"{TENANTS}/default/actors", headers=admin)).status_code == 200
        assert (await client.get("/v1/keys", headers=admin)).status_code == 200


@pytest.mark.parametrize("kind", ["tenant_admin", "auditor", "actor"])
async def test_the_private_listener_refuses_every_credential_but_a_clearinghouse_admins(
    clearinghouse: Clearinghouse, manager: FakeAllocationManager, kind: str
) -> None:
    """The private listener is for a clearinghouse admin only (the guide's "Routes"). A tenant's own
    admin, its auditor and an actor are refused on every route there, their own tenant's included: they
    use the public listener."""
    house = clearinghouse
    stored_before, calls_before = await everything_stored(house.engine), list(manager.calls)
    wrong, swept = await refused(
        house.private, house.params, getattr(house, kind), "accepts a clearinghouse admin credential only"
    )
    assert swept >= 34, f"the sweep found too few routes to be meaningful: {swept}"
    assert not wrong, f"the private listener took a {kind} key:\n" + "\n".join(wrong)
    assert manager.calls == calls_before
    assert await everything_stored(house.engine) == stored_before
    async with client_of(house.private) as client:  # no credential is still a challenge, not a refusal
        assert (await client.get(f"{TENANTS}/default/actors")).status_code == 401


async def test_one_listener_serves_every_route_to_every_kind_of_credential(
    clearinghouse: Clearinghouse,
) -> None:
    """The default, and what self-hosted payments runs: no private listener, so nothing is refused that
    was accepted before #167. The clearinghouse admin key does everything it did."""
    house = clearinghouse
    only = create_app(house.engine, manage_lifecycle=False)
    assert served(only).keys() >= PRIVATE
    async with client_of(only) as client:
        assert (await client.get(TENANTS, headers=house.clearinghouse_admin)).status_code == 200
        assert (await client.get("/v1/keys", headers=house.clearinghouse_admin)).status_code == 200
        assert (await client.get(f"{TENANTS}/default/grant", headers=house.tenant_admin)).status_code == 200
        assert (await client.get(f"{TENANTS}/default/actors", headers=house.auditor)).status_code == 200
        assert (await client.get("/v1/jobs", headers=house.actor)).status_code == 200


async def test_a_route_an_extension_adds_takes_the_credentials_its_listener_takes(
    clearinghouse: Clearinghouse,
) -> None:
    """``extend`` routes authenticate with ``actor_dependency``, as the built-in routes do, so the
    listener's rule reaches them with no check of their own."""
    house = clearinghouse
    for app in (house.public, house.private):
        Actor = Annotated[ActorContext, Depends(actor_dependency(house.engine))]

        @app.get("/v1/corp/whoami")
        async def whoami(actor: Actor) -> dict[str, str]:
            return {"kind": actor.credential_kind}

    async with client_of(house.public) as client:
        assert (await client.get("/v1/corp/whoami", headers=house.clearinghouse_admin)).status_code == 403
        assert (await client.get("/v1/corp/whoami", headers=house.actor)).json() == {"kind": "actor"}
    async with client_of(house.private) as client:
        assert (await client.get("/v1/corp/whoami", headers=house.actor)).status_code == 403
        answer = await client.get("/v1/corp/whoami", headers=house.clearinghouse_admin)
        assert answer.json() == {"kind": "clearinghouse_admin"}


# Mount functions that add no group of their own: they call the groups, or the table.
COMPOSERS = {"mount_clearinghouse_routes", "mount_listener_routes"}


def test_every_group_of_routes_is_given_to_a_listener() -> None:
    """A group of routes is one ``mount_..._routes`` function in ``livepeer_builder.http``. Each is
    named in ``http/listeners.py``, so a new group is served where somebody decided, never by default
    on the public listener and never nowhere."""
    import livepeer_builder.http as package

    groups = {
        name
        for module in pkgutil.iter_modules(package.__path__)
        for name, value in vars(importlib.import_module(f"{package.__name__}.{module.name}")).items()
        if name.startswith("mount_") and callable(value) and value.__module__.endswith(module.name)
    }
    given = {mount.__name__ for mount in (*listeners.EVERY_LISTENER, *listeners.PRIVATE_ONLY)}
    assert not set(listeners.EVERY_LISTENER) & set(listeners.PRIVATE_ONLY)
    assert groups - COMPOSERS == given, (
        f"not given to a listener: {sorted(groups - COMPOSERS - given)}. Add the group's mount function "
        "to EVERY_LISTENER or to PRIVATE_ONLY in src/livepeer_builder/http/listeners.py: PRIVATE_ONLY "
        "when only a clearinghouse admin calls it, so it is never served on the public listener."
    )


async def test_the_listeners_serve_what_the_two_sets_of_routes_mount(clearinghouse: Clearinghouse) -> None:
    """``mount_work_routes`` and ``mount_clearinghouse_routes`` are the two halves' sets. A group one of
    them gains is on a listener, or this fails: the table in ``http/listeners.py`` names every group."""
    sets = shared_app(clearinghouse.engine, manage_lifecycle=False)
    mount_work_routes(sets, clearinghouse.engine)
    mount_clearinghouse_routes(sets, clearinghouse.engine)
    assert served(clearinghouse.private).keys() == served(sets).keys()


async def test_the_published_schema_says_which_routes_are_private(clearinghouse: Clearinghouse) -> None:
    """Each listener publishes the schema of what it serves. The public one names no private route. The
    private one, and a deployment with one listener (which is what docs/generated/openapi.json is
    generated from), tag every route of the private group, and only those."""
    house = clearinghouse
    private_only = served(house.private).keys() - served(house.public).keys()

    def operations(app: FastAPI, tag: str | None = None) -> set[tuple[str, str]]:
        return {
            (method.upper(), path)
            for path, methods in app.openapi()["paths"].items()
            for method, operation in methods.items()
            if tag is None or tag in operation.get("tags", [])
        }

    for app in (house.private, create_app(house.engine, manage_lifecycle=False)):
        assert operations(app, listeners.PRIVATE_TAG) == private_only
        assert listeners.PRIVATE_TAG in {tag["name"] for tag in app.openapi()["tags"]}
    assert not operations(house.public, listeners.PRIVATE_TAG)
    assert not operations(house.public) & private_only
    async with client_of(house.public) as client:  # as served: /openapi.json and the docs page read it
        assert "/v1/clearinghouse/tenants" not in (await client.get("/openapi.json")).json()["paths"]


class KindFromHeader:
    """A deployment's own authenticator, as an identity provider's token would be read: the kind of
    the credential is whatever it verified. No engine key is involved."""

    async def authenticate(self, request: AuthRequest) -> ActorContext | None:
        kind = request.header("x-kind")
        if kind is None:
            return None
        return ActorContext.model_validate({"actor_id": "someone", "credential_kind": kind})

    def challenge(self) -> str | None:
        return None


async def test_the_rule_is_by_the_kind_of_credential_whatever_verified_it(make_engine: EngineFactory) -> None:
    """A token an identity provider issued with the admin scope is a clearinghouse admin credential too
    (``LPB_JWT_ADMIN_SCOPE``), and so is one a deployment's own authenticator makes. The listener asks
    the kind, not where the credential came from."""
    engine = await make_engine(supply=EngineSupply(api_keys=None, authenticators=(KindFromHeader(),)))
    actors = f"{TENANTS}/default/actors"
    expected = {
        "public": {"clearinghouse_admin": 403, "tenant_admin": 200, "auditor": 200},
        "private": {"clearinghouse_admin": 200, "tenant_admin": 403, "auditor": 403},
        "only": {"clearinghouse_admin": 200, "tenant_admin": 200, "auditor": 200},
    }
    for listener, answers in expected.items():
        app = create_app(engine, manage_lifecycle=False, listener=listener)  # type: ignore[arg-type]
        async with client_of(app) as client:
            got = {kind: (await client.get(actors, headers={"x-kind": kind})).status_code for kind in answers}
            assert got == answers, listener
            assert (await client.get(actors)).status_code == 401


# -- the signer's two paths (#166): on every listener, for a payment key ---------------------------------


async def test_the_signers_two_paths_are_served_on_every_listener(clearinghouse: Clearinghouse) -> None:
    """An enterprise's engine reaches a clearinghouse at its public listener, so the two paths are there.
    Every public route is also on the private one, and a deployment with one listener has them."""
    for name, app in clearinghouse.listeners().items():
        assert served(app).keys() >= PAYMENT_KEY, name


async def test_only_a_payment_key_is_answered_on_the_signers_paths_whatever_the_listener(
    clearinghouse: Clearinghouse, manager: FakeAllocationManager
) -> None:
    """No credential, an access key of every kind and a Batteries key are each 401 on both paths of
    every listener, with nothing handed to the signer. A clearinghouse admin key is 401 here on the
    private listener too, and a tenant's key is 401 here on the public one too: neither is a payment
    key, and the listener's rule about kinds is never asked."""
    house = clearinghouse
    callers = {"no credential": {}, "a Batteries key": bearer(house.batteries_key), **house.access_keys}
    stored_before, calls_before = await everything_stored(house.engine), list(manager.calls)
    wrong: list[str] = []
    for name, app in house.listeners().items():
        async with client_of(app) as client:
            for method, path in sorted(PAYMENT_KEY):
                for who, headers in callers.items():
                    answer = await client.request(method, path, content=b"{}", headers=headers)
                    if answer.status_code != 401 or "payment key" not in answer.text:
                        wrong.append(f"{name}: {method} {path} as {who}: {answer.status_code} {answer.text}")
    assert not wrong, "a signer path answered something that is no payment key:\n" + "\n".join(wrong)
    assert not house.signer.forwarded
    assert manager.calls == calls_before
    assert await everything_stored(house.engine) == stored_before


async def test_a_live_payment_key_signs_on_the_public_and_on_the_private_listener(
    clearinghouse: Clearinghouse,
) -> None:
    """The signer's paths are served on every listener, by the owner's decision, and what they take is a
    payment key on each: the private listener's "clearinghouse admin only" is a rule about access keys.
    Each call reaches the signer once, with the actor's Batteries key in the payment key's place."""
    house = clearinghouse
    for name, app in house.listeners().items():
        house.signer.forwarded.clear()
        async with client_of(app) as client:
            for method, path in sorted(PAYMENT_KEY):
                answer = await client.request(method, path, content=b"{}", headers=house.payment_key)
                assert answer.status_code == 200, (
                    f"{name}: {method} {path}: {answer.status_code} {answer.text}"
                )
        assert [(call.call, call.credential) for call in house.signer.forwarded] == [
            ("generate-live-payment", house.batteries_key),
            ("sign-orchestrator-info", house.batteries_key),
        ], name


async def test_each_listeners_own_schema_names_the_signers_paths_and_what_they_take(
    clearinghouse: Clearinghouse,
) -> None:
    """The public listener publishes the two paths, with the payment key as their credential. They are
    in no private group, so they carry no ``private listener`` tag on any listener."""
    for name, app in clearinghouse.listeners().items():
        async with client_of(app) as client:
            schema = (await client.get("/openapi.json")).json()
        for method, path in sorted(PAYMENT_KEY):
            operation = schema["paths"][path][method.lower()]
            assert operation["security"] == [{"PaymentKey": []}], (name, path)
            assert listeners.PRIVATE_TAG not in operation.get("tags", []), (name, path)
        assert schema["components"]["securitySchemes"]["PaymentKey"]["bearerFormat"] == "lpp", name
