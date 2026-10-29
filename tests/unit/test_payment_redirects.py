"""#72: no request in the payment plane follows a redirect (D58).

Renewals and refreshes go out on the engine's own payment client, run against a loopback runner, signer
and a second origin (``Target``) that a 3xx points to. The real aiohttp client meets a real server, so a
followed redirect shows up as a request on the target.
"""

import asyncio
import gc
import logging
from collections.abc import AsyncIterator
from typing import Any

import livepeer_gateway.remote_signer as remote_signer
import pytest
from livepeer_gateway.errors import PaymentError
from livepeer_gateway.remote_signer import LivePaymentChallenge

from livepeer_builder.adapters.payment_funding import EnginePaymentSession, fund, origin
from livepeer_builder.adapters.runner_call import PaymentEvents, SdkRunnerStream, start_funding, stop_funding
from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.contracts import JobRequest
from tests.unit.livepeer_net import Net, Reply, Target, start, start_target

APP = "example/echo"
INTERVAL_S = 0.02


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        yield value


@pytest.fixture
async def target() -> AsyncIterator[Target]:
    async for value in start_target():
        yield value


async def open_metered(net: Net, monkeypatch: pytest.MonkeyPatch) -> SdkRunnerStream:
    monkeypatch.setattr(remote_signer, "PAYMENT_INTERVAL_S", INTERVAL_S)
    net.replies = [Reply(content_type="text/event-stream", chunks=(b"a",))]
    return await SdkRunnerTransport(net.url).open(net.runner(unit="seconds"), JobRequest(app=APP), "lpg_k")


def session(net: Net, payment_url: str | None = None) -> EnginePaymentSession:
    """A session as the paid request leaves it, for driving ``fund`` directly."""
    challenge = LivePaymentChallenge("params", "m-1", payment_url or f"{net.url}/pay/m-1")
    return EnginePaymentSession(net.url, signer_headers={"Authorization": "Bearer lpg_k"}, type="live",
                                challenge=challenge, app=APP,
                                payment_origins=frozenset({origin(net.url)}))  # fmt: skip


