"""Construction as data: supply, decorate, assemble (docs/design-docs/extending-the-engine.md)."""

import contextlib
import dataclasses
import logging
from collections.abc import AsyncIterator

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from livepeer_builder import BuilderEngine, EngineSettings, generate_secret_key
from livepeer_builder.adapters.secrets import EncryptedDbSecretStore
from livepeer_builder.adapters.static import StaticDiscoverySource
from livepeer_builder.contracts import ActorContext, AuthRequest, Runner
from livepeer_builder.http import create_app
from livepeer_builder.protocols import DiscoverySource
from livepeer_builder.runtime.assembly import (
    UNSET,
    ApiKeys,
    EngineParts,
    EngineSupply,
    assemble,
    default_parts,
)
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FakeAllocationManager, FixedClock, runner
from tests.unit.test_runtime import LOOPS, running_loops

MEMORY = "sqlite+aiosqlite:///:memory:"
UNOPENABLE = "sqlite+aiosqlite:////nonexistent-dir/never-opened.db"


def settings(**values: object) -> EngineSettings:
    return EngineSettings.model_validate({"database_url": MEMORY, "log_json": False, **values})


class HeaderAuthenticator:
    """An enterprise authenticator: trusts ``x-user`` (as a proxy in front of the engine would)."""

    async def authenticate(self, request: AuthRequest) -> ActorContext | None:
        user = request.header("x-user")
        return (
            ActorContext(actor_id=user, application_id="corp", permissions=frozenset({"jobs.read"}))
            if user
            else None
        )

    def challenge(self) -> str | None:
        return None


class Recording:
    """A decorator over any DiscoverySource that records how often it was asked."""

    def __init__(self, inner: DiscoverySource) -> None:
        self.inner = inner
        self.fetches = 0

    async def fetch(self) -> tuple[Runner, ...]:
        self.fetches += 1
        return await self.inner.fetch()


def test_engine_supply_names_exactly_the_engine_parts() -> None:
    parts = [field.name for field in dataclasses.fields(EngineParts)]
    supply = [field.name for field in dataclasses.fields(EngineSupply)]
    assert supply == parts, "EngineSupply and EngineParts must have the same fields (decision 6)"
    assert all(getattr(EngineSupply(), name) is UNSET for name in supply)


async def test_replacing_one_part_keeps_the_other_defaults() -> None:
    clock = FixedClock()
    async with contextlib.AsyncExitStack() as stack:
        defaults = await default_parts(settings(), stack)
        supplied = await default_parts(settings(), stack, EngineSupply(clock=clock))
    assert supplied.clock is clock
    assert type(supplied.policy) is type(defaults.policy)
    assert type(supplied.discovery_source) is type(defaults.discovery_source)
    assert supplied.api_keys is not None and supplied.grants_manager is None


async def test_a_supplied_store_is_used_and_the_configured_database_is_never_opened(
    store: SqlStore, clock: FixedClock
) -> None:
    key = generate_secret_key()
    async with contextlib.AsyncExitStack() as stack:
        parts = await default_parts(
            settings(database_url=UNOPENABLE, secret_key=key), stack, EngineSupply(store=store, clock=clock)
        )
    assert parts.store is store
    assert parts.api_keys is not None and parts.api_keys.store is store
    ref = await parts.secret_store.put("payment-key", "lpg_secret")
    assert (
        await EncryptedDbSecretStore(store, key, clock).get(ref) == "lpg_secret"
    )  # written to the supplied store


async def test_a_decorated_default_discovery_source_keeps_the_signer_header() -> None:
    seen: list[str | None] = []

    async def discover(request: web.Request) -> web.Response:
        seen.append(request.headers.get("Authorization"))
        return web.json_response([])

    app = web.Application()
    app.router.add_get("/discover-orchestrators", discover)
    async with TestServer(app) as signer:
        configured = settings(signer_url=str(signer.make_url("/")), shared_payment_key="lpg_shared")
        recording: list[Recording] = []

        def decorate(parts: EngineParts) -> EngineParts:
            recording.append(Recording(parts.discovery_source))
            return dataclasses.replace(parts, discovery_source=recording[0])

        engine = await BuilderEngine.create(configured, decorate=decorate)
        try:
            await engine.discovery.refresh()
        finally:
            await engine.stop()
    assert recording[0].fetches == 1
    assert seen == ["Bearer lpg_shared"]  # the configured default, wrapped, not re-created


async def test_own_authenticators_run_before_api_keys(store: SqlStore) -> None:
    engine = await BuilderEngine.create(
        settings(), supply=EngineSupply(store=store, authenticators=(HeaderAuthenticator(),))
    )
    try:
        assert engine.keys is not None
        issued = await engine.keys.bootstrap_admin()
        both = AuthRequest(
            method="GET",
            path="/v1/jobs",
            headers={"x-user": "carol", "authorization": f"Bearer {issued.token}"},
        )
        only_key = AuthRequest(
            method="GET", path="/v1/jobs", headers={"authorization": f"Bearer {issued.token}"}
        )
        assert (await engine.authenticator.authenticate(both)).actor_id == "carol"  # type: ignore[union-attr]
        assert (await engine.authenticator.authenticate(only_key)) is not None  # keys still work
    finally:
        await engine.stop()


