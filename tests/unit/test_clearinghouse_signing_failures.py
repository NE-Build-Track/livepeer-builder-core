"""#168: what a failed signer call means to an engine that signs at a clearinghouse (D87 point 7).

A clearinghouse failure is classified like a signer failure: unreachable or not ready is ``retry``, an
unknown outcome is ``check``, and a refusal keeps its reason. A clearinghouse can say one thing a signer
cannot: that it could not reach its own signer, so **nothing was sent** (503, ``code:
signer_not_reached``). An engine that uses a clearinghouse reads that as ``payment`` /
``signer_unavailable``, with nothing paid, and a retry is safe. Everything else stays as it is with a
signer: a 5xx while signing may follow a signature, whoever made it, so it is an unknown outcome. When
in doubt the answer is "unknown", never "nothing was sent".

Each row here is produced by the real routes: a clearinghouse's engine behind its app on a loopback
socket, with its signer scripted (``FakeSigningTransport``), called by the gateway SDK's own client. The
rows for a proxy in front of a clearinghouse are a second server that answers by itself.

A self-hosted engine does not read the code. Its signer is livepeer/node's, which never sends one: what
answers with it at a signer's address is not the signer, and its word is no reason to pay twice.
"""

import json
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from livepeer_builder import BuilderEngine, EngineSupply
from livepeer_builder.adapters import signer_failures
from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.contracts import ActorContext, AllotRequest, JobRequest, Money, SignerAnswer
from livepeer_builder.errors import RunnerCallError, SignerNotReached, SignerOutcomeUnknown
from livepeer_builder.http import create_app, signing
from livepeer_builder.testing import FakeSigningTransport
from livepeer_builder.testing.signer import SIGNER_IDENTITY
from tests.conftest import EngineFactory
from tests.unit.clearinghouse_net import Listening, listening
from tests.unit.livepeer_net import Net, start

APP = "example/echo"
JSON = "application/json"
IDENTITY = SignerAnswer(status_code=200, content=SIGNER_IDENTITY, content_type=JSON)
Seen = tuple[str, str | None, int | None, bool, bool]  # kind, reason, status, payment_sent, payment_unknown


def refusal(status: int) -> SignerAnswer:
    """The signer's own answer with an error status, in its own shape: passed on as it is."""
    body = json.dumps({"error": {"message": "the signer said no"}}).encode()
    return SignerAnswer(status_code=status, content=body, content_type=JSON)


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        yield value


@dataclass
class House:
    engine: BuilderEngine
    signer: FakeSigningTransport  # the clearinghouse's signer, scripted
    served: Listening
    payment_key: str

    def enterprise(self, *, told: bool = True) -> SdkRunnerTransport:
        """The transport of an engine that uses this clearinghouse; ``told=False`` is a transport that
        was not told the address is a clearinghouse's, as a self-hosted engine's is not."""
        return SdkRunnerTransport(self.served.url, signer_is_clearinghouse=told)


@pytest.fixture
async def house(make_engine: EngineFactory, admin: ActorContext) -> AsyncIterator[House]:
    signer = FakeSigningTransport()
    engine = await make_engine(supply=EngineSupply(transport=signer))
    request = AllotRequest(
        tenant_id="default",
        actor_id="alice",
        amount=Money(amount=Decimal(5), currency="USD"),
        operation_ref="allot-alice",
    )
    allotted = await engine.payments.allot(admin, request)
    assert allotted.payment_key is not None and allotted.payment_key.token is not None
    async with listening(create_app(engine, manage_lifecycle=False, listener="public")) as served:
        yield House(engine, signer, served, allotted.payment_key.token)


async def failure(transport: SdkRunnerTransport, net: Net, credential: str) -> Seen:
    with pytest.raises(RunnerCallError) as failed:
        await transport.call(net.runner(), JobRequest(app=APP), credential)
    error = failed.value
    return error.kind, error.reason, error.status_code, error.payment_sent, error.payment_unknown


NOTHING_SENT: Seen = ("payment", "signer_unavailable", 503, False, False)


def unknown(status: int) -> Seen:
    return ("payment", None, status, False, True)


# what the clearinghouse's signer does while signing -> what the engine that uses the clearinghouse reads
WHILE_SIGNING: dict[str, tuple[SignerAnswer | Exception, Seen]] = {
    "the clearinghouse could not reach its signer": (SignerNotReached("refused"), NOTHING_SENT),
    "its signer gave no usable answer": (SignerOutcomeUnknown("timed out"), unknown(502)),
    "its signer answered 500": (refusal(500), unknown(500)),
    "its signer answered 502": (refusal(502), unknown(502)),
    "its signer answered 503": (refusal(503), unknown(503)),  # the signer's own 503: it has no code
    "its signer answered 504": (refusal(504), unknown(504)),
    "an empty allocation": (refusal(402), ("payment", "allowance_exhausted", 402, False, False)),
    "a paused allocation": (refusal(403), ("payment", "not_authorized", 403, False, False)),
    "its signer refused the key": (refusal(401), ("payment", "not_authorized", 401, False, False)),
    "payment params refused": (refusal(400), ("payment_refused", None, 400, False, False)),
    "the session is not this orchestrator's": (refusal(481), ("payment_refused", None, 481, False, False)),
}


