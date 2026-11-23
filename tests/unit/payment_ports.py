"""The two sets of payment adapters, for the shared port suite (#168, D84).

``open_ports("local")`` is an engine whose payment ports reach the payment provider itself: self-hosted
payments, with the shipped fakes standing in for Batteries. ``open_ports("remote")`` is an enterprise's
engine whose ports are the remote adapters, over an in-process transport into a standalone
clearinghouse's public listener (``standalone_clearinghouse``): the app ``create_app`` builds for an
engine in its ``clearinghouse`` role, which hands each allocation's key to the caller and stores none.
The clearinghouse is an engine of its own, with a store and a secret store of its own and the same fakes
behind it. So on the remote side both ends of the wire are this repository's code, and the provider that
holds the money is the same kind of object on both sides: a test reads it to see what happened.

Each engine is in the role its side has (#169): ``local`` is the ``both`` role, the enterprise's engine
is in its ``work`` role, and the clearinghouse in its ``clearinghouse`` role, which serves no work route
and runs no job. ``open_ports("remote", house=...)`` puts another engine where the clearinghouse should
be: a test of what an enterprise's engine meets there.
"""

import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

import httpx
from fastapi import FastAPI
from multidict import CIMultiDict

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.adapters.clearinghouse import ClearinghousePorts
from livepeer_builder.adapters.clearinghouse_wire import ClearinghouseWire
from livepeer_builder.contracts import ActorContext, Money
from livepeer_builder.http import create_app
from livepeer_builder.protocols import AllocationManager, AllowanceReader, CredentialResolver, UsageSource
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FakeAllocationManager, FakeUsageSource, MemorySecretStore, make_engine

Side = Literal["local", "remote"]
AppOf = Callable[[BuilderEngine], FastAPI]
SIDES: tuple[Side, ...] = ("local", "remote")
CLEARINGHOUSE_URL = "http://clearinghouse.test"
GRANT_TOTAL = Decimal(100)
OPERATOR = ActorContext(actor_id="operator", credential_kind="clearinghouse_admin")
# A clearinghouse has a private listener (#169). Nothing binds it here: the apps are called in process.
CLEARINGHOUSE_ROLE: dict[str, object] = {"role": "clearinghouse", "http_private_port": 18081}
# An engine with self-hosted payments and two listeners: no clearinghouse for anybody's engine.
BOTH_ROLE: dict[str, object] = {"role": "both", "http_private_port": 18081}
PERMISSIONS = frozenset({"discover", "jobs.run", "jobs.read", "costs.read"})


@dataclass
class Ports:
    """One engine and its payment ports, with the provider that holds the money behind them."""

    side: Side
    engine: BuilderEngine  # the engine that runs the jobs: self-hosted, or an enterprise's
    provider: FakeAllocationManager  # Batteries' stand-in, wherever it sits
    provider_usage: FakeUsageSource
    tenant_id: str  # the tenant the engine's actors are in: its grant's id
    other_tenant: str  # a second tenant with a grant of its own at the same provider
    manager: AllocationManager
    allowances: AllowanceReader
    usage: UsageSource
    credentials: CredentialResolver
    clearinghouse: BuilderEngine | None = None  # the remote side's other end
    secrets: MemorySecretStore | None = None  # the engine's own secret store, when a test supplied it
    clearinghouse_secrets: MemorySecretStore | None = None  # the clearinghouse's own, which stays empty
    requests: list[httpx.Request] = field(default_factory=list)  # what crossed the wire (remote)
    token: str = ""  # the tenant admin key (remote)
    lost: list[str] = field(default_factory=list)  # see ``lose_the_answer_to``
    answers: list[tuple[int, bytes]] = field(default_factory=list)  # what the clearinghouse answered (remote)
    # every request that arrived at the clearinghouse's socket, as (path, headers), when it has one
    seen: list[tuple[str, CIMultiDict[str]]] = field(default_factory=list)

    @property
    def admin(self) -> ActorContext:
        """Who allots at this engine: the admin of the engine's own deployment."""
        return OPERATOR

    def tenant_admin(self, tenant_id: str | None = None) -> ActorContext:
        return ActorContext(
            actor_id="desk", tenant_id=tenant_id or self.tenant_id, credential_kind="tenant_admin"
        )

    def actor(self, actor_id: str) -> ActorContext:
        return ActorContext(actor_id=actor_id, tenant_id=self.tenant_id, permissions=PERMISSIONS)

    def lose_the_answer_to(self, path_ending: str) -> None:
        """The next request whose path ends so is handled by the clearinghouse, and its answer never
        arrives: the connection drops (remote)."""
        self.lost.append(path_ending)

    def sent(self, method: str = "POST") -> list[str]:
        """The paths of the requests that crossed the wire with ``method``, in order (remote)."""
        return [request.url.path for request in self.requests if request.method == method]

    async def pays_for(self, token: str) -> str | None:  # NOSONAR - async, as the tests await it
        """The allocation the payment key ``token`` pays from now, as the signer's authorization webhook
        would find it at the provider, on either side: the key is the allocation's own Batteries key.
        ``None`` when it pays for nothing (never minted, rotated away, or its allocation revoked)."""
        for allocation_id, secret, revoked in self.provider.keys.values():
            if (
                secret == token
                and not revoked
                and self.provider.allocations[allocation_id].status != "revoked"
            ):
                return allocation_id
        return None


