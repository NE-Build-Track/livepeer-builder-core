"""#166: one signer call, forwarded by a clearinghouse to its own signer (``HttpSignerForwarder``).

A clearinghouse serves the signer's two paths and hands each call to its private signer with the actor's
Batteries key in place of the payment key (D87). This is the wire of that: the signer's address as the
SDK reads it, the request as the signer must see it, the answer as it came, and the two ways a call can
fail. The difference between them is money: a call that never left signed nothing, and one that left
and brought no answer may have signed.

The signer is an in-memory HTTP transport. No test here calls a signer.
"""

import asyncio
import logging
from typing import Any

import httpx
import pytest
from livepeer_gateway.http import _http_origin

from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.adapters.signer_forward import HttpSignerForwarder
from livepeer_builder.contracts import SignerAnswer
from livepeer_builder.errors import SignerNotReached, SignerOutcomeUnknown
from livepeer_builder.protocols import SignerForwarder

SIGNER = "http://signer.internal:8935"
KEY = "lpg_batteries-key-of-alice"
STATE = b'{"orchestrator":"cGFyYW1z","type":"live","ManifestID":"m-1","maxPrice":{"price":1.012e+3},"state":{"state":"b3BhcXVl","sig":"c2ln"}}'


class Signer:
    """What the signer received, and what it answers: a response, or an error raised by the transport."""

    def __init__(self, answer: httpx.Response | Exception | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.answer = answer

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer or httpx.Response(200, json={"payment": "p", "segCreds": "s", "state": {"n": 1}})

    def forwarder(self, address: str = SIGNER, **options: Any) -> HttpSignerForwarder:
        return HttpSignerForwarder(address, transport=httpx.MockTransport(self.handle), **options)


async def test_a_call_reaches_the_signer_with_the_actors_key_and_the_body_untouched() -> None:
    signer = Signer()
    answer = await signer.forwarder().forward_signer_call(
        "generate-live-payment", KEY, STATE, "application/json"
    )
    [request] = signer.requests
    assert (request.method, str(request.url)) == ("POST", f"{SIGNER}/generate-live-payment")
    assert request.content == STATE  # the bytes as they came: the float, the signed state, the key order
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["content-type"] == "application/json"
    # nothing else of anyone's travels: the signer hands every header it gets to the Batteries webhook
    assert set(request.headers) <= {
        "host", "authorization", "content-type", "content-length", "accept", "accept-encoding",
        "user-agent", "connection",
    }  # fmt: skip
    assert answer == SignerAnswer(
        status_code=200,
        content=b'{"payment":"p","segCreds":"s","state":{"n":1}}',
        content_type="application/json",
    )


async def test_the_identity_call_goes_to_the_signers_other_path() -> None:
    identity = httpx.Response(200, json={"address": "0xabc", "signature": "0xsig"})
    signer = Signer(identity)
    answer = await signer.forwarder().forward_signer_call("sign-orchestrator-info", KEY, b"{}", "")
    assert str(signer.requests[0].url) == f"{SIGNER}/sign-orchestrator-info"
    assert signer.requests[0].headers["content-type"] == "application/json"  # none given: the SDK's own
    assert answer.status_code == 200 and b"0xabc" in answer.content


@pytest.mark.parametrize(
    "address",
    ["signer.internal:8935", "http://signer.internal:8935/", "https://signer.example/a/path", " http://h:1 "],
)
async def test_the_signers_address_is_read_as_the_sdk_reads_it(address: str) -> None:
    """One setting names the signer for the engine's own payments and for the calls it forwards, so both
    must reach the same place: the scheme and the host, with any path dropped, and ``https`` for a bare
    host (``livepeer_gateway.http._http_origin`` at the pin)."""
    signer = Signer()
    await signer.forwarder(address).forward_signer_call("generate-live-payment", KEY, b"{}", "")
    assert str(signer.requests[0].url) == f"{_http_origin(address)}/generate-live-payment"


@pytest.mark.parametrize("status", [400, 401, 402, 403, 480, 481, 482, 500, 502, 503])
async def test_whatever_the_signer_answers_is_the_answer(status: int) -> None:
    """A refusal is the signer's, and Batteries' behind it: an empty allocation is its 402, a paused one
    its 403. The clearinghouse decides nothing about money, so the status, the body and the content type
    come back as they were."""
    body = b'{"error":{"message":"signer auth rejected request with status %d"}}' % status
    signer = Signer(httpx.Response(status, content=body, headers={"content-type": "application/json"}))
    answer = await signer.forwarder().forward_signer_call("generate-live-payment", KEY, STATE, "")
    assert (answer.status_code, answer.content, answer.content_type) == (status, body, "application/json")
    assert len(signer.requests) == 1


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("connection refused"),
        httpx.ConnectTimeout("no connection in time"),
        httpx.PoolTimeout("no connection free"),
        httpx.UnsupportedProtocol("no such scheme"),
    ],
    ids=lambda failure: type(failure).__name__,
)
async def test_a_call_that_never_left_is_not_reached(failure: Exception) -> None:
    with pytest.raises(SignerNotReached) as raised:
        await Signer(failure).forwarder().forward_signer_call("generate-live-payment", KEY, STATE, "")
    assert KEY not in str(raised.value) and KEY not in repr(raised.value)


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("no answer in time"),
        httpx.WriteTimeout("the request did not finish"),
        httpx.WriteError("the connection broke while sending"),
        httpx.ReadError("the connection broke while reading"),
        httpx.RemoteProtocolError("the signer closed the connection"),
        httpx.DecodingError("the answer did not decode"),
    ],
    ids=lambda failure: type(failure).__name__,
)
async def test_a_call_that_left_and_brought_no_answer_is_an_unknown_outcome(failure: Exception) -> None:
    signer = Signer(failure)
    with pytest.raises(SignerOutcomeUnknown) as raised:
        await signer.forwarder().forward_signer_call("generate-live-payment", KEY, STATE, "")
    assert KEY not in str(raised.value) and len(signer.requests) == 1  # and it is not sent again


