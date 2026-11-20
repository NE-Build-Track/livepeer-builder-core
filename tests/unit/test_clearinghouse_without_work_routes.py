"""#168: an enterprise's engine needs no work route of its clearinghouse.

A payment clearinghouse does payments only and never runs jobs, so it will serve no work route (the role
setting, #169). Everything an enterprise's engine asks of it is therefore a clearinghouse route: the
tenant's routes, the runner list and the signer's two paths. Here the clearinghouse's app is built from
those groups alone, with the work routes left out, and the remote adapters, the discovery reader and
``lpb payments check`` do all they do against it.
"""

from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI

from livepeer_builder import BuilderEngine, EngineSettings
from livepeer_builder.adapters.payment_probes import probe_payments
from livepeer_builder.adapters.sdk import HttpDiscoverySource
from livepeer_builder.contracts import AllotRequest, TopUpRequest
from livepeer_builder.errors import ProviderError, ProviderUnavailable
from livepeer_builder.http.app import shared_app
from livepeer_builder.http.clearinghouse import mount_tenant_routes
from livepeer_builder.http.signing import mount_signing_routes
from tests.unit.payment_ports import CLEARINGHOUSE_URL, Ports, open_ports, usd

WORK_ROUTES = ("/v1/allowance", "/v1/jobs", "/v1/runners", "/v1/costs/summary", "/v1/keys", "/v1/offerings")


def payments_only(house: BuilderEngine) -> FastAPI:
    """A clearinghouse's app with the routes of its own half and no work route."""
    app = shared_app(house, manage_lifecycle=False)
    mount_tenant_routes(app, house)
    mount_signing_routes(app, house)
    return app


@pytest.fixture
async def ports() -> AsyncIterator[Ports]:
    async with open_ports("remote", payments_only) as opened:
        yield opened


def client_of(ports: Ports) -> httpx.AsyncClient:
    return ports.manager._wire._client  # type: ignore[attr-defined,no-any-return]


async def allotted(ports: Ports, actor_id: str = "alice", amount: str = "5") -> str:
    request = AllotRequest(
        tenant_id=ports.tenant_id, actor_id=actor_id, amount=usd(amount), operation_ref=f"allot-{actor_id}"
    )
    return (await ports.engine.payments.allot(ports.admin, request)).allocation_id


async def test_this_clearinghouse_serves_no_work_route(ports: Ports) -> None:
    bearer = {"Authorization": f"Bearer {ports.token}"}
    for path in WORK_ROUTES:
        answer = await client_of(ports).get(CLEARINGHOUSE_URL + path, headers=bearer)
        assert answer.status_code == 404, path


async def test_the_allowance_is_read_from_the_allocation_route(ports: Ports) -> None:
    allocation_id = await allotted(ports)
    ports.provider.allocations[allocation_id].spent = Decimal("1.25")

    allowance = await ports.engine.payments.allowance(ports.actor("alice"))
    by_id = await ports.allowances.read_allowance(allocation_id)
    reconciled = await ports.engine.costs.reconcile(ports.admin, ports.tenant_id, "alice")

    assert allowance == by_id and allowance.allocation_id == allocation_id
    assert (allowance.granted, allowance.spent, allowance.available) == (usd(5), usd("1.25"), usd("3.75"))
    assert reconciled.provider_spent == usd("1.25")
    assert set(ports.sent("GET")) == {f"/v1/clearinghouse/tenants/{ports.tenant_id}/actors/alice/allocation"}


async def test_every_command_and_read_of_the_adapters_is_a_clearinghouse_route(ports: Ports) -> None:
    payments = ports.engine.payments
    allocation_id = await allotted(ports)
    await payments.top_up(
        ports.admin, ports.tenant_id, "alice", TopUpRequest(amount=usd(2), operation_ref="t-1")
    )
    await payments.set_status(ports.admin, ports.tenant_id, "alice", "paused")
    await payments.set_status(ports.admin, ports.tenant_id, "alice", "active")
    await payments.rotate_payment_key(ports.admin, ports.tenant_id, "alice", "r-1")
    await payments.allowance(ports.actor("alice"))
    await ports.manager.read_grant(ports.tenant_id)
    await ports.usage.page(None)
    await ports.engine.costs.sync_now(ports.admin)
    await payments.revoke(ports.admin, ports.tenant_id, "alice")

    assert ports.provider.allocations[allocation_id].status == "revoked"
    tenant = f"/v1/clearinghouse/tenants/{ports.tenant_id}/"
    paths = {request.url.path for request in ports.requests}
    assert paths and all(path.startswith(tenant) for path in paths), sorted(paths)


async def test_an_allowance_the_clearinghouse_could_not_read_is_unavailable_and_never_a_zero(
    ports: Ports,
) -> None:
    allocation_id = await allotted(ports)
    ports.provider.fail_once("read_allocation", ProviderUnavailable("Batteries unreachable"))
    with pytest.raises(ProviderUnavailable, match="could not read"):
        await ports.allowances.read_allowance(allocation_id)
    assert (await ports.allowances.read_allowance(allocation_id)).granted == usd(5)


async def test_an_actor_of_another_tenant_is_refused_by_the_clearinghouse(ports: Ports) -> None:
    theirs = ports.manager.for_actor(ports.other_tenant, "mallory")  # type: ignore[attr-defined]
    with pytest.raises(ProviderError) as refused:
        await theirs.read_allocation("alloc_1")
    assert refused.value.status_code == 403 and "another tenant" in refused.value.message


async def test_the_discovery_reader_reads_the_runner_list_there(ports: Ports) -> None:
    reader = HttpDiscoverySource(
        CLEARINGHOUSE_URL + "/v1/clearinghouse/discovery", client=client_of(ports), bearer=ports.token
    )
    assert ports.clearinghouse is not None
    assert await reader.fetch() == ports.clearinghouse.discovery.snapshot.runners


async def test_the_payments_check_passes_there_with_every_probe(ports: Ports) -> None:
    settings = EngineSettings.model_validate(
        {
            "role": "work",
            "clearinghouse_url": CLEARINGHOUSE_URL,
            "clearinghouse_token": ports.token,
            "clearinghouse_tenant_id": ports.tenant_id,
        }
    )
    before = len(ports.requests)
    report = await probe_payments(settings, client=client_of(ports))
    assert report.ok, report
    assert [probe.name for probe in report.probes] == [
        "clearinghouse_grant",
        "clearinghouse_usage",
        "clearinghouse_signer",
        "clearinghouse_runner_list",
    ]
    assert report.tenant is not None and report.tenant.tenant_id == ports.tenant_id
    asked = ports.requests[before:]
    assert {request.method for request in asked} == {"GET"}
    for request in asked:
        path = request.url.path
        assert path.startswith("/v1/clearinghouse/") or path == "/sign-orchestrator-info", path
