"""#168: an enterprise's engine signs at its clearinghouse, with the actor's payment key.

A clearinghouse serves the signer's two paths at the root of its listener (D87 point 3, as amended on
19 November). So an engine that uses one signs with the transport every engine signs with, the gateway
SDK's, pointed at the clearinghouse where a signer's address goes, and with the actor's payment key
where a Batteries key goes. The SDK is unmodified.

Two credentials leave such an engine, each for its own routes and never the other's: the payment key of
the job's actor, on the two signer paths, and the tenant admin key, on the tenant's routes. A Batteries
key never leaves a clearinghouse, so the engine holds none.

An engine that uses a clearinghouse is not one: it forwards no signer call for anybody and reports no
wallet, although its transport could do both.
"""

import socket
from collections.abc import AsyncIterator

import pytest
from fastapi.routing import APIRoute

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.adapters.signer_forward import HttpSignerForwarder
from livepeer_builder.contracts import AllotRequest, JobRequest
from livepeer_builder.errors import JobFailed
from livepeer_builder.http import create_app
from livepeer_builder.protocols import SignerForwarder, SignerProbe
from livepeer_builder.testing import FakeAllocationManager, FakeSigningTransport, MemorySecretStore
from tests.unit.clearinghouse_engine import TOKEN, parts_of, self_hosted, with_clearinghouse
from tests.unit.livepeer_net import Net, start
from tests.unit.paying_ports import FIXED_APP, open_paying_ports
from tests.unit.payment_ports import Ports, usd

SIGNER_PATHS = {"/sign-orchestrator-info", "/generate-live-payment"}
WALLET = "/v1/clearinghouse/wallet"


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        yield value


@pytest.fixture
async def ports(net: Net) -> AsyncIterator[Ports]:
    async with open_paying_ports("remote", net) as opened:
        yield opened


async def allotted(ports: Ports, actor_id: str = "alice") -> str:
    request = AllotRequest(
        tenant_id=ports.tenant_id, actor_id=actor_id, amount=usd(5), operation_ref=f"allot-{actor_id}"
    )
    return (await ports.engine.payments.allot(ports.admin, request)).allocation_id


def bearers(ports: Ports, *, signer: bool) -> set[str | None]:
    """The ``Authorization`` of each request that arrived at the clearinghouse on a signer path, or on
    any other."""
    return {headers.get("Authorization") for path, headers in ports.seen if (path in SIGNER_PATHS) is signer}


async def payment_key(ports: Ports, actor_id: str = "alice") -> str:
    key = await ports.credentials.credential(ports.actor(actor_id))
    assert key.token is not None and key.token.startswith("lpp_")
    return key.token


# -- the transport ------------------------------------------------------------------------------------------


async def test_an_engine_that_uses_a_clearinghouse_signs_with_the_sdk_transport_pointed_at_it() -> None:
    async with parts_of(with_clearinghouse("http://clearinghouse.internal:8080")) as parts:
        transport = parts.transport
        assert type(transport) is SdkRunnerTransport and transport.pays
        assert transport._signer_url == "http://clearinghouse.internal:8080"
        assert TOKEN not in repr(transport) + str(vars(transport))  # the tenant admin key is not its to hold
    async with parts_of(self_hosted(signer_url="http://127.0.0.1:9")) as parts:
        assert (
            type(parts.transport) is SdkRunnerTransport
            and parts.transport._signer_url == "http://127.0.0.1:9"
        )


# -- which credential travels where -------------------------------------------------------------------------


