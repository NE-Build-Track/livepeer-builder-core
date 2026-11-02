"""D53: the engine's own paid runner call, run against a loopback runner and signer.

The SDK's signer client is real; only the network is local. Each test reads ``net.log`` to check the
order the parties saw: the intent before the signer, the evidence before the paid request.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from decimal import Decimal

import livepeer_gateway.remote_signer as remote_signer
import pytest

from livepeer_builder.adapters.runner_call import (
    PaidRequest,
    PaymentEvents,
    max_price,
    payment_type,
    stop_funding,
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
    """The engine's recorder: ``remember`` before ``get_payment``, ``__call__`` once it returns."""

    def __init__(self, net: Net) -> None:
        self.net = net

    async def remember(self, manifest_id: str) -> None:
        self.net.log.append(f"intent {manifest_id}")

    async def __call__(self, manifest_id: str) -> None:
        self.net.log.append(f"stored {manifest_id}")


async def call(
    net: Net, request: JobRequest | None = None, *, credential: str | None = "lpg_k", **kw: object
):  # type: ignore[no-untyped-def]
    recorder = Recorder(net)
    transport = SdkRunnerTransport(net.url)
    return await transport.call(
        kw.pop("runner", net.runner()),  # type: ignore[arg-type]
        request or JobRequest(app=APP, path="/run"),
        credential,
        on_signed=recorder,
    )


def states(net: Net) -> list[str]:
    return [entry for entry in net.log if entry.startswith("intent")]


