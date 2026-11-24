"""#169: what an engine in each role serves, wires and runs (D84).

- ``both``, the default: both halves, as before. Self-hosted payments.
- ``work``: the work half alone. It mounts no clearinghouse route, and it is nobody's clearinghouse: it
  reports no wallet and publishes no runner list.
- ``clearinghouse``: the clearinghouse half alone, on a public and a private listener. It mounts no job
  route and no other work route, and it starts none of the work half's machinery: no interrupted job
  is resolved, and no cost sync and no retention purge run. It still reads discovery, for the runner
  list it publishes. An embedding caller that asks it to run a job is refused before anything is
  written.

The routes each role must *not* serve are read from the other half's own set (``mount_work_routes``,
``mount_clearinghouse_routes``) on an engine in its ``both`` role, so a route added to a half later is
swept here without editing this file. The exact set each role serves is in
tests/unit/test_route_sets.py, and a whole job paid from one role through the other in
tests/unit/test_work_through_a_clearinghouse.py.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from livepeer_builder import BuilderEngine, EngineSettings, EngineSupply, generate_secret_key
from livepeer_builder.contracts import ActorContext, AllotRequest, JobRequest, Money
from livepeer_builder.errors import InvalidRequest
from livepeer_builder.http import build_app, create_app
from livepeer_builder.http.app import shared_app
from livepeer_builder.http.clearinghouse import mount_clearinghouse_routes
from livepeer_builder.http.work import mount_work_routes
from livepeer_builder.testing import FakeAllocationManager, ScriptedStreamTransport
from tests.conftest import EngineFactory
from tests.unit.role_engines import CLEARINGHOUSE, PRIVATE_PORT, ROLES, Role, in_role, served
from tests.unit.test_admin_route_sweep import everything_stored, filled
from tests.unit.test_listeners import body_for, refused
from tests.unit.test_tenant_from_credential import NAMED as TENANT
from tests.unit.test_tenant_from_credential import SHORT_PATHS

LOOPS = {"lpb-discovery", "lpb-cost-sync", "lpb-retention"}
JOBS = {
    ("POST", "/v1/jobs"),
    ("GET", "/v1/jobs"),
    ("GET", "/v1/jobs/{job_id}"),
    ("GET", "/v1/jobs/{job_id}/result"),
    ("POST", "/v1/jobs/{job_id}/usage"),
    ("GET", "/v1/jobs/{job_id}/cost"),
}
SIGNER_PATHS = ("/sign-orchestrator-info", "/generate-live-payment")


def client_of(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine")


def bearer(token: str | None) -> dict[str, str]:
    assert token is not None
    return {"Authorization": f"Bearer {token}"}


def usd(amount: str) -> Money:
    return Money.model_validate({"amount": amount, "currency": "USD"})


async def keys_of(engine: BuilderEngine, admin: ActorContext) -> dict[str, dict[str, str]]:
    """An access key of every kind the engine issues, as Authorization headers."""
    assert engine.keys is not None
    issue = engine.keys.issue
    return {
        "clearinghouse_admin": bearer((await engine.keys.bootstrap_admin()).token),
        "tenant_admin": bearer(
            (await issue(admin, kind="tenant_admin", actor_id="desk", operation_ref="roles-desk")).token
        ),
        "auditor": bearer(
            (await issue(admin, kind="auditor", actor_id="audit", operation_ref="roles-audit")).token
        ),
        "actor": bearer(
            (await issue(admin, kind="actor", actor_id="alice", operation_ref="roles-alice")).token
        ),
    }


async def unknown_path(client: httpx.AsyncClient, headers: dict[str, str]) -> bytes:
    """What this app answers a path it does not know: the body a route of the other half must match."""
    answer = await client.get("/v1/no-such-route", headers=headers)
    assert (answer.status_code, answer.json()) == (404, {"detail": "Not Found"})
    return answer.content


async def not_served(
    app: FastAPI,
    routes: dict[tuple[str, str], Any],
    callers: dict[str, dict[str, str]],
    params: dict[str, str],
) -> list[str]:
    """Call every one of ``routes`` on ``app`` as each caller. What did not answer as an unknown path."""
    wrong: list[str] = []
    async with client_of(app) as client:
        for who, headers in callers.items():
            unknown = await unknown_path(client, headers)
            for (method, path), route in sorted(routes.items()):
                answer = await client.request(
                    method, filled(path, params), json=body_for(route), headers=headers
                )
                if (answer.status_code, answer.content) != (404, unknown):
                    wrong.append(f"{method} {path} as {who}: {answer.status_code} {answer.text[:80]}")
    return wrong


# -- the routes a role does not serve --------------------------------------------------------------------


async def test_a_clearinghouse_answers_404_on_every_job_route_and_every_other_work_route(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    """The ticket's "an engine in its clearinghouse role mounts no job routes". Every route of the work
    half's own set is an unknown path on both of its listeners, whoever asks, and nothing is stored."""
    whole = await in_role(make_engine, "both")
    work = shared_app(whole, manage_lifecycle=False)
    mount_work_routes(work, whole)
    work_routes = {key: route for key, route in served(work).items() if not key[1].endswith("z")}
    assert work_routes.keys() >= JOBS | {("GET", "/v1/offerings"), ("GET", "/v1/keys")}
    assert len(work_routes) >= 20, f"the sweep found too few work routes to be meaningful: {len(work_routes)}"

    house = await in_role(make_engine, "clearinghouse")
    keys = await keys_of(house, admin)
    params = {"job_id": str(uuid4()), "key_id": "key_1", "tenant_id": "default", "actor_id": "alice"}
    stored_before, calls_before = await everything_stored(house), list(manager.calls)
    public = create_app(house, manage_lifecycle=False, listener="public")
    private = create_app(house, manage_lifecycle=False, listener="private")
    on_public = {"no credential": {}, "a tenant admin": keys["tenant_admin"], "an actor": keys["actor"]}
    on_private = {"no credential": {}, "a clearinghouse admin": keys["clearinghouse_admin"]}
    wrong = await not_served(public, work_routes, on_public, params)
    wrong += await not_served(private, work_routes, on_private, params)
    assert not wrong, "an engine in its clearinghouse role answered a work route:\n" + "\n".join(wrong)
    assert not served(public).keys() & work_routes.keys()
    assert not served(private).keys() & work_routes.keys()
    assert manager.calls == calls_before
    assert await everything_stored(house) == stored_before
    assert await house.store.jobs_in_state("running") == []


