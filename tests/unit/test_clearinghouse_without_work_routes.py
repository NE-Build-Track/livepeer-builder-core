"""#168: an enterprise's engine needs no work route of its clearinghouse.

A payment clearinghouse does payments only and never runs jobs, so it will serve no work route (the role
setting, #169). Everything an enterprise's engine asks of it is therefore a clearinghouse route: the
tenant's routes and the runner list. Here the clearinghouse's app is a standalone clearinghouse's public
listener, which is those routes alone (``tests/unit/payment_ports.py``), and the remote adapters, the
discovery reader and ``lpb payments check`` do all they do against it. Signing is not among them: the
engine signs at the clearinghouse's signer, which is no route of an engine.
"""

from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest

from livepeer_builder import EngineSettings
from livepeer_builder.adapters.payment_probes import probe_payments
from livepeer_builder.adapters.sdk import HttpDiscoverySource
from livepeer_builder.contracts import AllotRequest, TopUpRequest
from livepeer_builder.errors import ProviderError, ProviderUnavailable
from tests.unit.payment_ports import CLEARINGHOUSE_URL, Ports, open_ports, usd

WORK_ROUTES = ("/v1/allowance", "/v1/jobs", "/v1/runners", "/v1/costs/summary", "/v1/keys", "/v1/offerings")
SIGNER_PATHS = ("/sign-orchestrator-info", "/generate-live-payment")


@pytest.fixture
async def ports() -> AsyncIterator[Ports]:
    async with open_ports("remote") as opened:
        yield opened


def client_of(ports: Ports) -> httpx.AsyncClient:
    return ports.manager._wire._client  # type: ignore[attr-defined,no-any-return]


async def allotted(ports: Ports, actor_id: str = "alice", amount: str = "5") -> str:
    request = AllotRequest(
        tenant_id=ports.tenant_id, actor_id=actor_id, amount=usd(amount), operation_ref=f"allot-{actor_id}"
    )
    return (await ports.engine.payments.allot(ports.admin, request)).allocation_id


async def test_this_clearinghouse_serves_no_work_route_and_no_signer_path(ports: Ports) -> None:
    bearer = {"Authorization": f"Bearer {ports.token}"}
    for path in WORK_ROUTES:
        answer = await client_of(ports).get(CLEARINGHOUSE_URL + path, headers=bearer)
        assert answer.status_code == 404, path
    for path in SIGNER_PATHS:
        answer = await client_of(ports).post(CLEARINGHOUSE_URL + path, headers=bearer)
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


async def test_the_payments_check_passes_there_and_asks_the_signer_at_its_own_address(ports: Ports) -> None:
    """The clearinghouse's engine is asked for clearinghouse routes only. The signer probe goes to
    ``LPB_SIGNER_URL``: a signer answers 405 to a GET of its identity path."""
    settings = EngineSettings.model_validate(
        {
            "role": "work",
            "clearinghouse_url": CLEARINGHOUSE_URL,
            "clearinghouse_token": ports.token,
            "clearinghouse_tenant_id": ports.tenant_id,
            "signer_url": "http://signer.test:8935",
        }
    )
    at_signer: list[httpx.Request] = []

    def signer(request: httpx.Request) -> httpx.Response:
        at_signer.append(request)
        return httpx.Response(405, headers={"Allow": "POST"})

    mounts = {
        "all://clearinghouse.test": client_of(ports)._transport,
        "all://signer.test": httpx.MockTransport(signer),
    }
    before = len(ports.requests)
    async with httpx.AsyncClient(mounts=mounts) as client:
        report = await probe_payments(settings, client=client)
    assert report.ok, report
    assert [probe.name for probe in report.probes] == [
        "clearinghouse_grant",
        "clearinghouse_usage",
        "signer",
        "clearinghouse_runner_list",
    ]
    assert report.tenant is not None and report.tenant.tenant_id == ports.tenant_id
    asked = ports.requests[before:]
    assert {request.method for request in asked} == {"GET"} and len(asked) == 3
    assert all(request.url.path.startswith("/v1/clearinghouse/") for request in asked)
    [identity] = at_signer
    assert (identity.method, identity.url.path) == ("GET", "/sign-orchestrator-info")
    assert "authorization" not in identity.headers
