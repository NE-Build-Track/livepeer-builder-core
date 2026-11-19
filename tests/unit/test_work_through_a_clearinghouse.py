"""#169's "Done when", in one process: an engine in its ``work`` role runs a paid job through an engine
in its ``clearinghouse`` role that holds two tenants, and an engine in its ``both`` role still runs a
paid job by itself.

Four parties, with the engine's real code between them and every hop on a loopback socket:

- the **clearinghouse** is a real engine in its ``clearinghouse`` role. Its public listener is on a
  socket; its private listener is called in process, as a clearinghouse admin on the same host would.
  Batteries is the shipped fake, and nobody but this engine reaches it or the signer;
- the **enterprise's engine** is a real engine in its ``work`` role, built from its settings and nothing
  else of payments: the clearinghouse's address, tenant one's admin key and tenant one's id. It reads the
  clearinghouse's runner list by default, and its jobs are run through its own REST app;
- the **signer**, the runner and its orchestrator are ``tests/unit/livepeer_net.py``.

Tenant two has an actor and an allocation of its own, allotted at the clearinghouse directly, and no
engine here: what tenant one does must leave it as it was.

An engine in its ``work`` role serves no route that allots (the clearinghouse routes are not its own), so
tenant one's actor is allotted through the embedded API of the enterprise's engine, which is where its
payment key must be kept.
"""

import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from livepeer_gateway import remote_signer

from livepeer_builder import BuilderEngine, EngineSettings, EngineSupply, generate_secret_key
from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.contracts import ActorContext, AllotRequest, Money
from livepeer_builder.http import create_app
from livepeer_builder.http.replies import JOB_ID_HEADER
from livepeer_builder.testing import FakeAllocationManager, FakeUsageSource, FixedClock, make_engine
from tests.unit.clearinghouse_net import Listening, listening
from tests.unit.livepeer_net import Net, Reply, start
from tests.unit.paying_ports import FIXED_APP, LIVE_APP, open_paying_ports, runners
from tests.unit.role_engines import PRIVATE_PORT, served
from tests.unit.test_admin_route_sweep import filled
from tests.unit.test_listeners import body_for
from tests.unit.test_payment_port_suite import row
from tests.unit.test_tenants import Tenant, onboard

OPERATOR = ActorContext(actor_id="operator", credential_kind="clearinghouse_admin")
TENANTS = "/v1/clearinghouse/tenants"
SIGNER_PATHS = {"/sign-orchestrator-info", "/generate-live-payment"}
PROMPT = "kept from the clearinghouse"