async def test_a_work_engine_answers_404_on_every_clearinghouse_route(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    """The ticket's "one in its work role mounts no clearinghouse routes". The tenant's routes, what
    only a clearinghouse admin does, the wallet and the runner list are each an unknown path there, to
    its own admin, to an actor and to nobody."""
    whole = await in_role(make_engine, "both")
    clearinghouse = shared_app(whole, manage_lifecycle=False)
    mount_clearinghouse_routes(clearinghouse, whole, deliver_allocation_keys=False)
    routes = {key: route for key, route in served(clearinghouse).items() if not key[1].endswith("z")}
    assert routes.keys() >= {
        ("POST", "/v1/clearinghouse/tenants"),
        ("GET", "/v1/clearinghouse/wallet"),
        ("GET", "/v1/clearinghouse/discovery"),
        ("POST", "/v1/clearinghouse/tenants/{tenant_id}/actors/{actor_id}/allocation"),
    }
    assert len(routes) >= 20, f"the sweep found too few clearinghouse routes to be meaningful: {len(routes)}"

    enterprise = await in_role(make_engine, "work")
    keys = await keys_of(enterprise, admin)
    params = {"tenant_id": "default", "actor_id": "alice", "key_id": "key_1"}
    stored_before, calls_before = await everything_stored(enterprise), list(manager.calls)
    app = create_app(enterprise, manage_lifecycle=False)
    callers = {"no credential": {}, "its own admin": keys["clearinghouse_admin"], "an actor": keys["actor"]}
    wrong = await not_served(app, routes, callers, params)
    assert not wrong, "an engine in its work role answered a clearinghouse route:\n" + "\n".join(wrong)
    assert not served(app).keys() & routes.keys()
    assert not [path for _, path in served(app) if path.startswith("/v1/clearinghouse")]
    assert manager.calls == calls_before
    assert await everything_stored(enterprise) == stored_before


@pytest.mark.parametrize("role", ROLES)
async def test_no_role_serves_a_signer_path(make_engine: EngineFactory, role: Role) -> None:
    """No engine signs for another (D85 point 1): an enterprise's engine signs at the signer itself."""
    engine = await in_role(make_engine, role)
    listeners = ("public", "private") if role == "clearinghouse" else ("only", "public", "private")
    for listener in listeners:
        app = create_app(engine, manage_lifecycle=False, listener=listener)  # type: ignore[arg-type]
        assert not [path for _, path in served(app) if path in SIGNER_PATHS]
        async with client_of(app) as client:
            for path in SIGNER_PATHS:
                assert (await client.post(path, content=b"{}")).status_code == 404, (listener, path)


async def test_a_work_engine_still_serves_its_work_routes(
    make_engine: EngineFactory, admin: ActorContext
) -> None:
    enterprise = await in_role(make_engine, "work")
    keys = await keys_of(enterprise, admin)
    async with client_of(create_app(enterprise, manage_lifecycle=False)) as client:
        assert (await client.get("/v1/offerings")).status_code == 200  # public, as before (#5)
        assert (await client.get("/v1/jobs", headers=keys["actor"])).status_code == 200
        assert (await client.get("/v1/keys", headers=keys["clearinghouse_admin"])).status_code == 200
        assert (await client.post("/v1/costs/sync", headers=keys["clearinghouse_admin"])).status_code == 200
        assert (await client.get("/v1/jobs")).status_code == 401


# -- the listeners of a clearinghouse --------------------------------------------------------------------


async def test_a_clearinghouse_keeps_its_listeners_rules(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    """The role chooses the half, and the listener still chooses within it (#167): what only a
    clearinghouse admin does, and a tenant's routes at the paths that name the tenant, are on the
    private listener alone; a tenant's own routes, which name none, are on the public one alone (D87
    point 3, as amended on 19 November 2026). A clearinghouse admin key does nothing on the public
    listener, and any other key does nothing on the private one."""
    house = await in_role(make_engine, "clearinghouse")
    request = AllotRequest(
        operation_ref="allot:default:alice", tenant_id="default", actor_id="alice", amount=usd("5")
    )
    await house.payments.allot(admin, request)
    keys = await keys_of(house, admin)
    public = create_app(house, manage_lifecycle=False, listener="public")
    private = create_app(house, manage_lifecycle=False, listener="private")
    private_only = served(private).keys() - served(public).keys()
    public_only = served(public).keys() - served(private).keys()
    named = {(method, path.replace("/v1/clearinghouse", TENANT, 1)) for method, path in SHORT_PATHS}
    assert private_only == named | {
        ("POST", "/v1/clearinghouse/tenants"),
        ("GET", "/v1/clearinghouse/tenants"),
        ("POST", "/v1/clearinghouse/tenants/{tenant_id}/grant/top-up"),
        ("POST", "/v1/clearinghouse/tenants/{tenant_id}/status"),
        ("GET", "/v1/clearinghouse/wallet"),
    }
    assert public_only == SHORT_PATHS
    params = {"tenant_id": "default", "actor_id": "alice", "key_id": "key_1"}
    stored_before, calls_before = await everything_stored(house), list(manager.calls)
    wrong, swept = await refused(
        public, params, keys["clearinghouse_admin"], "not accepted on the public listener"
    )
    assert swept >= 14 and not wrong, wrong
    for kind in ("tenant_admin", "auditor", "actor"):
        wrong, swept = await refused(
            private, params, keys[kind], "accepts a clearinghouse admin credential only"
        )
        assert swept >= 19 and not wrong, (kind, wrong)
    assert manager.calls == calls_before
    assert await everything_stored(house) == stored_before
    async with client_of(private) as client:
        assert (
            await client.get("/v1/clearinghouse/tenants", headers=keys["clearinghouse_admin"])
        ).status_code == 200
    async with client_of(public) as client:
        actors = "/v1/clearinghouse/actors"  # a tenant's own route: the tenant is the key's
        assert (await client.get(actors, headers=keys["tenant_admin"])).status_code == 200


async def test_a_clearinghouse_has_no_app_for_one_listener(make_engine: EngineFactory) -> None:
    """One listener would serve onboarding and top-ups beside the tenants' routes, to every kind of
    credential. The refusal says which two apps to build."""
    house = await in_role(make_engine, "clearinghouse")
    with pytest.raises(ValueError, match="clearinghouse") as error:
        create_app(house, manage_lifecycle=False)
    assert 'listener="public"' in str(error.value) and 'listener="private"' in str(error.value)
    settings = EngineSettings.model_validate(
        {"discovery_url": "http://127.0.0.1:9/discovery", "database_url": "sqlite+aiosqlite:///:memory:"}
        | CLEARINGHOUSE
    )
    with pytest.raises(ValueError, match="LPB_HTTP_PRIVATE_PORT"):
        await build_app(settings)  # as for any deployment with two listeners (#167): serve() runs both


# -- what each role wires -------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
async def test_the_engine_says_its_role(make_engine: EngineFactory, role: Role) -> None:
    engine = await in_role(make_engine, role)
    assert engine.role == role and engine.health().role == role
    listener = "public" if role == "clearinghouse" else "only"
    async with client_of(create_app(engine, manage_lifecycle=False, listener=listener)) as client:  # type: ignore[arg-type]
        ready = await client.get("/readyz")
        assert (ready.status_code, ready.json()["role"]) == (200, role)
        assert (await client.get("/livez")).json() == {"status": "ok"}


async def test_the_both_role_wires_both_halves_as_before(
    make_engine: EngineFactory, admin: ActorContext
) -> None:
    engine = await in_role(make_engine, "both")
    payments = engine.payments
    assert payments.wallet is not None and payments.tenants is not None
    assert engine.runner_list is not None and engine.costs._worker is not None
    request = AllotRequest(operation_ref="a-1", tenant_id="default", actor_id="alice", amount=usd("5"))
    assert (await payments.allot(admin, request)).credential_custody == "secret_store"


async def test_the_work_role_is_nobodys_clearinghouse(
    make_engine: EngineFactory, admin: ActorContext
) -> None:
    """It reports no wallet and publishes no runner list, whatever its parts could do: here its
    provider could report the escrow and its transport could ask the signer. It keeps its own actors'
    keys, as any engine that runs jobs."""
    engine = await in_role(make_engine, "work")
    payments = engine.payments
    assert payments.wallet is None
    assert engine.runner_list is None
    assert engine.costs._worker is not None  # its jobs' costs are synced, through the usage port
    request = AllotRequest(operation_ref="a-1", tenant_id="default", actor_id="alice", amount=usd("5"))
    assert (await payments.allot(admin, request)).credential_custody == "secret_store"


async def test_the_clearinghouse_role_wires_the_clearinghouse_half_and_no_cost_sync(
    make_engine: EngineFactory, admin: ActorContext
) -> None:
    engine = await in_role(make_engine, "clearinghouse")
    payments = engine.payments
    assert payments.wallet is not None and payments.tenants is not None
    assert payments.usage is not None and engine.runner_list is not None
    assert engine.costs._worker is None  # cost sync attributes usage to jobs: it has none


# -- what each role runs --------------------------------------------------------------------------------


def running_loops() -> set[str]:
    return {task.get_name() for task in asyncio.all_tasks() if task.get_name() in LOOPS}


@pytest.fixture
async def no_loop_left() -> AsyncIterator[None]:
    yield
    assert running_loops() == set()


@pytest.mark.parametrize(
    ("role", "loops", "recovers"),
    [
        ("both", LOOPS, True),
        ("work", LOOPS, True),
        ("clearinghouse", {"lpb-discovery"}, False),
    ],
)
async def test_a_role_starts_the_machinery_of_its_half(
    make_engine: EngineFactory,
    monkeypatch: pytest.MonkeyPatch,
    no_loop_left: None,
    role: Role,
    loops: set[str],
    recovers: bool,
) -> None:
    """Resolving interrupted jobs, cost sync and the retention purge are the work half's. Discovery is
    read in every role: a clearinghouse publishes its runner list from it."""
    engine = await in_role(make_engine, role, retention_jobs_days=30, retention_interval_s=3600)
    recovered: list[bool] = []

    async def recover() -> int:
        recovered.append(True)
        return 0

    monkeypatch.setattr(engine.jobs, "recover_interrupted", recover)
    await engine.start()
    try:
        # Each loop's first pass ends before the engine stops: a query cancelled half way would leave
        # the test's one in-memory connection unusable.
        await asyncio.sleep(0.05)
        assert running_loops() == loops
        assert bool(recovered) is recovers
        assert engine.health().ready
    finally:
        await engine.stop()


async def test_a_clearinghouse_runs_no_job_for_an_embedding_caller_either(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext
) -> None:
    """No route reaches ``jobs.run`` in this role. A caller that holds the engine is refused too, before
    a job is recorded, a runner is called or a payment is asked for: a clearinghouse never runs jobs
    (D82)."""
    house = await in_role(make_engine, "clearinghouse")
    request = AllotRequest(operation_ref="a-1", tenant_id="default", actor_id="alice", amount=usd("5"))
    await house.payments.allot(admin, request)
    transport = house.jobs._transport
    assert isinstance(transport, ScriptedStreamTransport)

    with pytest.raises(InvalidRequest, match="clearinghouse role"):
        await house.jobs.run(alice, JobRequest(app="example/echo"))
    assert house.jobs.streaming is not None
    with pytest.raises(InvalidRequest, match="clearinghouse role"):
        await house.jobs.streaming.stream(alice, JobRequest(app="example/echo"))

    assert transport.calls == []
    assert await house.jobs.list_jobs(alice) == []
    assert house.health().running_jobs == 0


# -- what a clearinghouse needs of its parts ---------------------------------------------------------------


def clearinghouse_settings(**values: Any) -> EngineSettings:
    base = {
        "database_url": "sqlite+aiosqlite:///:memory:",
        "log_json": False,
        "discovery_url": "http://127.0.0.1:9/discovery",
        "secret_key": generate_secret_key(),
        "role": "clearinghouse",
        "http_private_port": PRIVATE_PORT,
        "signer_url": "http://signer-not-to-be-shown:8937",
        "batteries_url": "http://batteries-not-to-be-shown:8081",
        "batteries_token": "bt_secret-not-to-be-shown",
        "batteries_grants_token": "gt_secret-not-to-be-shown",
    }
    return EngineSettings.model_validate({**base, **values})


async def test_a_clearinghouse_is_built_from_its_settings_alone() -> None:
    engine = await BuilderEngine.create(clearinghouse_settings())
    try:
        assert engine.role == "clearinghouse"
        assert engine.payments.tenants is not None
        assert engine.payments.wallet is not None and engine.runner_list is not None
    finally:
        await engine.stop()


@pytest.mark.parametrize(
    ("unset", "named"),
    [
        ("batteries_url", "LPB_BATTERIES_URL"),
        ("batteries_token", "LPB_BATTERIES_TOKEN"),
        ("batteries_grants_token", "LPB_BATTERIES_GRANTS_TOKEN"),
    ],
)
async def test_a_clearinghouse_without_batteries_does_not_start(unset: str, named: str) -> None:
    """A clearinghouse allots from its tenants' grants and onboards them. One that cannot would serve
    routes that do nothing, so it is refused when it is built, by the setting that is missing. No value
    is echoed."""
    with pytest.raises(ValueError, match="LPB_ROLE=clearinghouse") as error:
        await BuilderEngine.create(clearinghouse_settings(**{unset: None}))
    message = str(error.value)
    assert named in message
    assert "not-to-be-shown" not in message


async def test_a_clearinghouse_is_judged_on_the_parts_it_will_run_with(make_engine: EngineFactory) -> None:
    """Supplied parts count, as they do for the secret store (#112): a supplied provider is Batteries'
    stand-in. A part that is off is refused."""
    assert (await in_role(make_engine, "clearinghouse")).payments.tenants is not None
    with pytest.raises(ValueError, match="LPB_BATTERIES_TOKEN"):
        await make_engine(supply=EngineSupply(allocation_manager=None), **CLEARINGHOUSE)
    with pytest.raises(ValueError, match="LPB_BATTERIES_GRANTS_TOKEN"):
        await make_engine(supply=EngineSupply(grants_manager=None), **CLEARINGHOUSE)
