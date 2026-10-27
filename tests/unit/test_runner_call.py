"""D53: the engine's own paid runner call, run against a loopback runner and signer.

The SDK's signer client is real; only the network is local. Each test reads ``net.log`` to check the
order the parties saw: the intent before the signer, the evidence before the paid request.
"""

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal

import livepeer_gateway.remote_signer as remote_signer
import pytest
from livepeer_gateway.errors import LivepeerGatewayError, LivepeerHTTPError, PaymentError

from livepeer_builder.adapters.runner_call import (
    PaymentEvents,
    max_price,
    network_kind,
    payment_type,
    signer_error,
)
from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.contracts import JobRequest, NetworkRate, Runner
from livepeer_builder.errors import RunnerCallError
from tests.unit.livepeer_net import Net, Reply, start

APP = "example/echo"


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        yield value


class Recorder:
    """The engine's two callbacks, writing to the network log so the order is visible."""

    def __init__(self, net: Net, fail_intent: bool = False) -> None:
        self.net = net
        self.fail_intent = fail_intent

    async def on_signed(self, manifest_id: str) -> None:
        self.net.log.append(f"stored {manifest_id}")

    async def on_payment_state(self, manifest_id: str, state: str) -> None:
        if self.fail_intent:
            raise RuntimeError("the store is down")
        self.net.log.append(f"intent {manifest_id} {state}")


async def call(
    net: Net, request: JobRequest | None = None, *, credential: str | None = "lpg_k", **kw: object
):  # type: ignore[no-untyped-def]
    recorder = Recorder(net, fail_intent=bool(kw.pop("fail_intent", False)))
    transport = SdkRunnerTransport(net.url)
    return await transport.call(
        kw.pop("runner", net.runner()),  # type: ignore[arg-type]
        request or JobRequest(app=APP, path="/run"),
        credential,
        on_signed=recorder.on_signed,
        on_payment_state=recorder.on_payment_state,
    )


def states(net: Net) -> list[str]:
    return [entry for entry in net.log if entry.startswith("intent")]


async def test_a_paid_call_records_the_intent_signs_then_sends(net: Net) -> None:
    reply = await call(net, JobRequest(app=APP, path="/run", payload={"x": 1}))
    assert net.log == [
        "signer info",
        "challenge m-1",
        "intent m-1 unknown",  # on record before the signer is asked
        "sign m-1",
        "stored m-1",  # the evidence before the paid request (D42)
        "request pay-m-1",
    ]
    assert (reply.status_code, reply.content, reply.data) == (200, b'{"ok": true}', {"ok": True})
    assert (reply.manifest_ids, reply.payment_sent) == (("m-1",), True)
    assert net.bodies == [{"x": 1}, {"x": 1}]
    sent = net.signer_requests[0]
    assert (sent["type"], sent["app"], sent["ManifestID"]) == ("fixed", APP, "m-1")
    assert sent["maxPrice"] == {"price": pytest.approx(0.1012), "currency": "usd", "unit": "fixed"}


async def test_a_missing_payload_is_sent_as_an_empty_json_object(net: Net) -> None:
    await call(net, JobRequest(app=APP))
    assert net.bodies == [{}, {}]


async def test_an_unpaid_call_never_reaches_the_signer(net: Net) -> None:
    net.paid = False
    reply = await call(net, credential=None)
    assert net.log == ["request free"] and not reply.payment_sent and reply.manifest_ids == ()

    net.paid = True  # the runner wants payment, and the call has no credential
    with pytest.raises(RunnerCallError) as unpaid:
        await call(net, credential=None)
    assert (unpaid.value.kind, unpaid.value.status_code, unpaid.value.payment_sent) == ("http", 402, False)
    assert not any(entry.startswith("sign") for entry in net.log)