async def test_the_payment_key_travels_on_the_signer_calls_and_the_tenant_admin_key_on_the_rest(
    ports: Ports, net: Net
) -> None:
    await allotted(ports)
    key = await payment_key(ports)

    result = await ports.engine.jobs.run(ports.actor("alice"), JobRequest(app=FIXED_APP, payload={}))

    assert result.job.state == "succeeded" and result.job.attempts[0].payment_sent
    paths = [path for path, _ in ports.seen]
    assert paths.count("/sign-orchestrator-info") == 1 and paths.count("/generate-live-payment") == 1
    # the signer's two paths were shown the actor's payment key, and nothing else
    assert bearers(ports, signer=True) == {f"Bearer {key}"}
    # every other request carried the tenant admin key, and never a payment key
    assert bearers(ports, signer=False) == {f"Bearer {ports.token}"}
    crossed = " ".join(f"{name}: {value}" for _, headers in ports.seen for name, value in headers.items())
    assert crossed.count(ports.token) == len(paths) - 2 and crossed.count(key) == 2
    # a Batteries key never leaves the clearinghouse: none crossed, and the engine holds none
    assert "lpg_" not in crossed
    secrets = ports.engine.payments._secrets
    assert not isinstance(secrets, MemorySecretStore)  # the engine's own encrypted store
    assert (await ports.credentials.credential(ports.actor("alice"))).token == key
    # the runner was shown neither
    to_the_runner = " ".join(f"{name}: {value}" for headers in net.headers for name, value in headers.items())
    assert key not in to_the_runner and ports.token not in to_the_runner


async def test_each_actor_job_is_signed_with_that_actor_payment_key(ports: Ports, net: Net) -> None:
    await allotted(ports, "alice")
    await allotted(ports, "bob")
    keys = {name: await payment_key(ports, name) for name in ("alice", "bob")}
    assert keys["alice"] != keys["bob"]
    for name in ("alice", "bob"):
        ports.seen.clear()
        await ports.engine.jobs.run(ports.actor(name), JobRequest(app=FIXED_APP, payload={}))
        assert bearers(ports, signer=True) == {f"Bearer {keys[name]}"}


async def test_a_rotated_payment_key_is_picked_up_for_the_next_job(ports: Ports, net: Net) -> None:
    await allotted(ports)
    old = await payment_key(ports)
    alice = ports.actor("alice")
    await ports.engine.jobs.run(alice, JobRequest(app=FIXED_APP, payload={}))
    assert bearers(ports, signer=True) == {f"Bearer {old}"}

    await ports.engine.payments.rotate_payment_key(ports.admin, ports.tenant_id, "alice", "rot-1")
    new = await payment_key(ports)
    ports.seen.clear()
    second = await ports.engine.jobs.run(alice, JobRequest(app=FIXED_APP, payload={}))

    assert new != old and second.job.state == "succeeded" and second.job.attempts[0].payment_sent
    # the next job signs with the new key, from its first signer call on: the old one is not sent again
    assert bearers(ports, signer=True) == {f"Bearer {new}"}
    assert await ports.pays_for(old) is None


# -- when the clearinghouse's signer fails (D87 point 7) ----------------------------------------------------


def unused_address() -> str:
    """A loopback address nothing listens on: a signer that is down."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{probe.getsockname()[1]}"


def signer_of(ports: Ports) -> SdkRunnerTransport:
    """The clearinghouse's own transport: the part that forwards to its signer."""
    assert ports.clearinghouse is not None
    transport = ports.clearinghouse.jobs._transport
    assert isinstance(transport, SdkRunnerTransport)
    return transport


