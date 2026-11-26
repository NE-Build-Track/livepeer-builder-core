"""ISS-26 on both sets of payment adapters (D84, #168): a queued job pays at dispatch, never at submit.

Each test runs twice, as the signing section of ``tests/unit/test_payment_port_suite.py`` does: ``local``
is a self-hosted engine (its ``both`` role), and ``remote`` an enterprise's engine in its ``work`` role,
whose payment ports call a standalone clearinghouse on a loopback socket. Either engine signs at the
signer itself, with the allocation's own key: the one its own Batteries call minted, or the one the
clearinghouse handed over at allotment. The signer and the runner are ``tests/unit/livepeer_net.py``. A
queued job must behave the same on either engine (the guide's amendment of 18 November 2026): with a
clearinghouse, ``not_provisioned`` means the actor has no allocation there.
"""

from decimal import Decimal

from livepeer_builder.contracts import JobRequest
from tests.unit.livepeer_net import Net
from tests.unit.paying_ports import FIXED_APP
from tests.unit.payment_ports import Ports
from tests.unit.test_payment_port_suite import allotted, batteries_key, cost_of, net, paying

__all__ = ["net", "paying"]  # the fixtures, shared with the port suite
SIGNER_PATHS = {"/sign-orchestrator-info", "/generate-live-payment"}


def queued(payload: dict[str, str]) -> JobRequest:
    return JobRequest(app=FIXED_APP, mode="async", payload=payload)


def signer_calls(ports: Ports, net: Net) -> list[str]:
    """Every signer call made so far: at the signer, and any that reached the clearinghouse's listener,
    which serves no signer path and is in no payment's path."""
    at_the_clearinghouse = [path for path, _ in ports.seen if path in SIGNER_PATHS]
    return [*(line for line in net.log if line.startswith("sign")), *at_the_clearinghouse]


async def test_a_queued_job_is_paid_when_it_is_dispatched_through_each_set_of_adapters(
    paying: Ports, net: Net
) -> None:
    ports, alice = paying, paying.actor("alice")
    allocation_id = await allotted(ports)

    job = await ports.engine.jobs.submit(alice, queued({"prompt": "hello"}))
    assert job.state == "queued"
    assert net.log == [] and signer_calls(ports, net) == []  # nothing signed or sent while it waits

    assert await ports.engine.jobs.worker.run_once() == 1
    result = await ports.engine.jobs.result(alice, job.id)
    [attempt] = result.job.attempts
    assert (result.job.state, attempt.payment_sent, attempt.allocation_id) == (
        "succeeded",
        True,
        allocation_id,
    )
    assert [session.manifest_id for session in attempt.sessions] == ["m-1"]
    assert net.log.count("sign m-1") == 1 and "request pay-m-1" in net.log
    assert {headers.get("Authorization") for headers in net.signer_headers} == {
        f"Bearer {batteries_key(ports, allocation_id)}"
    }
    cost = await cost_of(ports, job.id, allocation_id, "m-1", "0.25")
    assert cost.status == "observed" and cost.fees.fee_usd == Decimal("0.25")
    assert [attempt_cost.cost_class for attempt_cost in cost.attempts] == ["delivered"]


async def test_an_allocation_spent_while_the_job_waited_fails_it_unpaid(paying: Ports, net: Net) -> None:
    ports, alice = paying, paying.actor("alice")
    await allotted(ports)
    job = await ports.engine.jobs.submit(alice, queued({}))
    net.sign = [402]  # the signer's answer once the job is dispatched: the allocation is spent

    assert await ports.engine.jobs.worker.run_once() == 1
    ended = await ports.engine.jobs.get(alice, job.id)
    assert ended.state == "failed" and ended.failure is not None
    assert (ended.failure.kind, ended.failure.reason, ended.failure.payment_sent) == (
        "payment",
        "allowance_exhausted",
        False,
    )
    assert [attempt.payment_sent for attempt in ended.attempts] == [False]
    assert "request pay-m-1" not in net.log  # the runner did no work


async def test_an_allocation_revoked_while_the_job_waited_fails_it_before_any_payment(
    paying: Ports, net: Net
) -> None:
    ports, alice = paying, paying.actor("alice")
    await allotted(ports)
    job = await ports.engine.jobs.submit(alice, queued({}))
    await ports.engine.payments.revoke(ports.admin, ports.tenant_id, "alice")

    assert await ports.engine.jobs.worker.run_once() == 1
    ended = await ports.engine.jobs.get(alice, job.id)
    assert ended.state == "failed" and ended.failure is not None
    assert (ended.failure.kind, ended.failure.payment_sent, ended.attempts) == ("not_provisioned", False, ())
    assert net.log == [] and signer_calls(ports, net) == []
    assert (await ports.engine.costs.for_job(alice, job.id)).status == "none"