class Lossy(httpx.AsyncBaseTransport):
    """The wire into the clearinghouse's app, which can lose an answer after the app handled the request.
    ``answers`` is what the app answered each request with, lost or not, as (status, body)."""

    def __init__(self, app: httpx.ASGITransport, requests: list[httpx.Request], lost: list[str]) -> None:
        self._app = app
        self._requests = requests
        self._lost = lost
        self.answers: list[tuple[int, bytes]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._requests.append(request)
        response = await self._app.handle_async_request(request)
        self.answers.append((response.status_code, await response.aread()))
        for ending in self._lost:
            if request.url.path.endswith(ending):
                self._lost.remove(ending)
                await response.aclose()
                raise httpx.ReadError("the connection dropped before the answer")
        return response


def usd(amount: str | int) -> Money:
    return Money(amount=Decimal(amount), currency="USD")


@contextlib.asynccontextmanager
async def open_ports(
    side: Side, served: AppOf | None = None, house: dict[str, object] | None = None
) -> AsyncIterator[Ports]:
    """``served`` builds the app the enterprise's engine calls from the engine behind it (remote): a
    standalone clearinghouse's public listener unless a test serves something else. ``house`` is that
    engine's role and listeners: a clearinghouse's unless a test puts another engine there."""
    provider, provider_usage = FakeAllocationManager(), FakeUsageSource()
    if side == "local":
        async with _local(provider, provider_usage) as ports:
            yield ports
    else:
        role = CLEARINGHOUSE_ROLE if house is None else house
        async with _remote(provider, provider_usage, served or standalone_clearinghouse, role) as ports:
            yield ports


def standalone_clearinghouse(house: BuilderEngine) -> FastAPI:
    """What a payment clearinghouse serves an enterprise's engine: the public listener of an engine in
    its ``clearinghouse`` role, as ``lpb serve`` builds it. The role decides that each allocation's key
    is handed to the caller once and none is stored; nothing here says so."""
    return create_app(house, manage_lifecycle=False, listener="public")


@contextlib.asynccontextmanager
async def _local(provider: FakeAllocationManager, usage: FakeUsageSource) -> AsyncIterator[Ports]:
    one = provider.add_grant("grant_1", GRANT_TOTAL, name="one")
    two = provider.add_grant("grant_2", GRANT_TOTAL, name="two")
    supply = EngineSupply(allocation_manager=provider, grants_manager=provider, usage_source=usage)
    async with make_engine(supply=supply) as engine:
        yield Ports(
            side="local",
            engine=engine,
            provider=provider,
            provider_usage=usage,
            tenant_id=one.id,
            other_tenant=two.id,
            manager=provider,
            allowances=engine.payments,
            usage=usage,
            credentials=engine.payments,
        )


@contextlib.asynccontextmanager
async def _remote(
    provider: FakeAllocationManager, usage: FakeUsageSource, served: AppOf, role: dict[str, object]
) -> AsyncIterator[Ports]:
    # The clearinghouse's own engine, store and secret store: nothing of them is the enterprise engine's.
    house_secrets = MemorySecretStore()
    house_supply = EngineSupply(
        allocation_manager=provider, grants_manager=provider, usage_source=usage, secret_store=house_secrets
    )
    async with contextlib.AsyncExitStack() as stack:
        house = await stack.enter_async_context(make_engine(supply=house_supply, **role))
        assert house.payments.tenants is not None
        one = await house.payments.tenants.onboard(OPERATOR, "one", usd(100), operation_ref="onboard-one")
        two = await house.payments.tenants.onboard(OPERATOR, "two", usd(100), operation_ref="onboard-two")
        assert one.admin_key is not None and one.admin_key.token is not None
        token = one.admin_key.token

        requests: list[httpx.Request] = []
        lost: list[str] = []
        # a failure inside the clearinghouse's app is its 500, as over a socket, and never this side's error
        inside = httpx.ASGITransport(app=served(house), raise_app_exceptions=False)
        lossy = Lossy(inside, requests, lost)
        client = await stack.enter_async_context(httpx.AsyncClient(transport=lossy))
        wire = ClearinghouseWire(CLEARINGHOUSE_URL, token, one.tenant_id, client)
        store = await SqlStore.connect("sqlite+aiosqlite:///:memory:")
        stack.push_async_callback(store.close)
        secrets = MemorySecretStore()
        remote = ClearinghousePorts.over(wire, store, secrets)
        supply = EngineSupply(
            store=store,
            secret_store=secrets,
            allocation_manager=remote.allocations,
            grants_manager=remote.allocations,
            usage_source=remote.usage,
            allowances=remote.allowances,
        )
        settings = {
            "role": "work",
            "clearinghouse_url": CLEARINGHOUSE_URL,
            "clearinghouse_token": token,
            "clearinghouse_tenant_id": one.tenant_id,
        }
        engine = await stack.enter_async_context(make_engine(supply=supply, **settings))
        yield Ports(
            side="remote",
            engine=engine,
            provider=provider,
            provider_usage=usage,
            tenant_id=one.tenant_id,
            other_tenant=two.tenant_id,
            manager=remote.allocations,
            allowances=remote.allowances,
            usage=remote.usage,
            credentials=engine.payments,  # the engine's own: the key it was handed, from its own store
            clearinghouse=house,
            secrets=secrets,
            clearinghouse_secrets=house_secrets,
            requests=requests,
            token=token,
            lost=lost,
            answers=lossy.answers,
        )
