"""#166: the tenant list carries each tenant's payment usage, as what its allocations have spent.

Batteries serves no figure for a grant's usage, and reading the usage feed for every tenant on every list
would be unbounded. It does serve ``spent_<currency>`` on each allocation, and the tenant list and the
grant read already read the allocations for the unallocated balance (#165). So ``spent`` is the sum of
those, at no further request: every payment charged to the tenant's allocations, the revoked ones
included (Batteries keeps ``spent_*`` on a revoked allocation, late charges too).
"""

from decimal import Decimal

import httpx
import pytest

from livepeer_builder.contracts import ActorContext, AllotRequest, Money
from livepeer_builder.errors import ProviderError
from livepeer_builder.http import create_app
from livepeer_builder.testing import FakeAllocationManager
from tests.conftest import EngineFactory
from tests.unit.test_batteries_grants import Batteries, allocation, grant, usd


def spending(allocation_id: str, grant_id: str, allocated: str, spent: str, currency: str = "usd") -> dict:  # type: ignore[type-arg]
    return allocation(allocation_id, grant_id, allocated, currency) | {f"spent_{currency}": spent}


async def test_a_tenants_spent_is_what_its_allocations_have_spent_at_no_further_request() -> None:
    batteries = Batteries(
        [grant("g1", "100"), grant("g2", "7"), grant("g3", "1.5", currency="eth")],
        [spending("a1", "g1", "60", "1.25"), spending("a2", "g2", "7", "0.5"), spending("a3", "g1", "10", "0.75"),
         spending("a4", "g3", "1", "0.000000000000000007", "eth")],
    )  # fmt: skip
    listed = await batteries.client().grant_balances()
    assert [(tenant.tenant_id, tenant.spent) for tenant in listed] == [
        ("g1", usd("2.00")),
        ("g2", usd("0.5")),
        ("g3", Money(amount=Decimal("0.000000000000000007"), currency="ETH")),
    ]
    assert [path for _, path, _ in batteries.requests] == [
        "/v1/grants", "/v1/grants", "/v1/allocations", "/v1/allocations"
    ]  # fmt: skip  # the requests the balance already made (#165): none is added

    batteries.requests.clear()
    one = await batteries.client().read_grant("g1")
    assert (one.spent, one.unallocated) == (usd("2.00"), usd("30"))
    assert [path for _, path, _ in batteries.requests] == ["/v1/grants/g1", "/v1/allocations"]


async def test_a_tenant_with_no_allocation_has_spent_nothing() -> None:
    batteries = Batteries([grant("g1", "5")], [])
    assert (await batteries.client().read_grant("g1")).spent == usd("0")
    assert [tenant.spent for tenant in await batteries.client().grant_balances()] == [usd("0")]


@pytest.mark.parametrize("spent", ["[error]", "", "NaN", None, 5], ids=str)
async def test_a_spent_figure_the_engine_cannot_read_is_never_a_zero(spent: object) -> None:
    batteries = Batteries([grant("g1", "5")], [allocation("a1", "g1", "1") | {"spent_usd": spent}])
    with pytest.raises(ProviderError):
        await batteries.client().read_grant("g1")
    with pytest.raises(ProviderError):
        await batteries.client().grant_balances()


async def test_the_tenant_list_and_the_grant_read_show_what_each_tenant_spent(
    make_engine: EngineFactory, manager: FakeAllocationManager, admin: ActorContext
) -> None:
    engine = await make_engine()
    manager.add_grant("other", total=50)
    for tenant, actor in (("default", "alice"), ("default", "bob"), ("other", "carol")):
        request = AllotRequest(
            tenant_id=tenant, actor_id=actor, amount=usd("5"), operation_ref=f"allot-{tenant}-{actor}"
        )
        allotted = await engine.payments.allot(admin, request)
        manager.allocations[allotted.allocation_id].spent = Decimal("1.25")
    await engine.payments.revoke(admin, "default", "bob")  # what a revoked allocation spent stays spent

    assert engine.keys is not None
    key = {"Authorization": f"Bearer {(await engine.keys.bootstrap_admin()).token}"}
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        listed = (await client.get("/v1/clearinghouse/tenants", headers=key)).json()
        assert {tenant["tenant_id"]: tenant["spent"] for tenant in listed} == {
            "default": {"amount": "2.50", "currency": "USD"},
            "other": {"amount": "1.25", "currency": "USD"},
        }
        one = (await client.get("/v1/clearinghouse/tenants/other/grant", headers=key)).json()
        assert one["spent"] == {"amount": "1.25", "currency": "USD"}
        assert one["unallocated"] == {"amount": "45", "currency": "USD"}
