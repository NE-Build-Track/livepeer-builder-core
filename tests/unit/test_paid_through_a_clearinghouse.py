"""#166: a fixed job and a live job, paid through a clearinghouse by the unmodified gateway SDK.

Three parties, each on a loopback socket, with the engine's real code between them:

- the **enterprise's side** is ``SdkRunnerTransport``, the transport an engine pays its jobs with today,
  given the clearinghouse's address where the signer's goes and an actor's payment key where the
  Batteries key goes. Nothing else of it changes, and the SDK it signs through is the pinned one;
- the **clearinghouse** is a real engine behind its REST app, with the two signer paths at the root;
- the **signer**, the runner and its orchestrator are ``tests/unit/livepeer_net.py``.

What these tests leave to #168: the engine around the transport (a job, its failure and its cost on an
engine whose payment ports all point at the clearinghouse), and classifying ``signer_not_reached``.
"""

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal

import pytest
from livepeer_gateway import remote_signer

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.adapters.signer_forward import HttpSignerForwarder
from livepeer_builder.contracts import ActorContext, AllotRequest, JobRequest, Money
from livepeer_builder.errors import RunnerCallError
from livepeer_builder.http import create_app
from livepeer_builder.testing import FakeAllocationManager
from tests.conftest import EngineFactory
from tests.unit.clearinghouse_net import Listening, listening
from tests.unit.livepeer_net import Net, Reply, start

APP = "example/echo"
SIGNER_ADDRESS = "0x" + "ab" * 20  # the wallet the loopback signer signs with


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        yield value


@dataclass
class Clearinghouse:
    engine: BuilderEngine
    served: Listening
    transport: SdkRunnerTransport  # the clearinghouse's own: the part that forwards to its signer
    manager: FakeAllocationManager
    admin: ActorContext

    async def allot(self, actor_id: str) -> tuple[str, str]:
        """An actor's payment key, which leaves the clearinghouse, and its Batteries key, which does not."""
        request = AllotRequest(
            tenant_id="default", actor_id=actor_id, amount=Money(amount=Decimal(5), currency="USD"),
            operation_ref=f"allot-{actor_id}",
        )  # fmt: skip
        allotted = await self.engine.payments.allot(self.admin, request)
        assert allotted.payment_key is not None and allotted.payment_key.token is not None
        return allotted.payment_key.token, self.manager.keys[allotted.api_key_id][1]

    def enterprise(self) -> SdkRunnerTransport:
        """Today's transport, pointed at the clearinghouse where it points at a signer."""
        return SdkRunnerTransport(self.served.url)


@pytest.fixture
async def clearinghouse(
    make_engine: EngineFactory, net: Net, manager: FakeAllocationManager, admin: ActorContext
) -> AsyncIterator[Clearinghouse]:
    transport = SdkRunnerTransport(net.url)  # its signer is the loopback one, which no one else reaches
    engine = await make_engine(supply=EngineSupply(transport=transport))
    async with listening(create_app(engine, manage_lifecycle=False)) as served:
        yield Clearinghouse(engine, served, transport, manager, admin)


