"""#169: what each role needs when it is built, now that every engine that pays signs at the signer
itself (D84 point 2, D85 point 1, D87 point 1).

| Rule | ``both`` | ``work`` | ``clearinghouse`` |
| --- | --- | --- | --- |
| A writable secret store (``LPB_SECRET_KEY``) | Needed: it stores the keys its Batteries mints | Needed: it stores the keys it is handed | Not needed: it stores none |
| A runner transport that pays | The SDK's, at ``LPB_SIGNER_URL`` | The SDK's, at ``LPB_SIGNER_URL`` | None: only the signer's identity call, for the wallet report |
| ``LPB_SIGNER_URL`` | Unset: its jobs run unpaid | Unset: its jobs run unpaid | Unset: the wallet report says so |
| Batteries, with the grants credential | As set | Refused | Needed (tests/unit/test_roles.py) |

No role is refused for a missing signer address. ``lpb payments check`` fails for it in every role
(tests/unit/test_payments_check.py, tests/unit/test_cli_roles.py).
"""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from livepeer_builder import BuilderEngine, EngineSettings, EngineSupply
from livepeer_builder.adapters.sdk import IdentityOnlyTransport, SdkRunnerTransport
from livepeer_builder.contracts import ActorContext, AllotRequest, JobRequest, Money, SignerReadiness
from livepeer_builder.errors import InvalidRequest
from livepeer_builder.http import create_app
from livepeer_builder.protocols import SignerProbe, StreamingRunnerTransport
from livepeer_builder.testing import FakeAllocationManager, FakeUsageSource, runner
from tests.unit.clearinghouse_engine import self_hosted, with_clearinghouse
from tests.unit.livepeer_net import Net, start
from tests.unit.role_engines import PRIVATE_PORT

OPERATOR = ActorContext(actor_id="operator", credential_kind="clearinghouse_admin")
BATTERIES = {
    "batteries_url": "http://batteries-not-to-be-shown:8081",
    "batteries_token": "bt_secret-not-to-be-shown",
    "batteries_grants_token": "gt_secret-not-to-be-shown",
}
ALLOCATION = "/v1/clearinghouse/tenants/default/actors/alice/allocation"


def a_clearinghouse(**values: Any) -> EngineSettings:
    """A clearinghouse from its settings alone. No address here is called."""
    base = {"role": "clearinghouse", "http_private_port": PRIVATE_PORT, **BATTERIES}
    return self_hosted(**{**base, **values})


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        yield value


def usd(amount: str) -> Money:
    return Money.model_validate({"amount": amount, "currency": "USD"})


# -- the secret store: needed where the engine itself stores allocation keys ------------------------------


@pytest.mark.parametrize(
    "storing",
    [
        pytest.param(self_hosted(secret_key=None, **BATTERIES), id="both"),
        pytest.param(with_clearinghouse(secret_key=None), id="work"),
    ],
)
async def test_an_engine_that_stores_allocation_keys_does_not_start_without_a_secret_key(
    storing: EngineSettings,
) -> None:
    """Allotment would fund an allocation and then fail to keep its key (#112, C2). An engine in its
    ``both`` role stores the keys its own Batteries mints, and one in its ``work`` role the keys its
    clearinghouse hands over."""
    with pytest.raises(ValueError, match="LPB_SECRET_KEY is required") as refused:
        await BuilderEngine.create(storing)
    assert "not-to-be-shown" not in str(refused.value)


async def test_a_clearinghouse_stores_no_allocation_key_and_starts_without_a_secret_key() -> None:
    engine = await BuilderEngine.create(a_clearinghouse(secret_key=None))
    try:
        assert engine.role == "clearinghouse" and engine.payments.tenants is not None
    finally:
        await engine.stop()


async def test_a_clearinghouse_without_a_secret_key_hands_keys_over_and_refuses_to_store_one() -> None:
    """Its routes hand each key to the caller and ask nothing of the secret store. The storing command,
    which only an embedding caller reaches in this role, is refused before the provider is called, as on
    any engine whose store cannot write: nothing is allotted that nobody holds the key of."""
    provider = FakeAllocationManager()
    provider.add_grant("default")
    supply = EngineSupply(
        allocation_manager=provider, grants_manager=provider, usage_source=FakeUsageSource()
    )
    engine = await BuilderEngine.create(a_clearinghouse(secret_key=None), supply=supply)
    try:
        assert engine.keys is not None
        admin = {"Authorization": f"Bearer {(await engine.keys.bootstrap_admin()).token}"}
        app = create_app(engine, manage_lifecycle=False, listener="private")
        body = {"amount": {"amount": "5", "currency": "USD"}, "operation_ref": "allot-alice"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://engine"
        ) as client:
            allotted = await client.post(ALLOCATION, json=body, headers=admin)
        assert allotted.status_code == 201, allotted.text
        [(_, minted, _)] = provider.keys.values()
        assert allotted.json()["allocation_api_key"] == minted

        calls = list(provider.calls)
        storing = AllotRequest(
            tenant_id="default", actor_id="bob", amount=usd("5"), operation_ref="allot-bob"
        )
        with pytest.raises(InvalidRequest, match="nothing was sent to the payment provider"):
            await engine.payments.allot(OPERATOR, storing)
        assert provider.calls == calls and len(provider.allocations) == 1
    finally:
        await engine.stop()