@pytest.mark.parametrize("case", sorted(WHILE_SIGNING))
async def test_each_answer_of_the_signer_paths_while_signing_is_classified(
    house: House, net: Net, case: str
) -> None:
    answer, expected = WHILE_SIGNING[case]
    house.signer.signer_answers = [IDENTITY, answer]

    seen = await failure(house.enterprise(), net, house.payment_key)

    assert seen == expected
    assert [call.call for call in house.signer.forwarded] == [
        "sign-orchestrator-info",
        "generate-live-payment",
    ]
    assert net.log == ["challenge m-1"]  # the runner asked for payment once, and did no work


async def test_a_clearinghouse_that_could_not_reach_its_signer_is_read_from_the_code_it_answers(
    house: House, net: Net
) -> None:
    """The row the others turn on, with what crossed the wire: the route's own 503 with
    ``code: signer_not_reached`` and ``Retry-After``, which the SDK keeps on the error's ``body``."""
    house.signer.signer_answers = [IDENTITY, SignerNotReached("refused")]
    with pytest.raises(RunnerCallError) as failed:
        await house.enterprise().call(net.runner(), JobRequest(app=APP), house.payment_key)
    error = failed.value
    crossed = json.loads(error.__cause__.body)  # type: ignore[union-attr]
    assert crossed["code"] == signing.NOT_REACHED_CODE == signer_failures.NOT_REACHED_CODE
    assert (error.kind, error.reason, error.payment_sent, error.payment_unknown) == (
        *NOTHING_SENT[:2],
        False,
        False,
    )
    assert "nothing was signed" in str(error) and house.served.url not in str(error)


async def test_a_transport_that_was_not_told_it_faces_a_clearinghouse_reads_no_code(
    house: House, net: Net
) -> None:
    """A self-hosted engine's transport. Its signer never sends the code, so the code is not read: a 503
    while signing is an unknown outcome, as every 5xx from a signer is."""
    house.signer.signer_answers = [IDENTITY, SignerNotReached("refused")]
    assert await failure(house.enterprise(told=False), net, house.payment_key) == unknown(503)
    assert SdkRunnerTransport(house.served.url)._signer_is_clearinghouse is False  # the default


@pytest.mark.parametrize(
    ("at_identity", "expected"),
    [
        (SignerNotReached("refused"), ("payment", "signer_unavailable", 503, False, False)),
        # nothing is signed at the identity call, so no answer to it can follow a signature
        (SignerOutcomeUnknown("timed out"), ("payment", "signer_unavailable", 502, False, False)),
        (refusal(503), ("payment", "signer_unavailable", 503, False, False)),
    ],
    ids=["not reached", "no usable answer", "the signer's own 503"],
)
async def test_a_failure_at_the_identity_call_signed_nothing_whatever_it_was(
    house: House, net: Net, at_identity: SignerAnswer | Exception, expected: Seen
) -> None:
    house.signer.signer_answers = [at_identity]
    assert await failure(house.enterprise(), net, house.payment_key) == expected
    assert net.log == []  # no runner was asked


async def test_a_payment_key_that_was_rotated_or_revoked_is_not_authorized_and_nothing_is_forwarded(
    house: House, net: Net, admin: ActorContext
) -> None:
    await house.engine.payments.rotate_payment_key(admin, "default", "alice", "rot-1")
    seen = await failure(house.enterprise(), net, house.payment_key)  # the key from before the rotation
    assert seen == ("payment", "not_authorized", 401, False, False)
    assert house.signer.forwarded == [] and net.log == []


async def test_a_clearinghouse_that_is_not_there_is_unavailable_with_nothing_signed(net: Net) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        nowhere = f"http://127.0.0.1:{probe.getsockname()[1]}"
    transport = SdkRunnerTransport(nowhere, signer_is_clearinghouse=True)
    seen = await failure(transport, net, "lpp_" + "x" * 43)
    assert seen == ("payment", "signer_unavailable", None, False, False)


# -- a proxy in front of the clearinghouse makes its own answers ------------------------------------------------

PROXY_ANSWERS: dict[str, tuple[int, str]] = {
    "502 with no body": (502, ""),
    "503 with a page": (503, "<html>Service Unavailable</html>"),
    "504 with a page": (504, "<html>Gateway Timeout</html>"),
    "503 with another code": (503, json.dumps({"error": {"message": "busy"}, "code": "overloaded"})),
    "503 with the code inside the error": (503, json.dumps({"error": {"code": "signer_not_reached"}})),
    "503 with a body that is a list": (503, json.dumps(["signer_not_reached"])),
    "502 with the code": (502, json.dumps({"code": "signer_not_reached"})),  # the code belongs to a 503
    "500 with the code": (500, json.dumps({"code": "signer_not_reached"})),
}


@pytest.mark.parametrize("case", sorted(PROXY_ANSWERS))
async def test_a_5xx_without_the_clearinghouse_own_code_is_an_unknown_outcome_while_signing(
    net: Net, case: str
) -> None:
    """A proxy answers for a clearinghouse it lost, perhaps after the clearinghouse's signer signed. Only
    the clearinghouse's own 503 with its own code says nothing was sent."""
    status, body = PROXY_ANSWERS[case]

    async def identity(_: web.Request) -> web.Response:
        return web.Response(body=SIGNER_IDENTITY, content_type=JSON)

    async def payment(_: web.Request) -> web.Response:
        return web.Response(status=status, text=body)

    app = web.Application()
    app.router.add_post("/sign-orchestrator-info", identity)
    app.router.add_post("/generate-live-payment", payment)
    async with TestServer(app) as proxy:
        transport = SdkRunnerTransport(str(proxy.make_url("")).rstrip("/"), signer_is_clearinghouse=True)
        assert await failure(transport, net, "lpp_" + "x" * 43) == unknown(status)