async def test_a_paid_call_records_the_intent_signs_then_sends(net: Net) -> None:
    reply = await call(net, JobRequest(app=APP, path="/run", payload={"x": 1}))
    assert net.log == [
        "signer info",
        "challenge m-1",
        "intent m-1",  # LivePaymentSession.manifest_id, before get_payment
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
    ("status", "kind", "unknown"),
    [
        (402, "payment", False),  # allowance: the caller's refusal
        (403, "payment", False),
        (400, "payment_refused", False),  # this orchestrator's params (D26)
        (481, "payment_refused", False),  # price above the ceiling
        (503, "payment", True),  # the outbox: the signer may have charged
    ],
)
async def test_a_signer_refusal_is_unpaid_only_when_the_signer_says_so(
    net: Net, status: int, kind: str, unknown: bool
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
    assert error.payment_unknown is unknown  # a refusal did not validate; a 503 may have charged
    assert states(net) == ["intent m-1"]
    assert not any(entry.startswith("request") for entry in net.log)  # the runner never got paid work


async def test_a_signer_error_logs_its_type_and_whether_the_attempt_was_paid(
    net: Net, caplog: pytest.LogCaptureFixture
) -> None:
    net.sign = [481]
    with (
        caplog.at_level(logging.WARNING, logger="livepeer_builder.adapters.runner_call"),
        pytest.raises(RunnerCallError),
    ):
        await call(net)
    [record] = [item for item in caplog.records if item.message == "signer error"]
    assert (record.error_type, record.paid, record.manifest_id) == ("LivepeerHTTPError", False, "m-1")


@pytest.mark.parametrize(("step", "kind"), [("malformed", "payment"), ("drop", "unreachable")])
async def test_a_lost_or_malformed_signer_reply_leaves_the_manifest_unknown(
    net: Net, step: str, kind: str
) -> None:
    net.sign = [step]
    with pytest.raises(RunnerCallError) as failed:
        await call(net)
    assert (failed.value.kind, failed.value.payment_sent, failed.value.manifest_ids) == (kind, False, ())
    assert failed.value.payment_unknown  # it may have charged, so the job pays nobody else
    assert states(net) == ["intent m-1"]  # asked, not validated: cost sync has no session to match


async def test_a_refresh_is_unpaid_and_asks_for_a_fresh_challenge(net: Net) -> None:
    net.sign = [480]
    reply = await call(net)
    assert reply.manifest_ids == ("m-2",) and reply.payment_sent
    assert net.log[1:] == [
        "challenge m-1",
        "intent m-1",
        "sign m-1",
        "challenge m-2",
        "intent m-2",
        "sign m-2",
        "stored m-2",
        "request pay-m-2",
    ]


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


@pytest.mark.parametrize(
    ("body", "content_type", "data"),
    [
        (b"\x89PNG", "image/png", None),
        (b'{"ok": 1}', "application/json; charset=utf-8", {"ok": 1}),
        (b'{"ok": 1}', "application/vnd.api+json", {"ok": 1}),
        (b"[1, 2]", "application/json", None),  # valid JSON, not an object: still the result
        (b"not json", "application/json", None),
    ],
)
async def test_a_delivered_reply_keeps_its_bytes_whatever_its_shape(
    net: Net, body: bytes, content_type: str, data: object
) -> None:
    net.replies = [Reply(body=body, content_type=content_type)]
    reply = await call(net)
    assert (reply.content, reply.data, reply.payment_sent) == (body, data, True)


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
    # D58: the signer is not a runner; every runner shares it, so this is not a reason to fail over
    down = signer_down.value
    assert (down.kind, down.reason, down.payment_sent) == ("payment", "signer_unavailable", False)


async def test_payment_retries_are_bounded(net: Net) -> None:
    transport = SdkRunnerTransport(net.url, max_payment_challenge_retries=0)  # two requests at most
    net.sign = [480]
    with pytest.raises(RunnerCallError) as refreshing:
        await transport.call(net.runner(), JobRequest(app=APP), "lpg_k")
    assert (refreshing.value.kind, refreshing.value.payment_sent, refreshing.value.payment_unknown) == (
        "payment_refused",
        False,
        False,
    )

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


OTHER_HOST = b'{"payment_params": "p", "manifest_id": "m-1", "payment_url": "https://elsewhere.example/pay"}'


@pytest.mark.parametrize(
    "body", [b"not json", b"[]", b'{"manifest_id": "m-1"}', b'{"payment_params": ""}', OTHER_HOST]
)
async def test_an_invalid_payment_challenge_is_refused_before_signing(net: Net, body: bytes) -> None:
    net.challenge_body = body
    with pytest.raises(RunnerCallError) as invalid:
        await call(net)
    assert (invalid.value.kind, invalid.value.status_code, invalid.value.payment_sent) == ("http", 402, False)
    assert not any(entry.startswith("sign m") for entry in net.log)


async def test_a_redirect_is_a_runner_error_and_is_never_followed(net: Net) -> None:
    """Following it would replay the payment headers to another host and take its answer as the runner's."""
    net.replies = [Reply(status=302, body=b"", headers={"Location": f"{net.url}/app/elsewhere"})]
    with pytest.raises(RunnerCallError) as redirected:
        await call(net)
    assert (redirected.value.kind, redirected.value.status_code, redirected.value.payment_sent) == (
        "http",
        302,
        True,
    )
    assert net.log.count("request pay-m-1") == 1 and len(net.bodies) == 2  # nothing went to /elsewhere


# -- streams -----------------------------------------------------------------------------------------


async def test_a_stream_delivers_the_runner_s_bytes_and_closes(net: Net) -> None:
    net.replies = [Reply(content_type="text/event-stream", chunks=(b"data: a\n\n", b"", b"data: b\n\n"))]
    recorder = Recorder(net)
    stream = await SdkRunnerTransport(net.url).open(
        net.runner(), JobRequest(app=APP, path="/v1/chat"), "lpg_k", on_signed=recorder
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
            on_signed=recorder,
        )
    assert (during.value.kind, during.value.payment_sent, during.value.payment_unknown) == (
        "timeout",
        False,
        True,
    )
    assert states(net) == ["intent m-2"]  # remembered before get_payment, then the deadline fired


async def test_metered_funding_runs_while_the_work_runs_and_is_not_a_new_session(
    net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remote_signer, "PAYMENT_INTERVAL_S", 0.02)
    net.replies = [Reply(hang_s=0.15)]  # a buffered call: funding covers the wait for the reply
    reply = await call(net, runner=net.runner(unit="seconds"))
    await asyncio.sleep(0.05)  # a renewal already on the wire when funding stopped still lands
    renewals = net.log.count("renewal m-1")
    assert renewals >= 2 and reply.manifest_ids == ("m-1",)
    assert net.log.count("stored m-1") == 1 and net.log.count("intent m-1") == 1  # renewals are quiet
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


def test_a_challenge_may_send_payments_to_the_runner_or_its_orchestrator_only() -> None:
    def challenge(payment_url: str) -> str:
        return json.dumps({"payment_params": "p", "manifest_id": "m-1", "payment_url": payment_url})

    paid = PaidRequest(
        "https://runner.example/apps/r1/run", method="POST", body=b"{}", app=APP, rate=None,
        signer_url=None, signer_headers=None, retries=0, events=PaymentEvents(),
        orchestrator_url="https://orch.example:8935",
    )  # fmt: skip
    assert (
        paid._challenge(challenge("https://orch.example:8935/apps/r1/session/m-1/payment")).manifest_id
        == "m-1"
    )
    assert paid._challenge(challenge("https://RUNNER.example:443/pay")).manifest_id == "m-1"
    assert paid._challenge(challenge("https://runner.example:/pay")).manifest_id == "m-1"
    for payment_url in (
        "https://runner.example:0/pay",
        "https://runner.example:00/pay",
        "https://runner.example:nope/pay",
    ):
        with pytest.raises(RunnerCallError, match="invalid payment challenge") as refused:
            paid._challenge(challenge(payment_url))
        assert (refused.value.kind, refused.value.status_code, refused.value.payment_sent) == (
            "http",
            402,
            False,
        )
    with pytest.raises(RunnerCallError, match="invalid payment challenge"):
        paid._challenge(challenge("https://10.0.0.5/pay"))


def test_price_unit_and_ceiling() -> None:
    rate = NetworkRate(amount=Decimal("2"), currency="USD", unit="Seconds")
    assert (payment_type(rate), payment_type(None)) == ("live", "live")
    assert max_price(rate) == {"price": pytest.approx(2.024), "currency": "usd", "unit": "seconds"}
    assert max_price(None) is None
    with pytest.raises(RunnerCallError, match="unsupported runner price unit") as unsupported:
        payment_type(NetworkRate(amount=Decimal("1"), currency="usd", unit="request"))
    assert (unsupported.value.kind, unsupported.value.payment_sent) == (
        "payment_refused",
        False,
    )  # next runner


async def test_stopping_the_funding_never_swallows_the_caller_s_own_cancel() -> None:
    release = asyncio.Event()

    async def slow_to_stop() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await release.wait()  # winds down slowly

    funding = asyncio.create_task(slow_to_stop())
    await asyncio.sleep(0)
    caller = asyncio.create_task(stop_funding(funding))
    await asyncio.sleep(0.01)
    caller.cancel()  # a disconnect or a deadline while the caller waits for the funding to stop
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()
    await asyncio.wait([funding])