@pytest.fixture
async def keys_off(store: SqlStore) -> AsyncIterator[tuple[BuilderEngine, httpx.AsyncClient]]:
    engine = await BuilderEngine.create(
        settings(), supply=EngineSupply(store=store, api_keys=None, authenticators=(HeaderAuthenticator(),))
    )
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        yield engine, client
    await engine.stop()


async def test_keys_off_removes_key_issuance_and_acceptance(
    keys_off: tuple[BuilderEngine, httpx.AsyncClient], store: SqlStore
) -> None:
    engine, client = keys_off
    assert engine.keys is None
    spec = (await client.get("/openapi.json")).json()
    paths = spec["paths"]
    assert not [path for path in paths if path.startswith("/v1/admin/keys")]
    # no lpk_ bearer is advertised: Swagger would offer a credential this deployment rejects
    assert "securitySchemes" not in spec.get("components", {})
    assert not [(p, m) for p, ops in paths.items() for m, op in ops.items() if "security" in op]
    # a key that exists in the store is not accepted, because nothing accepts keys
    issued = await (
        await BuilderEngine.create(settings(), supply=EngineSupply(store=store))
    ).keys.bootstrap_admin()  # type: ignore[union-attr]
    response = await client.get("/v1/jobs", headers={"authorization": f"Bearer {issued.token}"})
    assert response.status_code == 401
    assert (await client.get("/v1/jobs", headers={"x-user": "carol"})).status_code == 200


async def test_without_a_grants_manager_the_grant_desk_routes_do_not_exist(store: SqlStore) -> None:
    with_desk = await BuilderEngine.create(
        settings(), supply=EngineSupply(store=store, grants_manager=FakeAllocationManager())
    )
    without = await BuilderEngine.create(settings(), supply=EngineSupply(store=store))
    try:
        assert with_desk.payments.grants is not None and without.payments.grants is None
        on = create_app(with_desk, manage_lifecycle=False).openapi()["paths"]
        off = create_app(without, manage_lifecycle=False).openapi()["paths"]
        assert "/v1/admin/grants" in on and "/v1/admin/grants" not in off
        assert "/v1/admin/grant/fund" in off  # funding uses the engine token and stays
    finally:
        await with_desk.stop()
        await without.stop()


async def test_api_keys_carry_the_realm_into_the_challenge(store: SqlStore) -> None:
    engine = await BuilderEngine.create(
        settings(), supply=EngineSupply(store=store, api_keys=ApiKeys(store, "corp"))
    )
    try:
        assert engine.authenticator.challenge() == 'Bearer realm="corp"'
    finally:
        await engine.stop()


async def test_async_with_starts_and_stops_the_engine(store: SqlStore, clock: FixedClock) -> None:
    engine = await BuilderEngine.create(
        settings(),
        supply=EngineSupply(store=store, clock=clock, discovery_source=StaticDiscoverySource([runner()])),
    )
    async with engine as started:
        assert started is engine and engine.health().ready and running_loops() == LOOPS
    assert running_loops() == set()
    with pytest.raises(RuntimeError, match="engine is stopped"):
        await engine.start()


async def test_assemble_does_no_io(store: SqlStore, clock: FixedClock) -> None:
    async with contextlib.AsyncExitStack() as stack:
        parts = await default_parts(settings(), stack, EngineSupply(store=store, clock=clock))
    engine = assemble(settings(), parts)  # synchronous: nothing to await, nothing opened
    assert engine.store is store and engine.clock is clock


async def test_supplied_parts_are_borrowed_not_closed(store: SqlStore) -> None:
    engine = await BuilderEngine.create(settings(), supply=EngineSupply(store=store))
    await engine.stop()
    assert await store.list_jobs("default", "nobody") == []  # still open: the caller owns it


async def test_start_logs_the_authentication_and_grant_desk_modes(
    store: SqlStore, clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    engine = await BuilderEngine.create(
        settings(),
        supply=EngineSupply(
            store=store,
            clock=clock,
            api_keys=None,
            authenticators=(HeaderAuthenticator(),),
            discovery_source=StaticDiscoverySource(),
        ),
    )
    with caplog.at_level(logging.INFO, logger="livepeer_builder.runtime.engine"):
        async with engine:
            pass
    (started,) = [r for r in caplog.records if getattr(r, "event", "") == "engine.started"]
    assert (started.api_keys, started.own_authenticators, started.grant_desk) == ("off", 1, "off")  # type: ignore[attr-defined]