@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_a_redirect_is_not_followed_and_not_passed_on(status: int) -> None:
    """The SDK follows a redirect with its credential, and the clearinghouse would follow one with the
    Batteries key. Neither happens: a signer does not redirect, so this is no answer."""
    signer = Signer(httpx.Response(status, headers={"location": "http://elsewhere.example/steal"}))
    with pytest.raises(SignerOutcomeUnknown):
        await signer.forwarder().forward_signer_call("generate-live-payment", KEY, STATE, "")
    assert [str(request.url) for request in signer.requests] == [f"{SIGNER}/generate-live-payment"]


async def test_a_signer_that_does_not_answer_inside_the_deadline_is_an_unknown_outcome() -> None:
    """The SDK gives a signer call 5 seconds and does not retry, so the clearinghouse's own call ends
    before that. Whether the signer signed is not known."""
    release = asyncio.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(200)

    forwarder = HttpSignerForwarder(SIGNER, transport=httpx.MockTransport(hang), deadline_s=0.05)
    try:
        with pytest.raises(SignerOutcomeUnknown):
            await forwarder.forward_signer_call("generate-live-payment", KEY, STATE, "")
    finally:
        release.set()
    assert HttpSignerForwarder(SIGNER).deadline_s < 5.0  # inside the SDK's budget


async def test_the_key_is_in_no_log_line_error_or_repr(caplog: pytest.LogCaptureFixture) -> None:
    said: list[str] = []
    with caplog.at_level(logging.DEBUG):
        for failure in (httpx.ConnectError(f"refused {KEY}"), httpx.ReadTimeout(f"late {KEY}"), None):
            forwarder = Signer(failure).forwarder()
            said += [repr(forwarder), str(vars(forwarder))]
            try:
                await forwarder.forward_signer_call("generate-live-payment", KEY, STATE, "")
            except (SignerNotReached, SignerOutcomeUnknown) as error:
                said += [str(error), repr(error)]
    said += [f"{record.getMessage()} {vars(record)}" for record in caplog.records]
    assert len(said) >= 10 and not [text for text in said if KEY in text]


async def test_the_sdk_transport_forwards_to_the_signer_it_pays_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transport that pays the engine's own jobs is the part that knows the signer, so it is the one
    that forwards. With no signer it pays nothing and forwards nothing."""
    transport = SdkRunnerTransport(SIGNER)
    assert isinstance(transport, SignerForwarder) and transport.pays
    signer = Signer()
    monkeypatch.setattr(transport, "_forwarder", signer.forwarder())
    answer = await transport.forward_signer_call("generate-live-payment", KEY, STATE, "application/json")
    assert answer.status_code == 200 and signer.requests[0].content == STATE

    unpaid = SdkRunnerTransport(None)
    assert not unpaid.pays
    with pytest.raises(SignerNotReached):
        await unpaid.forward_signer_call("generate-live-payment", KEY, STATE, "")
