"""Whether the signer is ready, asked the way a payment begins (#167).

The wallet report says whether the clearinghouse's signer answers. The engine asks with the call every
paid job starts with, ``POST /sign-orchestrator-info`` at the root of the signer address: it needs no
credential at the pin (livepeer/node ``eee09c4``, ``signer/service.go``), signs no payment and costs
nothing. The signer's own ``/readyz`` is on its metrics listener, which the engine is not given.

The payment SDK remembers a signer's identity for the life of the process. The probe does not use what
is remembered: a signer that has gone away is not ready, though jobs would still find its identity.
"""

import socket
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from livepeer_gateway.remote_signer import get_signer_info

from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.contracts import SignerReadiness
from livepeer_builder.protocols import SignerProbe
from tests.unit.livepeer_net import Net, identity_only_signer, start


@pytest.fixture
async def net() -> AsyncIterator[Net]:
    async for value in start():
        yield value


def closed_port_url() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{probe.getsockname()[1]}"


async def test_a_signer_that_answers_its_identity_call_is_ready(net: Net) -> None:
    transport = SdkRunnerTransport(net.url)
    assert isinstance(transport, SignerProbe)
    assert await transport.signer_readiness() == SignerReadiness(ready=True, reason=None)
    assert net.log == ["signer info"]


async def test_the_probe_sends_no_credential_and_asks_at_the_root_of_the_signer_address() -> None:
    """The SDK keeps only the scheme, host and port of the signer address, and so does the probe."""
    seen: list[tuple[str, dict[str, str], Any]] = []

    async def info(request: web.Request) -> web.Response:
        seen.append((request.path, dict(request.headers), await request.json()))
        return web.json_response({"address": "0x" + "ab" * 20, "signature": "0xsig"})

    app = web.Application()
    app.router.add_post("/sign-orchestrator-info", info)
    async with TestServer(app) as server:
        url = str(server.make_url("/a/path/the/sdk/drops"))
        assert (await SdkRunnerTransport(url).signer_readiness()).ready
    [(path, headers, body)] = seen
    assert (path, body) == ("/sign-orchestrator-info", {})
    assert "Authorization" not in headers


async def test_the_answer_is_asked_for_each_time_and_never_remembered(net: Net) -> None:
    transport = SdkRunnerTransport(net.url)
    net.info = ["ok", 503, "ok"]
    answers = [await transport.signer_readiness() for _ in range(3)]
    assert [answer.ready for answer in answers] == [True, False, True]
    assert net.log == ["signer info"] * 3


async def test_a_signer_that_went_away_is_not_ready_though_the_sdk_remembers_its_identity() -> None:
    async with identity_only_signer() as signer:
        url = str(signer.make_url("")).rstrip("/")
        await get_signer_info(url, None)  # what a paid job leaves behind
        transport = SdkRunnerTransport(url)
        assert (await transport.signer_readiness()).ready
    assert await transport.signer_readiness() == SignerReadiness(ready=False, reason="unavailable")


async def test_an_engine_with_no_signer_address_has_no_signer_to_ask() -> None:
    assert await SdkRunnerTransport(None).signer_readiness() == SignerReadiness(
        ready=False, reason="not_configured"
    )


@pytest.mark.parametrize("step", [500, 502, 503, "drop"])
async def test_a_signer_that_fails_or_drops_the_call_is_unavailable(net: Net, step: int | str) -> None:
    net.info = [step]
    assert await SdkRunnerTransport(net.url).signer_readiness() == SignerReadiness(
        ready=False, reason="unavailable"
    )


@pytest.mark.parametrize("address", ["a closed port", "ftp://signer.test:8935"])
async def test_a_signer_that_cannot_be_called_is_unavailable(address: str) -> None:
    """Nothing listens there, or the address is one the SDK cannot call at all."""
    url = closed_port_url() if address == "a closed port" else address
    assert await SdkRunnerTransport(url).signer_readiness() == SignerReadiness(
        ready=False, reason="unavailable"
    )


@pytest.mark.parametrize("step", [401, 403, 404, "malformed", "garbage", "array"])
async def test_a_signer_that_answers_with_no_identity_is_not_ready(net: Net, step: int | str) -> None:
    """A refusal, or a 200 that is not the signer's address and a signature: something answered at the
    address, and it did not identify itself as a signer that can sign."""
    net.info = [step]
    assert await SdkRunnerTransport(net.url).signer_readiness() == SignerReadiness(
        ready=False, reason="no_identity"
    )


async def test_the_answer_holds_no_address_and_no_signature(net: Net) -> None:
    """The identity call returns the wallet's address and a signature. Neither is in the answer."""
    readiness = await SdkRunnerTransport(net.url).signer_readiness()
    assert set(readiness.model_dump()) == {"ready", "reason"}
    assert "0x" not in readiness.model_dump_json()