@pytest.mark.parametrize(
    ("status", "kind", "seen"),
    [
        (402, "payment", ["unknown", "unpaid"]),  # allowance: the caller's refusal
        (403, "payment", ["unknown", "unpaid"]),
        (400, "payment_refused", ["unknown", "unpaid"]),  # this orchestrator's params (D26)
        (481, "payment_refused", ["unknown", "unpaid"]),  # price above the ceiling
        (503, "payment", ["unknown"]),  # the outbox: the signer may have charged
    ],
)
async def test_a_signer_refusal_is_unpaid_only_when_the_signer_says_so(
    net: Net, status: int, kind: str, seen: list[str]
) -> None:
    net.sign = [status]
    with pytest.raises(RunnerCallError) as refused:
        await call(net)
    error = refused.value
    assert (error.kind, error.status_code, error.payment_sent, error.manifest_ids) == (
        kind,
        status,
        False,
        (),
    )
    assert states(net) == [f"intent m-1 {state}" for state in seen]
    assert not any(entry.startswith("request") for entry in net.log)  # the runner never got paid work


@pytest.mark.parametrize(("step", "kind"), [("malformed", "payment"), ("drop", "unreachable")])
async def test_a_lost_or_malformed_signer_reply_leaves_the_manifest_unknown(
    net: Net, step: str, kind: str
) -> None:
    net.sign = [step]
    with pytest.raises(RunnerCallError) as failed:
        await call(net)
    assert (failed.value.kind, failed.value.payment_sent, failed.value.manifest_ids) == (kind, False, ())
    assert states(net) == ["intent m-1 unknown"]  # it may have charged: cost sync settles it later


async def test_a_refresh_is_unpaid_and_asks_for_a_fresh_challenge(net: Net) -> None:
    net.sign = [480]
    reply = await call(net)
    assert reply.manifest_ids == ("m-2",) and reply.payment_sent
    assert net.log[1:] == [
        "challenge m-1",
        "intent m-1 unknown",
        "sign m-1",
        "intent m-1 unpaid",
        "challenge m-2",
        "intent m-2 unknown",
        "sign m-2",
        "stored m-2",
        "request pay-m-2",
    ]


async def test_nothing_is_signed_when_the_intent_cannot_be_recorded(net: Net) -> None:
    with pytest.raises(RunnerCallError) as failed:
        await call(net, fail_intent=True)
    assert (failed.value.kind, failed.value.payment_sent) == ("payment", False)
    assert "intent" in str(failed.value) and not any(entry.startswith("sign m") for entry in net.log)


@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (503, b"no capacity available for runner", "refused"),
        (404, b"runner session not found", "http"),  # the runner stopped the session (gateway #83)
        (409, b"session already exists", "http"),
        (500, b"trace", "http"),
    ],
)
async def test_a_runner_refusal_after_signing_keeps_the_payment(
    net: Net, status: int, body: bytes, kind: str
) -> None:
    net.replies = [Reply(status=status, body=body, content_type="text/plain")]
    with pytest.raises(RunnerCallError) as refused:
        await call(net)
    error = refused.value
    assert (error.kind, error.status_code, error.body) == (kind, status, body.decode())
    assert (error.payment_sent, error.manifest_ids) == (True, ("m-1",))


async def test_replies_keep_their_bytes_and_bad_json_is_a_runner_error(net: Net) -> None:
    net.replies = [Reply(body=b"\x89PNG", content_type="image/png"), Reply(body=b"not json")]
    image = await call(net)
    assert (image.content, image.content_type, image.data) == (b"\x89PNG", "image/png", None)
    with pytest.raises(RunnerCallError) as bad:
        await call(net)
    assert (bad.value.kind, bad.value.payment_sent) == ("http", True)