def unused_address() -> str:
    """A loopback address nothing listens on: a signer that is down."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{probe.getsockname()[1]}"


def bearers(net: Net) -> set[str | None]:
    return {headers.get("Authorization") for headers in net.signer_headers}


# -- paid ----------------------------------------------------------------------------------------------------


async def test_a_fixed_job_is_paid_through_the_clearinghouse(clearinghouse: Clearinghouse, net: Net) -> None:
    payment_key, batteries_key = await clearinghouse.allot("alice")
    signed: list[str] = []

    async def on_signed(manifest_id: str) -> None:
        signed.append(manifest_id)

    reply = await clearinghouse.enterprise().call(
        net.runner(), JobRequest(app=APP, payload={"prompt": "kept from the clearinghouse"}), payment_key, on_signed
    )  # fmt: skip

    assert reply.status_code == 200 and reply.payment_sent and reply.manifest_ids == ("m-1",) == tuple(signed)
    # the signer was asked who it is, the runner asked for payment, the signer signed once, the runner ran
    assert net.log == ["signer info", "challenge m-1", "sign m-1", "request pay-m-1"]
    assert net.headers[0]["Livepeer-Payer-Address"] == SIGNER_ADDRESS  # the signer's identity, passed on
    assert net.signer_requests[0]["type"] == "fixed" and "state" not in net.signer_requests[0]

    # the enterprise presented the payment key to the clearinghouse, at the signer's two paths...
    assert [path for path, _ in clearinghouse.served.seen] == [
        "/sign-orchestrator-info",
        "/generate-live-payment",
    ]
    assert {headers["Authorization"] for _, headers in clearinghouse.served.seen} == {f"Bearer {payment_key}"}
    # ...and the signer saw the actor's Batteries key, and never the payment key
    assert bearers(net) == {f"Bearer {batteries_key}"}
    assert payment_key not in str(net.signer_headers) and payment_key not in str(net.signer_requests)
    # the clearinghouse saw the payment's terms and nothing of the job: the prompt went to the runner only
    assert "kept from the clearinghouse" not in json.dumps(net.signer_requests)
    assert net.bodies[-1] == {"prompt": "kept from the clearinghouse"}


async def test_a_live_job_is_funded_through_the_clearinghouse_cycle_after_cycle(
    clearinghouse: Clearinghouse, net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live work pays in cycles: the SDK asks for the next payment every ``PAYMENT_INTERVAL_S`` (3 s; a
    fiftieth of a second here) and sends the signer's ``state`` from the cycle before. Every cycle goes
    through the clearinghouse with the same payment key, and reaches the signer with the same Batteries
    key, which is what Batteries binds the payment session to."""
    monkeypatch.setattr(remote_signer, "PAYMENT_INTERVAL_S", 0.02)
    payment_key, batteries_key = await clearinghouse.allot("alice")
    net.replies = [Reply(hang_s=0.6)]  # the work takes a while: funding covers it

    reply = await clearinghouse.enterprise().call(
        net.runner(unit="seconds"), JobRequest(app=APP), payment_key
    )
    await asyncio.sleep(0.15)  # a cycle already on the wire when the work ended still lands

    assert reply.status_code == 200 and reply.manifest_ids == ("m-1",)
    cycles = net.log.count("sign m-1")
    assert cycles >= 3 and net.log.count("renewal m-1") >= 2  # the opening payment, then the cycles
    assert all(request["type"] == "live" for request in net.signer_requests)
    opening, *renewals = net.signer_requests
    assert "state" not in opening
    assert all(request["state"] == {"n": 1} for request in renewals)  # the signer's own, sent back whole
    assert bearers(net) == {f"Bearer {batteries_key}"}  # one key for every cycle of the session
    await asyncio.sleep(0.15)
    assert net.log.count("sign m-1") == cycles  # the funding stopped with the work
    paths = [path for path, _ in clearinghouse.served.seen]
    assert paths.count("/generate-live-payment") == cycles and paths.count("/sign-orchestrator-info") == 1


async def test_each_actors_job_is_paid_from_its_own_allocation(
    clearinghouse: Clearinghouse, net: Net
) -> None:
    alice, alice_batteries = await clearinghouse.allot("alice")
    bob, bob_batteries = await clearinghouse.allot("bob")
    enterprise = clearinghouse.enterprise()
    await enterprise.call(net.runner(), JobRequest(app=APP), alice)
    assert bearers(net) == {f"Bearer {alice_batteries}"}
    net.signer_headers.clear()
    await enterprise.call(net.runner(), JobRequest(app=APP), bob)
    assert bearers(net) == {f"Bearer {bob_batteries}"}