def client_of(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine")


def bearer(token: str | None) -> dict[str, str]:
    assert token is not None
    return {"Authorization": f"Bearer {token}"}


def usd(amount: str) -> Money:
    return Money(amount=Decimal(amount), currency="USD")


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        yield value


@dataclass
class Arrangement:
    net: Net
    provider: FakeAllocationManager  # Batteries' stand-in, behind the clearinghouse and nobody else
    provider_usage: FakeUsageSource
    house: BuilderEngine  # the clearinghouse role
    public: Listening  # its public listener, on a loopback socket
    public_app: FastAPI
    private: httpx.AsyncClient  # its private listener
    admin: dict[str, str]  # the clearinghouse admin's key
    one: Tenant
    two: Tenant
    enterprise: BuilderEngine  # the work role: tenant one's engine
    work: httpx.AsyncClient  # the enterprise engine's own REST app
    enterprise_admin: dict[str, str]
    alice: dict[str, str]  # an actor of tenant one, at the enterprise's engine
    alice_allocation: str
    bob_allocation: str  # tenant two's actor, allotted at the clearinghouse directly
    outside: httpx.AsyncClient  # anybody on the network, calling the public listener's socket

    def batteries_key(self, allocation_id: str) -> str:
        [secret] = [secret for held, secret, _ in self.provider.keys.values() if held == allocation_id]
        return secret

    def crossed(self) -> list[tuple[str, str]]:
        """(path, Authorization) of every request that arrived at the clearinghouse's public socket."""
        return [(path, headers.get("Authorization", "")) for path, headers in self.public.seen]


@pytest.fixture
async def arrangement(net: Net, tmp_path: Path) -> AsyncIterator[Arrangement]:
    provider, usage = FakeAllocationManager(), FakeUsageSource()
    house_supply = EngineSupply(
        allocation_manager=provider,
        grants_manager=provider,
        usage_source=usage,
        transport=SdkRunnerTransport(net.url),  # the clearinghouse's signer: nobody else reaches it
    )
    async with contextlib.AsyncExitStack() as stack:
        house = await stack.enter_async_context(
            make_engine(
                runners=runners(net),  # the runner list the clearinghouse publishes
                supply=house_supply,
                role="clearinghouse",
                http_private_port=PRIVATE_PORT,
                # a file: the public listener answers several requests at once
                database_url=f"sqlite+aiosqlite:///{tmp_path / 'clearinghouse.db'}",
            )
        )
        assert house.keys is not None
        admin = bearer((await house.keys.bootstrap_admin()).token)
        public_app = create_app(house, manage_lifecycle=False, listener="public")
        public = await stack.enter_async_context(listening(public_app))
        private = await stack.enter_async_context(
            client_of(create_app(house, manage_lifecycle=False, listener="private"))
        )
        outside = await stack.enter_async_context(httpx.AsyncClient(base_url=public.url))

        # The clearinghouse admin onboards two tenants, on the private listener.
        one, two = await onboard(private, admin, "one"), await onboard(private, admin, "two")

        # Tenant two allots its actor at the clearinghouse itself, on the public listener.
        body = {"amount": {"amount": "5", "currency": "USD"}, "operation_ref": "allot-bob"}
        bob = await outside.post(f"{TENANTS}/{two.id}/actors/bob/allocation", json=body, headers=two.admin)
        assert bob.status_code == 201 and bob.json()["payment_key"]["token"].startswith("lpp_"), bob.text

        # Tenant one's engine: the work role, from its settings and nothing else of payments.
        settings = EngineSettings.model_validate(
            {
                "role": "work",
                "database_url": "sqlite+aiosqlite:///:memory:",
                "secret_key": generate_secret_key(),
                "log_json": False,
                "clearinghouse_url": public.url,
                "clearinghouse_token": one.admin["Authorization"].removeprefix("Bearer "),
                "clearinghouse_tenant_id": one.id,
            }
        )
        enterprise = await BuilderEngine.create(settings, supply=EngineSupply(clock=FixedClock()))
        stack.push_async_callback(enterprise.stop)
        await enterprise.discovery.refresh()  # the clearinghouse's runner list, read over the socket
        assert enterprise.keys is not None
        enterprise_admin = bearer((await enterprise.keys.bootstrap_admin()).token)
        work = await stack.enter_async_context(client_of(create_app(enterprise, manage_lifecycle=False)))
        key = {"kind": "actor", "actor_id": "alice", "tenant_id": one.id, "operation_ref": "key-alice"}
        issued = await work.post("/v1/keys", json=key, headers=enterprise_admin)
        assert issued.status_code == 201, issued.text
        allotted = await enterprise.payments.allot(
            OPERATOR,
            AllotRequest(tenant_id=one.id, actor_id="alice", amount=usd("5"), operation_ref="allot-alice"),
        )
        yield Arrangement(
            net=net,
            provider=provider,
            provider_usage=usage,
            house=house,
            public=public,
            public_app=public_app,
            private=private,
            admin=admin,
            one=one,
            two=two,
            enterprise=enterprise,
            work=work,
            enterprise_admin=enterprise_admin,
            alice=bearer(issued.json()["token"]),
            alice_allocation=allotted.allocation_id,
            bob_allocation=bob.json()["allocation_id"],
            outside=outside,
        )


async def jobs_recorded(engine: BuilderEngine) -> int:
    async with engine.store.engine.connect() as conn:  # type: ignore[attr-defined]
        return int((await conn.exec_driver_sql("SELECT count(*) FROM lpb_jobs")).scalar_one())


async def charged(arrangement: Arrangement, manifest_id: str, fee: str) -> None:
    """Batteries charges the payment to the allocation it was signed for, as it does once the signer's
    accounting event arrives: a usage row, and what the allocation has spent."""
    allocation_id = arrangement.alice_allocation
    usage_row = row(len(arrangement.provider_usage.rows) + 1, allocation_id).model_copy(
        update={"manifest_id": manifest_id, "computed_fee_usd": Decimal(fee)}
    )
    arrangement.provider_usage.rows.append(usage_row)
    arrangement.provider_usage.grant_of[allocation_id] = arrangement.one.id
    arrangement.provider.allocations[allocation_id].spent += Decimal(fee)


async def run_job(arrangement: Arrangement, app: str, payload: dict[str, Any]) -> dict[str, Any]:
    """One job through the enterprise engine's own ``POST /v1/jobs``, and its record."""
    answer = await arrangement.work.post(
        "/v1/jobs", json={"app": app, "payload": payload}, headers=arrangement.alice
    )
    assert answer.status_code == 200, answer.text
    record = await arrangement.work.get(
        f"/v1/jobs/{answer.headers[JOB_ID_HEADER]}", headers=arrangement.alice
    )
    job: dict[str, Any] = record.json()
    return job


# -- the ticket's "Done when" ------------------------------------------------------------------------------


async def test_a_work_engine_runs_a_paid_fixed_job_and_a_paid_live_job_through_a_clearinghouse(
    arrangement: Arrangement, monkeypatch: pytest.MonkeyPatch
) -> None:
    house, net = arrangement, arrangement.net
    assert (house.enterprise.role, house.house.role) == ("work", "clearinghouse")
    signs_with = f"Bearer {house.batteries_key(house.alice_allocation)}"
    before = len(house.public.seen)

    # fixed work: one payment, up front
    fixed = await run_job(house, FIXED_APP, {"prompt": PROMPT})
    assert fixed["state"] == "succeeded"
    [attempt] = fixed["attempts"]
    assert attempt["payment_sent"] and attempt["allocation_id"] == house.alice_allocation
    assert [session["manifest_id"] for session in attempt["sessions"]] == ["m-1"]
    assert net.log.count("sign m-1") == 1 and "request pay-m-1" in net.log

    # live work: a payment each cycle, for as long as the work runs
    monkeypatch.setattr(remote_signer, "PAYMENT_INTERVAL_S", 0.02)
    net.replies = [Reply(hang_s=0.5)]
    live = await run_job(house, LIVE_APP, {"n": 1})
    assert live["state"] == "succeeded"
    assert [session["manifest_id"] for session in live["attempts"][0]["sessions"]] == ["m-2"]
    assert net.log.count("sign m-2") >= 3  # the opening payment, then the cycles
    assert all(call["type"] == "live" for call in net.signer_requests[1:])

    # The signer was called by the clearinghouse alone, with the Batteries key of tenant one's actor.
    assert {headers.get("Authorization") for headers in net.signer_headers} == {signs_with}
    # What crossed to the clearinghouse while the jobs ran: the signer's two paths, each with the
    # actor's payment key, which is neither the tenant admin key nor the Batteries key.
    crossed = house.crossed()[before:]
    assert {path for path, _ in crossed} == SIGNER_PATHS
    [payment_key] = {authorization for _, authorization in crossed}
    assert payment_key.startswith("Bearer lpp_")
    assert payment_key not in (signs_with, house.one.admin["Authorization"])
    # Payment calls only: the prompt went to the runner and never to the clearinghouse or its signer.
    assert PROMPT not in json.dumps(net.signer_requests)
    assert {"prompt": PROMPT} in net.bodies

    # The job's cost, once Batteries has charged it and the enterprise's engine has synced its usage.
    await charged(house, "m-1", "0.25")
    await charged(house, "m-2", "0.5")
    synced = await house.work.post("/v1/costs/sync", headers=house.enterprise_admin)
    assert synced.status_code == 200, synced.text
    cost = (await house.work.get(f"/v1/jobs/{fixed['id']}/cost", headers=house.alice)).json()
    assert (cost["status"], Decimal(cost["fees"]["fee_usd"])) == ("observed", Decimal("0.25"))
    live_cost = (await house.work.get(f"/v1/jobs/{live['id']}/cost", headers=house.alice)).json()
    assert (live_cost["status"], Decimal(live_cost["billable"]["fee_usd"])) == ("observed", Decimal("0.5"))

    # The jobs are the enterprise's: the clearinghouse recorded none, and ran none.
    assert await jobs_recorded(house.enterprise) == 2
    assert await jobs_recorded(house.house) == 0


async def test_the_second_tenant_is_untouched_by_what_the_first_one_paid(
    arrangement: Arrangement,
) -> None:
    house = arrangement
    await run_job(house, FIXED_APP, {"prompt": PROMPT})
    await charged(house, "m-1", "0.25")

    # No call to the signer carried the Batteries key of tenant two's actor.
    theirs = f"Bearer {house.batteries_key(house.bob_allocation)}"
    assert theirs not in {headers.get("Authorization") for headers in house.net.signer_headers}
    # Tenant two's usage, read by its own admin on the public listener: nothing. Tenant one's: its row.
    usage_two = await house.outside.get(f"{TENANTS}/{house.two.id}/usage", headers=house.two.admin)
    usage_one = await house.outside.get(f"{TENANTS}/{house.one.id}/usage", headers=house.one.admin)
    assert (usage_two.status_code, usage_two.json()["items"]) == (200, [])
    assert [item["manifest_id"] for item in usage_one.json()["items"]] == ["m-1"]
    # Its allocation is as it was allotted, and its grant has spent nothing.
    allocation = f"{TENANTS}/{house.two.id}/actors/bob/allocation"
    bob = (await house.outside.get(allocation, headers=house.two.admin)).json()
    assert (bob["allowance"]["spent"]["amount"], bob["allowance"]["available"]["amount"]) == ("0", "5")
    tenants = (await house.private.get(TENANTS, headers=house.admin)).json()
    spent = {tenant["tenant_id"]: Decimal(tenant["spent"]["amount"]) for tenant in tenants}
    assert spent == {house.one.id: Decimal("0.25"), house.two.id: Decimal(0)}
    # And the first tenant's engine asked for nothing of the second: with tenant one's admin key, every
    # path that names a tenant names tenant one.
    as_one = [path for path, key in house.crossed() if key == house.one.admin["Authorization"]]
    assert as_one and all(f"/tenants/{house.one.id}/" in path for path in as_one if "/tenants/" in path)


async def test_the_first_tenants_admin_is_refused_on_every_route_of_the_second_tenant(
    arrangement: Arrangement,
) -> None:
    """Every route of the public listener that names a tenant, with tenant two's id and tenant one's
    admin key: 403 before the provider is asked, and nothing of tenant two changes."""
    house = arrangement
    params = {"tenant_id": house.two.id, "actor_id": "bob", "key_id": house.two.key_id}
    named = {key: route for key, route in served(house.public_app).items() if "{tenant_id}" in key[1]}
    assert len(named) >= 14, f"the sweep found too few tenant routes to be meaningful: {len(named)}"
    calls_before = list(house.provider.calls)
    wrong: list[str] = []
    for (method, path), route in sorted(named.items()):
        answer = await house.outside.request(
            method, filled(path, params), json=body_for(route), headers=house.one.admin
        )
        if answer.status_code != 403:
            wrong.append(f"{method} {path}: {answer.status_code} {answer.text[:80]}")
    assert not wrong, "tenant one's admin was answered on tenant two's routes:\n" + "\n".join(wrong)
    assert house.provider.calls == calls_before
    own = await house.outside.get(f"{TENANTS}/{house.one.id}/grant", headers=house.one.admin)
    assert own.status_code == 200 and own.json()["tenant_id"] == house.one.id


async def test_each_side_serves_its_own_half_and_the_admin_routes_stay_private(
    arrangement: Arrangement,
) -> None:
    house = arrangement
    body = {"name": "three", "amount": {"amount": "1", "currency": "USD"}, "operation_ref": "onboard-3"}
    # the clearinghouse's published listener: no job route, no onboarding, and no use for its admin's key
    assert (
        await house.outside.post("/v1/jobs", json={"app": FIXED_APP}, headers=house.one.admin)
    ).status_code == 404
    assert (await house.outside.get("/v1/offerings")).status_code == 404
    assert (await house.outside.post(TENANTS, json=body, headers=house.admin)).status_code == 404
    assert (
        await house.outside.get(f"{TENANTS}/{house.one.id}/grant", headers=house.admin)
    ).status_code == 403
    assert (await house.outside.get("/readyz")).json()["role"] == "clearinghouse"
    # the private listener: the wallet report, for the clearinghouse admin
    wallet = await house.private.get("/v1/clearinghouse/wallet", headers=house.admin)
    assert wallet.status_code == 200 and wallet.json()["signer"]["ready"] is True
    # the enterprise's engine: no clearinghouse route and no signer path, and its own catalog
    grant = f"{TENANTS}/{house.one.id}/grant"
    assert (await house.work.get(grant, headers=house.enterprise_admin)).status_code == 404
    assert (await house.work.post("/sign-orchestrator-info", content=b"{}")).status_code == 404
    assert (await house.work.get("/readyz")).json()["role"] == "work"
    assert {offering["app"] for offering in (await house.work.get("/v1/offerings")).json()} == {
        FIXED_APP,
        LIVE_APP,
    }


# -- an engine in its both role still pays by itself ---------------------------------------------------------


async def test_a_both_engine_still_runs_a_paid_job_by_itself_on_one_listener(net: Net) -> None:
    """Self-hosted payments, as one deployment with one listener: the engine's own admin onboards
    nothing new, allots on the clearinghouse routes of the same app, and the job is paid by the engine's
    own signer with the actor's Batteries key."""
    async with open_paying_ports("local", net) as ports:
        engine = ports.engine
        assert engine.role == "both" and engine.keys is not None
        admin = bearer((await engine.keys.bootstrap_admin()).token)
        async with client_of(create_app(engine, manage_lifecycle=False)) as client:
            tenant = f"{TENANTS}/{ports.tenant_id}"
            body = {"amount": {"amount": "5", "currency": "USD"}, "operation_ref": "allot-alice"}
            allotted = await client.post(f"{tenant}/actors/alice/allocation", json=body, headers=admin)
            assert allotted.status_code == 201, allotted.text
            key = {"kind": "actor", "actor_id": "alice", "tenant_id": ports.tenant_id, "operation_ref": "k"}
            alice = bearer((await client.post("/v1/keys", json=key, headers=admin)).json()["token"])
            answer = await client.post("/v1/jobs", json={"app": FIXED_APP, "payload": {}}, headers=alice)
            assert answer.status_code == 200, answer.text
            job = (await client.get(f"/v1/jobs/{answer.headers[JOB_ID_HEADER]}", headers=alice)).json()
        assert job["state"] == "succeeded" and job["attempts"][0]["payment_sent"]
        [batteries_key] = [secret for _, secret, _ in ports.provider.keys.values()]
        assert {headers.get("Authorization") for headers in net.signer_headers} == {f"Bearer {batteries_key}"}
        assert net.log.count("sign m-1") == 1 and "request pay-m-1" in net.log