async def test_unreachable_runner_or_signer_is_unpaid() -> None:
    closed = "http://127.0.0.1:9"
    transport = SdkRunnerTransport(None)
    target = Runner(
        url=closed, app=APP, runner_id="r", mode="single-shot", orchestrator_url=closed,
        capacity=1, capacity_available=1, rate=None,
    )  # fmt: skip
    with pytest.raises(RunnerCallError) as runner_down:
        await transport.call(target, JobRequest(app=APP), None)
    assert (runner_down.value.kind, runner_down.value.payment_sent) == ("unreachable", False)
    with pytest.raises(RunnerCallError) as signer_down:
        await SdkRunnerTransport(closed).call(target, JobRequest(app=APP), "lpg_k")
    assert (signer_down.value.kind, signer_down.value.payment_sent) == ("unreachable", False)


async def test_payment_retries_are_bounded(net: Net) -> None:
    transport = SdkRunnerTransport(net.url, max_payment_challenge_retries=0)  # two requests at most
    net.sign = [480]
    with pytest.raises(RunnerCallError) as refreshing:
        await transport.call(net.runner(), JobRequest(app=APP), "lpg_k")
    assert (refreshing.value.kind, refreshing.value.payment_sent) == ("payment", False)

    net.challenge_paid = True  # the runner takes the payment and asks again
    with pytest.raises(RunnerCallError) as asking:
        await transport.call(net.runner(), JobRequest(app=APP), "lpg_k")
    error = asking.value
    assert (error.kind, error.status_code, error.payment_sent, error.manifest_ids) == (
        "http",
        402,
        True,
        ("m-2",),
    )


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"manifest_id": "m-1"}', b'{"payment_params": ""}'])
async def test_an_invalid_payment_challenge_is_refused_before_signing(net: Net, body: bytes) -> None:
    net.challenge_body = body
    with pytest.raises(RunnerCallError) as invalid:
        await call(net)
    assert (invalid.value.kind, invalid.value.status_code, invalid.value.payment_sent) == ("http", 402, False)
    assert not any(entry.startswith("sign m") for entry in net.log)


async def test_a_json_reply_must_be_an_object(net: Net) -> None:
    net.replies = [Reply(body=b"[1, 2]")]
    with pytest.raises(RunnerCallError, match="not an object") as bad:
        await call(net)
    assert bad.value.payment_sent


# -- streams -----------------------------------------------------------------------------------------


async def test_a_stream_delivers_the_runner_s_bytes_and_closes(net: Net) -> None:
    net.replies = [Reply(content_type="text/event-stream", chunks=(b"data: a\n\n", b"", b"data: b\n\n"))]
    recorder = Recorder(net)
    stream = await SdkRunnerTransport(net.url).open(
        net.runner(), JobRequest(app=APP, path="/v1/chat"), "lpg_k", on_signed=recorder.on_signed
    )
    assert (stream.status_code, stream.content_type) == (200, "text/event-stream")
    assert (stream.manifest_ids, stream.payment_sent) == (("m-1",), True)
    assert b"".join([chunk async for chunk in stream]) == b"data: a\n\ndata: b\n\n"
    await stream.aclose()


async def test_a_stream_cut_mid_way_is_a_runner_error_that_keeps_the_payment(net: Net) -> None:
    net.replies = [Reply(content_type="text/event-stream", chunks=(b"x",), drop_after_chunks=True)]
    stream = await SdkRunnerTransport(net.url).open(net.runner(), JobRequest(app=APP), "lpg_k")
    received: list[bytes] = []

    async def drain() -> None:
        async for chunk in stream:
            received.append(chunk)

    with pytest.raises(RunnerCallError) as cut:
        await drain()
    await stream.aclose()
    assert received == [b"x"] and "stream failed" in str(cut.value)
    assert (cut.value.payment_sent, cut.value.manifest_ids) == (True, ("m-1",))