async def test_the_clearinghouse_calls_its_signer_directly_whatever_proxy_the_environment_names(
    clearinghouse: Clearinghouse, net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK's own client reads no proxy from the environment, so an engine's payments reach its signer
    directly. The calls a clearinghouse forwards carry a Batteries key to the same signer, and go the
    same way: never through a proxy a variable names."""
    for variable in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(variable, unused_address())
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    payment_key, batteries_key = await clearinghouse.allot("alice")
    reply = await clearinghouse.enterprise().call(net.runner(), JobRequest(app=APP), payment_key)
    assert reply.payment_sent and bearers(net) == {f"Bearer {batteries_key}"}


# -- refused, as with self-hosted payments ----------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 402, 403, 400, 481, 500, 502, 503])
async def test_a_signers_refusal_reads_through_the_clearinghouse_as_it_reads_from_the_signer(
    clearinghouse: Clearinghouse, net: Net, status: int
) -> None:
    """The same runner, the same signer answer, once with self-hosted payments (the transport calls the
    signer with the Batteries key) and once through the clearinghouse. The engine's own classification
    (``adapters/signer_failures.py``) reads both alike: an empty allocation is ``allowance_exhausted``
    (402), a paused one ``not_authorized`` (403), and a 5xx an unknown outcome."""
    payment_key, batteries_key = await clearinghouse.allot("alice")
    seen: list[tuple[str, str | None, int | None, bool, bool]] = []
    for transport, credential in ((SdkRunnerTransport(net.url), batteries_key), (clearinghouse.enterprise(), payment_key)):  # fmt: skip
        net.sign = [status]
        with pytest.raises(RunnerCallError) as refused:
            await transport.call(net.runner(), JobRequest(app=APP), credential)
        error = refused.value
        seen.append((error.kind, error.reason, error.status_code, error.payment_sent, error.payment_unknown))
    self_hosted, through_the_clearinghouse = seen
    assert through_the_clearinghouse == self_hosted
    expected = {
        401: ("payment", "not_authorized", False),
        402: ("payment", "allowance_exhausted", False),
        403: ("payment", "not_authorized", False),
        400: ("payment_refused", None, False),
        481: ("payment_refused", None, False),
    }.get(status, ("payment", None, True))  # a 5xx: the signer may have charged
    assert (self_hosted[0], self_hosted[1], self_hosted[4]) == expected


async def test_an_expired_challenge_is_refreshed_through_the_clearinghouse(
    clearinghouse: Clearinghouse, net: Net
) -> None:
    """The signer's 480 reaches the SDK as a 480: the engine asks the runner for a fresh challenge and
    signs that one."""
    payment_key, _ = await clearinghouse.allot("alice")
    net.sign = [480]
    reply = await clearinghouse.enterprise().call(net.runner(), JobRequest(app=APP), payment_key)
    assert reply.payment_sent and reply.manifest_ids == ("m-2",)
    assert [entry for entry in net.log if entry.startswith("sign m-")] == ["sign m-1", "sign m-2"]


async def test_a_key_that_is_no_live_payment_key_fails_before_any_runner_is_asked(
    clearinghouse: Clearinghouse, net: Net
) -> None:
    _, batteries_key = await clearinghouse.allot("alice")
    for credential in ("lpp_" + "x" * 43, batteries_key):
        with pytest.raises(RunnerCallError) as refused:
            await clearinghouse.enterprise().call(net.runner(), JobRequest(app=APP), credential)
        error = refused.value
        assert (error.kind, error.reason, error.status_code) == ("payment", "not_authorized", 401)
        assert not error.payment_sent and not error.payment_unknown
    assert net.log == []  # neither the runner nor the signer heard of it


# -- the clearinghouse's signer gives no answer -----------------------------------------------------------------


async def test_a_signer_that_is_down_fails_the_job_as_unavailable_with_nothing_signed(
    clearinghouse: Clearinghouse, net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every job begins with the signer's identity. The clearinghouse answers that call 503 when its
    signer cannot be reached, which the engine reads as ``signer_unavailable``: retry later."""
    payment_key, _ = await clearinghouse.allot("alice")
    monkeypatch.setattr(clearinghouse.transport, "_forwarder", HttpSignerForwarder(unused_address()))
    with pytest.raises(RunnerCallError) as failed:
        await clearinghouse.enterprise().call(net.runner(), JobRequest(app=APP), payment_key)
    error = failed.value
    assert (error.kind, error.reason, error.status_code) == ("payment", "signer_unavailable", 503)
    assert not error.payment_sent and not error.payment_unknown and net.log == []


async def test_a_signer_that_goes_down_between_two_jobs_is_told_apart_from_one_that_gave_no_answer(
    clearinghouse: Clearinghouse, net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK remembers a signer's identity for the life of the process, so a later job goes straight to
    signing. When the clearinghouse cannot reach its signer then, nothing was sent, and its answer says
    so: 503 with ``code: signer_not_reached``. When the call left and no answer came inside the
    clearinghouse's deadline, it says 502 with ``code: signer_outcome_unknown``.

    Today's classification reads both as an unknown outcome, the safe side: it knows a signer's 5xx only.
    The engine that uses a clearinghouse (#168) reads the code and retries after the first."""
    payment_key, _ = await clearinghouse.allot("alice")
    enterprise = clearinghouse.enterprise()
    assert (await enterprise.call(net.runner(), JobRequest(app=APP), payment_key)).payment_sent

    monkeypatch.setattr(clearinghouse.transport, "_forwarder", HttpSignerForwarder(unused_address()))
    with pytest.raises(RunnerCallError) as failed:
        await enterprise.call(net.runner(), JobRequest(app=APP), payment_key)
    not_reached = failed.value
    assert (not_reached.kind, not_reached.status_code, not_reached.payment_sent) == ("payment", 503, False)
    assert not_reached.payment_unknown  # today: any 5xx while signing
    assert json.loads(not_reached.__cause__.body)["code"] == "signer_not_reached"  # type: ignore[union-attr]
    assert net.log.count("sign m-2") == 0  # the signer never heard of the second job's payment

    monkeypatch.setattr(clearinghouse.transport, "_forwarder", HttpSignerForwarder(net.url, deadline_s=0.2))
    net.sign = [5.0]  # the signer takes the request and does not answer in time
    with pytest.raises(RunnerCallError) as failed:
        await enterprise.call(net.runner(), JobRequest(app=APP), payment_key)
    unknown = failed.value
    assert (unknown.kind, unknown.status_code, unknown.payment_unknown) == ("payment", 502, True)
    assert json.loads(unknown.__cause__.body)["code"] == "signer_outcome_unknown"  # type: ignore[union-attr]
    assert net.log.count("sign m-3") == 1  # handed over once, and not again