# -- the clearinghouse role and the signer: it signs nothing, and reports whether the signer answers -------


async def test_a_clearinghouse_is_given_no_transport_that_could_pay() -> None:
    """It never runs a job (D82), so its default transport is not the SDK's paying one: it can ask the
    signer's identity, for the wallet report, and it calls no runner and no signing path."""
    engine = await BuilderEngine.create(a_clearinghouse(signer_url="http://signer-not-to-be-shown:8937"))
    try:
        transport = engine.jobs._transport
        assert isinstance(transport, IdentityOnlyTransport)
        assert not isinstance(transport, SdkRunnerTransport)
        assert not isinstance(transport, StreamingRunnerTransport)  # it opens no stream either
        assert isinstance(transport, SignerProbe) and transport.pays is False
        assert engine.jobs.streaming is None and engine.payments.wallet is not None
        with pytest.raises(InvalidRequest, match="clearinghouse role") as refused:
            await transport.call(runner(), JobRequest(app="example/echo"), "lpg_a-key-not-to-be-sent")
        assert "not-to-be-s" not in str(refused.value)
    finally:
        await engine.stop()


@pytest.mark.parametrize("role", ["both", "work"])
async def test_an_engine_that_runs_jobs_keeps_the_sdks_paying_transport(role: str) -> None:
    signer = "http://127.0.0.1:9"
    given = self_hosted(signer_url=signer) if role == "both" else with_clearinghouse(signer_url=signer)
    engine = await BuilderEngine.create(given)
    try:
        transport = engine.jobs._transport
        assert engine.role == role and isinstance(transport, SdkRunnerTransport) and transport.pays
    finally:
        await engine.stop()


async def wallet_signer(engine: BuilderEngine) -> SignerReadiness:
    assert engine.payments.wallet is not None
    return (await engine.payments.wallet.report(OPERATOR)).signer


def reporting_supply() -> EngineSupply:
    """Batteries' stand-in, so the wallet report has an escrow and grants to read."""
    provider = FakeAllocationManager()
    provider.add_grant("default")
    return EngineSupply(allocation_manager=provider, grants_manager=provider, usage_source=FakeUsageSource())


async def test_a_clearinghouses_wallet_report_asks_the_signer_at_lpb_signer_url_and_signs_nothing(
    net: Net,
) -> None:
    engine = await BuilderEngine.create(a_clearinghouse(signer_url=net.url), supply=reporting_supply())
    try:
        assert await wallet_signer(engine) == SignerReadiness(ready=True)
        net.info = [503]
        assert await wallet_signer(engine) == SignerReadiness(ready=False, reason="unavailable")
        # the identity call, twice, with no credential; no payment was asked for
        assert net.log == ["signer info"] * 2 and net.signer_requests == []
        assert all("Authorization" not in headers for headers in net.signer_headers)
    finally:
        await engine.stop()


async def test_a_clearinghouse_without_a_signer_address_starts_and_its_wallet_report_says_so() -> None:
    """It signs nothing, and its tenants' engines reach the signer at its published address, not through
    this engine: no rule asks for ``LPB_SIGNER_URL``. Without it the wallet report cannot say whether
    the signer answers, and says that."""
    engine = await BuilderEngine.create(a_clearinghouse(signer_url=None), supply=reporting_supply())
    try:
        assert engine.role == "clearinghouse"
        assert await wallet_signer(engine) == SignerReadiness(ready=False, reason="not_configured")
        await engine.start()  # and it starts
        assert engine.health().ready
    finally:
        await engine.stop()


# -- the work role and the signer: unset, its jobs run unpaid, as on any engine -----------------------------


async def test_a_work_engine_without_a_signer_address_starts_and_does_not_pay() -> None:
    """No start-up rule asks an engine in its ``work`` role for ``LPB_SIGNER_URL``: unset, it is an
    engine that does not pay, as a self-hosted one is. ``lpb payments check`` says so and fails
    (tests/unit/test_payments_check.py)."""
    engine = await BuilderEngine.create(with_clearinghouse(signer_url=None))
    try:
        transport = engine.jobs._transport
        assert engine.role == "work" and isinstance(transport, SdkRunnerTransport)
        assert transport.pays is False
    finally:
        await engine.stop()
