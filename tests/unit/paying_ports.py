"""The two sets of payment adapters with a signer and a runner behind them, for the shared suite's
signing section (#168).

``open_paying_ports("local", net)`` is a self-hosted engine: its transport signs at the loopback signer
(``tests/unit/livepeer_net.py``) with the actor's Batteries key. ``open_paying_ports("remote", net)`` is
an enterprise's engine built from its settings alone, so all five of its payment ports are the defaults
of an engine that uses a clearinghouse: the three remote adapters, its own stored keys, and the gateway
SDK's transport at ``LPB_SIGNER_URL``, which names the same loopback signer: the clearinghouse's
published one. The clearinghouse is a real engine of its own behind a standalone clearinghouse's public
listener, on a loopback socket of its own. It runs no job and has no signer address. The runner is the
same on both sides.

``seen`` is every request that arrived at the clearinghouse's socket, in order: the tenant's routes,
and nothing of a payment.

Since #169 each engine is in the role its side has: the self-hosted engine in ``both``, the enterprise's
in ``work`` and the clearinghouse in ``clearinghouse``. The clearinghouse keeps its records in a file: its
listener answers several requests at once, and an in-memory database is one connection, which two
requests at once could leave unusable.
"""

import contextlib
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
from aiohttp import web
from aiohttp.test_utils import TestServer
from fastapi import FastAPI
from multidict import CIMultiDict

from livepeer_builder import BuilderEngine, EngineSettings, EngineSupply
from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.config import generate_secret_key
from livepeer_builder.contracts import Runner
from livepeer_builder.testing import FakeAllocationManager, FakeUsageSource, FixedClock, make_engine, serve
from tests.unit.livepeer_net import Net
from tests.unit.payment_ports import (
    CLEARINGHOUSE_ROLE,
    GRANT_TOTAL,
    OPERATOR,
    Ports,
    Side,
    standalone_clearinghouse,
    usd,
)

FIXED_APP = "example/echo"
LIVE_APP = "example/live"


@dataclass
class Listening:
    url: str
    seen: list[tuple[str, CIMultiDict[str]]] = field(default_factory=list)  # (path, headers) of each request


@contextlib.asynccontextmanager
async def listening(app: FastAPI) -> AsyncIterator[Listening]:
    """``app`` on a loopback socket: an aiohttp server hands every request to it, in process, and returns
    its answer. ``seen`` records what arrived, in order."""
    served = Listening(url="")
    inside = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://clearinghouse")

    async def handle(request: web.Request) -> web.Response:
        served.seen.append((request.path, CIMultiDict(request.headers)))
        headers = [(name, value) for name, value in request.headers.items() if name.lower() != "host"]
        answer = await inside.request(
            request.method, request.raw_path, content=await request.read(), headers=headers
        )
        kept = {name: value for name, value in answer.headers.items() if name.lower() != "content-length"}
        return web.Response(status=answer.status_code, body=answer.content, headers=kept)

    outside = web.Application()
    outside.router.add_route("*", "/{path:.*}", handle)
    async with inside, TestServer(outside) as server:
        served.url = str(server.make_url("")).rstrip("/")
        yield served


def runners(net: Net) -> tuple[Runner, Runner]:
    """One runner priced for each request and one priced by the second, each under its own app."""
    live = net.runner(unit="seconds", name="live").model_copy(update={"app": LIVE_APP})
    return net.runner(), live


@contextlib.asynccontextmanager
async def open_paying_ports(side: Side, net: Net) -> AsyncIterator[Ports]:
    provider, usage = FakeAllocationManager(), FakeUsageSource()
    if side == "local":
        async with _self_hosted(provider, usage, net) as ports:
            yield ports
    else:
        async with _through_a_clearinghouse(provider, usage, net) as ports:
            yield ports


@contextlib.asynccontextmanager
async def _self_hosted(
    provider: FakeAllocationManager, usage: FakeUsageSource, net: Net
) -> AsyncIterator[Ports]:
    one = provider.add_grant("grant_1", GRANT_TOTAL, name="one")
    two = provider.add_grant("grant_2", GRANT_TOTAL, name="two")
    supply = EngineSupply(
        allocation_manager=provider,
        grants_manager=provider,
        usage_source=usage,
        transport=SdkRunnerTransport(net.url),
    )
    async with make_engine(runners=runners(net), supply=supply) as engine:
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
async def _through_a_clearinghouse(
    provider: FakeAllocationManager, usage: FakeUsageSource, net: Net
) -> AsyncIterator[Ports]:
    # The clearinghouse's engine pays for nothing: it has no signer address, and its app serves no job.
    house_supply = EngineSupply(allocation_manager=provider, grants_manager=provider, usage_source=usage)
    async with contextlib.AsyncExitStack() as stack:
        folder = stack.enter_context(tempfile.TemporaryDirectory(prefix="lpb-clearinghouse-"))
        database = f"sqlite+aiosqlite:///{folder}/clearinghouse.db"
        house = await stack.enter_async_context(
            make_engine(supply=house_supply, database_url=database, **CLEARINGHOUSE_ROLE)
        )
        assert house.payments.tenants is not None
        one = await house.payments.tenants.onboard(OPERATOR, "one", usd(100), operation_ref="onboard-one")
        two = await house.payments.tenants.onboard(OPERATOR, "two", usd(100), operation_ref="onboard-two")
        assert one.admin_key is not None and one.admin_key.token is not None
        token = one.admin_key.token
        served = await stack.enter_async_context(listening(standalone_clearinghouse(house)))

        # The enterprise's engine, from its settings and nothing else of payments: every port a default.
        settings = EngineSettings.model_validate(
            {
                "database_url": "sqlite+aiosqlite:///:memory:",
                "secret_key": generate_secret_key(),
                "log_json": False,
                "role": "work",
                "clearinghouse_url": served.url,
                "clearinghouse_token": token,
                "clearinghouse_tenant_id": one.tenant_id,
                "signer_url": net.url,  # the clearinghouse's published signer: the engine signs there itself
            }
        )
        install, discovery = serve(runners(net))
        stack.push_async_callback(discovery.aclose)
        engine = await BuilderEngine.create(
            settings, supply=EngineSupply(clock=FixedClock()), decorate=install
        )
        stack.push_async_callback(engine.stop)
        await engine.discovery.refresh()
        payments = engine.payments
        assert payments._manager is not None and payments._allowances is not None
        assert engine.costs._worker is not None
        yield Ports(
            side="remote",
            engine=engine,
            provider=provider,
            provider_usage=usage,
            tenant_id=one.tenant_id,
            other_tenant=two.tenant_id,
            manager=payments._manager,
            allowances=payments._allowances,
            usage=engine.costs._worker._source,
            credentials=engine.jobs._dispatcher._credentials,  # the engine's own resolver: PaymentsService
            clearinghouse=house,
            token=token,
            seen=served.seen,
        )