async def test_opening_is_bounded_by_first_byte_timeout_whether_or_not_it_paid(net: Net) -> None:
    net.replies = [Reply(hang_s=5)]  # the runner took the payment and never sent its headers
    transport = SdkRunnerTransport(net.url)
    with pytest.raises(RunnerCallError) as after:
        await transport.open(net.runner(), JobRequest(app=APP, first_byte_timeout_s=0.1), "lpg_k")
    assert (after.value.kind, after.value.payment_sent, after.value.manifest_ids) == (
        "timeout",
        True,
        ("m-1",),
    )
    assert "0.1s" in str(after.value)

    net.sign = [5.0]  # the signer never answers
    recorder = Recorder(net)
    with pytest.raises(RunnerCallError) as during:
        await transport.open(
            net.runner(),
            JobRequest(app=APP, first_byte_timeout_s=0.1),
            "lpg_k",
            on_payment_state=recorder.on_payment_state,
        )
    assert (during.value.kind, during.value.payment_sent) == ("timeout", False)
    assert states(net) == ["intent m-2 unknown"]  # cancelled while signing: still on record


async def test_metered_funding_runs_while_the_work_runs_and_is_not_a_new_session(
    net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remote_signer, "PAYMENT_INTERVAL_S", 0.02)
    net.replies = [Reply(hang_s=0.15)]  # a buffered call: funding covers the wait for the reply
    reply = await call(net, runner=net.runner(unit="seconds"))
    await asyncio.sleep(0.05)  # a renewal already on the wire when funding stopped still lands
    renewals = net.log.count("renewal m-1")
    assert renewals >= 2 and reply.manifest_ids == ("m-1",)
    assert net.log.count("stored m-1") == 1 and net.log.count("intent m-1 unknown") == 1  # renewals are quiet
    await asyncio.sleep(0.1)
    assert net.log.count("renewal m-1") == renewals  # the funding stopped with the call

    net.replies = [Reply(content_type="text/event-stream", chunks=(b"a",))]
    stream = await SdkRunnerTransport(net.url).open(net.runner(unit="seconds"), JobRequest(app=APP), "lpg_k")
    await asyncio.sleep(0.1)
    assert net.log.count("renewal m-2") >= 2  # the open stream owns its funding
    await stream.aclose()
    await asyncio.sleep(0.05)
    stopped = net.log.count("renewal m-2")
    await asyncio.sleep(0.1)
    assert net.log.count("renewal m-2") == stopped

    net.renewal_status = 404  # the orchestrator released the session: funding stops on its own
    net.replies = [Reply(content_type="text/event-stream", chunks=(b"a",))]
    stream = await SdkRunnerTransport(net.url).open(net.runner(unit="seconds"), JobRequest(app=APP), "lpg_k")
    await asyncio.sleep(0.1)
    assert net.log.count("renewal m-3") == 1
    await stream.aclose()


# -- pure pieces -------------------------------------------------------------------------------------


def test_signer_errors_map_to_outcomes() -> None:
    events = PaymentEvents()
    signer = "https://signer.example/generate-live-payment"
    assert signer_error(LivepeerHTTPError(400, signer), events).kind == "payment_refused"
    assert signer_error(LivepeerHTTPError(481, signer), events).kind == "payment_refused"
    assert signer_error(LivepeerHTTPError(402, signer), events).kind == "payment"  # every orchestrator alike
    assert signer_error(PaymentError("no segCreds"), events).kind == "payment"
    lost = LivepeerGatewayError("failed to reach endpoint")
    lost.__cause__ = TimeoutError()
    assert signer_error(lost, events).kind == "timeout"
    assert network_kind(RuntimeError("other")) == "http"


def test_price_unit_and_ceiling() -> None:
    rate = NetworkRate(amount=Decimal("2"), currency="USD", unit="Seconds")
    assert (payment_type(rate), payment_type(None)) == ("live", "live")
    assert max_price(rate) == {"price": pytest.approx(2.024), "currency": "usd", "unit": "seconds"}
    assert max_price(None) is None
    with pytest.raises(RunnerCallError, match="unsupported runner price unit"):
        payment_type(NetworkRate(amount=Decimal("1"), currency="usd", unit="request"))