async def test_a_clearinghouse_that_cannot_reach_its_signer_fails_the_job_with_nothing_paid_and_the_next_pays(
    ports: Ports, net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine remembers who the signer is, so a later job goes straight to signing. The
    clearinghouse answers that it sent its signer nothing, and the job fails ``payment`` /
    ``signer_unavailable``: failed, not uncertain, with the manifest recorded as unpaid. Retrying is
    safe, and the next job pays once the signer is back."""
    await allotted(ports)
    alice = ports.actor("alice")
    await ports.engine.jobs.run(alice, JobRequest(app=FIXED_APP, payload={}))
    working = signer_of(ports)._forwarder

    monkeypatch.setattr(signer_of(ports), "_forwarder", HttpSignerForwarder(unused_address()))
    with pytest.raises(JobFailed) as failed:
        await ports.engine.jobs.run(alice, JobRequest(app=FIXED_APP, payload={}))

    job = failed.value.job
    assert job.state == "failed" and job.failure is not None
    assert (job.failure.kind, job.failure.reason, job.failure.status_code) == (
        "payment",
        "signer_unavailable",
        503,
    )
    assert not job.failure.payment_sent and "nothing was signed" in job.failure.message
    [attempt] = job.attempts
    assert not attempt.payment_sent and [session.payment_state for session in attempt.sessions] == ["unpaid"]
    assert net.log.count("sign m-2") == 0  # the signer never heard of it

    monkeypatch.setattr(signer_of(ports), "_forwarder", working)
    again = await ports.engine.jobs.run(alice, JobRequest(app=FIXED_APP, payload={}))
    assert again.job.state == "succeeded" and again.job.attempts[0].payment_sent


async def test_a_signer_call_that_left_the_clearinghouse_and_got_no_answer_leaves_the_job_uncertain(
    ports: Ports, net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clearinghouse handed the call to its signer and no answer came inside its deadline: it may
    have signed. The job is ``uncertain`` and nobody else is paid."""
    await allotted(ports)
    alice = ports.actor("alice")
    await ports.engine.jobs.run(alice, JobRequest(app=FIXED_APP, payload={}))

    monkeypatch.setattr(signer_of(ports), "_forwarder", HttpSignerForwarder(net.url, deadline_s=0.2))
    net.sign = [5.0]
    with pytest.raises(JobFailed) as failed:
        await ports.engine.jobs.run(alice, JobRequest(app=FIXED_APP, payload={}))

    job = failed.value.job
    assert job.state == "uncertain" and job.failure is not None
    assert (job.failure.kind, job.failure.reason, job.failure.status_code) == (
        "payment_unknown",
        "signer_unknown",
        502,
    )
    assert [session.payment_state for session in job.attempts[0].sessions] == ["unknown"]
    assert net.log.count("sign m-2") == 1  # handed over once


# -- an engine that uses a clearinghouse is not one ---------------------------------------------------------


async def test_an_engine_that_uses_a_clearinghouse_forwards_no_signer_call_and_reports_no_wallet(
    ports: Ports,
) -> None:
    """Its transport could do both: the SDK's transport answers a signer probe and forwards a signer
    call. An enterprise's engine is nobody's clearinghouse, so neither is offered, and neither route is
    served. (Which routes each role serves is the role setting's, #169.)"""
    engine = ports.engine
    transport = engine.jobs._transport
    assert isinstance(transport, SignerForwarder) and isinstance(transport, SignerProbe)
    assert engine.payments.signing is None and engine.payments.wallet is None
    served = {
        route.path
        for route in create_app(engine, manage_lifecycle=False).routes
        if isinstance(route, APIRoute)
    }
    assert not served & (SIGNER_PATHS | {WALLET})
    # the clearinghouse it uses serves the two signer paths
    house = ports.clearinghouse
    assert house is not None and house.payments.signing is not None


async def test_a_supplied_part_that_could_report_a_wallet_or_forward_does_not_make_it_a_clearinghouse() -> (
    None
):
    """Whatever parts it is given: a grants manager that reports an escrow and a transport that forwards
    signer calls are what a clearinghouse's wallet report and signer paths are built from."""
    supply = EngineSupply(grants_manager=FakeAllocationManager(), transport=FakeSigningTransport())
    engine = await BuilderEngine.create(with_clearinghouse(), supply=supply)
    try:
        assert engine.payments.wallet is None and engine.payments.signing is None
    finally:
        await engine.stop()
    self_hosted_engine = await BuilderEngine.create(self_hosted(), supply=supply)
    try:  # the same parts on a self-hosted engine are a clearinghouse's
        assert self_hosted_engine.payments.wallet is not None
        assert self_hosted_engine.payments.signing is not None
    finally:
        await self_hosted_engine.stop()
