"""#168: the signing seam. Signing through a clearinghouse is not built yet, so an engine that uses one
signs nothing.

Signing happens inside the runner transport. Until the payments route exists (#166), an engine that uses
a clearinghouse runs a transport that holds no signer: a runner that asks for no payment is called, and
one that asks for payment fails the job plainly, as a payment failure with nothing paid. There is no
fallback to a local signer, and the actor's payment key is sent nowhere.
"""

import logging
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.adapters.clearinghouse_signing import SIGNING_NOT_BUILT, ClearinghouseSigningNotBuilt
from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.contracts import ActorContext, AllotRequest, JobRequest, Money
from livepeer_builder.errors import JobFailed, RunnerCallError
from livepeer_builder.protocols import SignerProbe, StreamingRunnerTransport
from livepeer_builder.testing import (
    FakeAllocationManager,
    serve,
)
from tests.unit.clearinghouse_engine import TOKEN, parts_of, self_hosted, with_clearinghouse
from tests.unit.livepeer_net import Net, Reply, start

PAYMENT_KEY = "lpp_an-actor-payment-key"
OPERATOR = ActorContext(actor_id="operator", credential_kind="clearinghouse_admin")
ALICE = ActorContext(
    actor_id="alice", tenant_id="grant_7", permissions=frozenset({"discover", "jobs.run", "jobs.read"})
)


async def test_an_engine_that_uses_a_clearinghouse_runs_the_seam_and_has_no_signer_to_ask() -> None:
    async with parts_of(with_clearinghouse()) as parts:
        assert isinstance(parts.transport, ClearinghouseSigningNotBuilt)
        assert not isinstance(parts.transport, SignerProbe)
    async with parts_of(self_hosted(signer_url="http://127.0.0.1:9")) as parts:
        assert isinstance(parts.transport, SdkRunnerTransport) and parts.transport.pays


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        yield value


def nothing_reached_a_signer_and_the_payment_key_went_nowhere(net: Net) -> bool:
    crossed = " ".join(f"{name}: {value}" for headers in net.headers for name, value in headers.items())
    return not net.signer_requests and "signer info" not in net.log and PAYMENT_KEY not in crossed


async def test_a_call_that_needs_a_payment_fails_plainly_and_signs_nothing(net: Net) -> None:
    transport = ClearinghouseSigningNotBuilt()
    with pytest.raises(RunnerCallError) as failed:
        await transport.call(net.runner(), JobRequest(app="example/echo", payload={}), PAYMENT_KEY)
    error = failed.value
    assert (error.kind, error.status_code, error.payment_sent) == ("payment", 402, False)
    assert not error.payment_unknown and error.manifest_ids == ()
    assert str(error) == SIGNING_NOT_BUILT and "clearinghouse" in SIGNING_NOT_BUILT
    assert len(net.headers) == 1  # the one unpaid request, which the runner answered 402
    assert nothing_reached_a_signer_and_the_payment_key_went_nowhere(net)


async def test_a_stream_that_needs_a_payment_fails_the_same_way(net: Net) -> None:
    transport = ClearinghouseSigningNotBuilt()
    assert isinstance(transport, StreamingRunnerTransport)
    with pytest.raises(RunnerCallError) as failed:
        await transport.open(
            net.runner(unit="seconds"), JobRequest(app="example/echo", payload={}), PAYMENT_KEY
        )
    assert (failed.value.kind, failed.value.payment_sent) == ("payment", False)
    assert nothing_reached_a_signer_and_the_payment_key_went_nowhere(net)


async def test_work_that_asks_for_no_payment_still_runs(net: Net) -> None:
    net.paid = False
    transport = ClearinghouseSigningNotBuilt()
    assert transport.pays is False and not isinstance(transport, SignerProbe)
    reply = await transport.call(net.runner(), JobRequest(app="example/echo", payload={}), PAYMENT_KEY)
    assert reply.status_code == 200 and not reply.payment_sent
    stream = await transport.open(net.runner(), JobRequest(app="example/echo", payload={}), PAYMENT_KEY)
    assert stream.status_code == 200 and not stream.payment_sent
    await stream.aclose()
    assert nothing_reached_a_signer_and_the_payment_key_went_nowhere(net)


async def test_any_other_failure_of_the_runner_is_passed_on_as_it_is(net: Net) -> None:
    net.paid = False
    net.replies.append(Reply(status=500, body=b"boom"))
    with pytest.raises(RunnerCallError) as failed:
        await ClearinghouseSigningNotBuilt().call(
            net.runner(), JobRequest(app="example/echo", payload={}), PAYMENT_KEY
        )
    assert (failed.value.kind, failed.value.status_code) == ("http", 500)


async def test_a_job_that_needs_a_payment_fails_as_a_payment_failure_with_nothing_paid(
    net: Net, caplog: pytest.LogCaptureFixture
) -> None:
    """The whole engine, built from the settings: an allotted actor's job reaches a runner that asks for
    payment. No local signer is fallen back to, and nothing that could charge is called."""
    caplog.set_level(logging.DEBUG)
    install, client = serve((net.runner(),))
    given = with_clearinghouse()
    manager = FakeAllocationManager()
    manager.add_grant("grant_7")
    supply = EngineSupply(allocation_manager=manager, credentials=None, allowances=None)
    engine = await BuilderEngine.create(given, supply=supply, decorate=install)
    try:
        assert isinstance(engine.jobs._transport, ClearinghouseSigningNotBuilt)
        await engine.discovery.refresh()
        await allot(engine, ALICE)
        with pytest.raises(JobFailed) as failed:
            await engine.jobs.run(ALICE, JobRequest(app="example/echo", payload={"n": 1}))
    finally:
        await client.aclose()
        await engine.stop()
    job = failed.value.job
    assert job.failure is not None and job.state == "failed"
    assert (job.failure.kind, job.failure.payment_sent) == ("payment", False)
    assert job.failure.message == SIGNING_NOT_BUILT
    assert [attempt.payment_sent for attempt in job.attempts] == [False]
    assert not net.signer_requests and "signer info" not in net.log
    assert TOKEN not in caplog.text


async def allot(engine: BuilderEngine, actor: ActorContext) -> None:
    request = AllotRequest(
        tenant_id=actor.tenant_id,
        actor_id=actor.actor_id,
        amount=Money(amount=Decimal(5), currency="USD"),
        operation_ref="allot-alice",
    )
    await engine.payments.allot(OPERATOR, request)