def tickets(net: Net) -> int:
    return sum(entry.startswith("sign ") for entry in net.log)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_a_redirected_renewal_reaches_no_one_and_mints_no_further_ticket(
    net: Net, target: Target, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    net.renewal_status, net.location = status, f"{target.url}/elsewhere"
    stream = await open_metered(net, monkeypatch)
    await asyncio.sleep(10 * INTERVAL_S)
    await stream.aclose()
    assert target.requests == []
    assert tickets(net) == 1 + 1  # the paid request's, then the renewal that was redirected


async def test_a_redirected_refresh_reaches_no_one_and_its_params_are_never_signed(
    net: Net, target: Target, monkeypatch: pytest.MonkeyPatch
) -> None:
    net.sign = ["ok", 480]  # the paid request's ticket; at the first renewal the signer asks for a refresh
    net.refresh_status, net.location = 307, f"{target.url}/elsewhere"
    stream = await open_metered(net, monkeypatch)
    await asyncio.sleep(10 * INTERVAL_S)
    await stream.aclose()
    assert target.requests == []
    assert [request["orchestrator"] for request in net.signer_requests] == ["params", "params"]


async def test_a_redirect_to_the_payment_origin_itself_is_not_followed_either(
    net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    net.renewal_status, net.location = (
        308,
        f"{net.url}/pay/m-1-moved",
    )  # a trailing-slash or canonical-host fix
    stream = await open_metered(net, monkeypatch)
    await asyncio.sleep(10 * INTERVAL_S)
    await stream.aclose()
    assert net.log.count("renewal m-1") == 1 and "renewal m-1-moved" not in net.log


# -- parity with the SDK's run_payments ----------------------------------------------------------------


async def test_a_paid_renewal_keeps_funding_once_every_interval(net: Net) -> None:
    funding = asyncio.create_task(fund(session(net), interval_s=INTERVAL_S))
    await asyncio.sleep(5.5 * INTERVAL_S)
    funding.cancel()
    assert await funding == "cancelled"
    assert 3 <= net.log.count("renewal m-1") <= 5  # never faster than the interval; the first waits one


@pytest.mark.parametrize(("status", "end"), [(404, "released"), (403, "refused"), (409, "refused")])
async def test_a_4xx_renewal_ends_funding(net: Net, status: int, end: str) -> None:
    net.renewal_status = status
    assert await fund(session(net), interval_s=INTERVAL_S) == end
    assert net.log.count("renewal m-1") == 1


@pytest.mark.parametrize("status", [408, 429, 503])
async def test_a_renewal_asked_to_retry_is_tried_again_next_cycle(net: Net, status: int) -> None:
    net.renewal_status = status
    funding = asyncio.create_task(fund(session(net), interval_s=INTERVAL_S))
    await asyncio.sleep(5.5 * INTERVAL_S)
    funding.cancel()
    assert await funding == "cancelled" and net.log.count("renewal m-1") >= 3


async def test_a_renewal_that_loses_its_connection_is_tried_again_next_cycle(net: Net) -> None:
    net.renewal_drops = 2
    funding = asyncio.create_task(fund(session(net), interval_s=INTERVAL_S))
    await asyncio.sleep(5.5 * INTERVAL_S)
    funding.cancel()
    assert await funding == "cancelled" and net.log.count("renewal m-1") >= 3  # two lost, then paid


async def test_a_signer_refresh_during_renewal_fetches_new_params_once_and_renews_with_them(
    net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    net.sign = ["ok", 480]  # the paid request's ticket; the first renewal's signer asks for fresh params
    stream = await open_metered(net, monkeypatch)
    await asyncio.sleep(3.5 * INTERVAL_S)
    await stream.aclose()
    signed_with = [request["orchestrator"] for request in net.signer_requests]
    assert signed_with[:3] == ["params", "params", "params-1"] and set(signed_with[3:]) <= {"params-1"}
    assert net.log.count("refresh m-1") == 1 and net.log.count("renewal m-1") >= 1


async def test_stopping_the_funding_closes_its_payment_client(
    net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remote_signer, "PAYMENT_INTERVAL_S", INTERVAL_S)
    reported: list[str] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(str(context.get("message"))))
    try:
        funding: Any = start_funding(session(net), PaymentEvents())
        await asyncio.sleep(2.5 * INTERVAL_S)
        await stop_funding(funding)
        del funding
        gc.collect()  # aiohttp reports a client nobody closed when it is collected
    finally:
        loop.set_exception_handler(previous)
    assert "renewal m-1" in net.log and reported == []


@pytest.mark.parametrize("answer", [b"not json", b"[]", b'{"payment_params": ""}'])
async def test_a_refresh_without_usable_params_signs_nothing_new_and_retries_next_cycle(
    net: Net, monkeypatch: pytest.MonkeyPatch, answer: bytes
) -> None:
    net.sign, net.refresh_body = ["ok", *[480] * 10], answer  # every renewal's signer asks for a refresh
    stream = await open_metered(net, monkeypatch)
    await asyncio.sleep(3.5 * INTERVAL_S)
    await stream.aclose()
    assert {request["orchestrator"] for request in net.signer_requests} == {"params"}
    assert net.log.count("refresh m-1") >= 2 and "renewal m-1" not in net.log


async def test_a_cycle_the_signer_skips_sends_no_renewal_and_funding_goes_on(net: Net) -> None:
    net.sign = [482]  # SkipPaymentCycle, then tickets again
    funding = asyncio.create_task(fund(session(net), interval_s=INTERVAL_S))
    await asyncio.sleep(3.5 * INTERVAL_S)
    funding.cancel()
    assert await funding == "cancelled"
    assert net.log[:2] == ["sign m-1", "sign m-1"] and net.log.count("renewal m-1") >= 1


async def test_an_unpaid_session_sends_no_renewal(net: Net) -> None:
    unpaid = EnginePaymentSession(None, type="live", challenge=LivePaymentChallenge("params", "m-1", f"{net.url}/pay/m-1"),
                                  payment_origins=frozenset({origin(net.url)}))  # fmt: skip
    await unpaid.send_payment()
    assert net.log == []


# -- guards -------------------------------------------------------------------------------------------


def stops(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    return [
        (record.reason, record.manifest_id)  # type: ignore[attr-defined]
        for record in caplog.records
        if getattr(record, "event", None) == "runner.funding_stopped"
    ]


async def test_why_funding_ended_is_recorded_on_the_call_and_logged(
    net: Net, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="livepeer_builder.adapters")
    monkeypatch.setattr(remote_signer, "PAYMENT_INTERVAL_S", INTERVAL_S)
    net.renewal_status = 404
    events = PaymentEvents()
    funding: Any = start_funding(session(net), events)
    await asyncio.wait([funding])
    assert events.funding_end == "released"
    assert stops(caplog) == [("released", "m-1")]


async def test_a_redirected_renewal_stops_the_funding_but_not_the_call(
    net: Net, target: Target, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="livepeer_builder.adapters")
    monkeypatch.setattr(remote_signer, "PAYMENT_INTERVAL_S", INTERVAL_S)
    net.renewal_status, net.location = 307, f"{target.url}/elsewhere"
    net.replies = [Reply(hang_s=10 * INTERVAL_S)]  # the work outlives the first renewal
    reply = await SdkRunnerTransport(net.url).call(net.runner(unit="seconds"), JobRequest(app=APP), "lpg_k")
    assert (reply.status_code, reply.payment_sent) == (200, True)  # the orchestrator ends the session
    assert stops(caplog) == [("redirected", "m-1")]


async def test_a_payment_url_off_the_challenge_s_origins_is_refused_before_a_ticket_or_a_request(
    net: Net, target: Target
) -> None:
    payment = session(net, payment_url=f"{target.url}/pay/m-1")  # only the runner's origin was proven
    with pytest.raises(PaymentError, match="not a payment origin"):
        await payment.send_payment()
    await payment.aclose()
    assert target.requests == [] and tickets(net) == 0
