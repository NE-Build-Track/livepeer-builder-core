"""The Arbitrum JSON-RPC client behind OnChainDirectory: plain eth_call over httpx (#45)."""

import json
import logging
from typing import Any

import httpx
import pytest

from livepeer_builder.discovery.abi import ChainError
from livepeer_builder.discovery.rpc import JsonRpcChain

RPC = "https://arb.example/rpc/key"


def chain(handler) -> JsonRpcChain:  # type: ignore[no-untyped-def]
    return JsonRpcChain(RPC, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_eth_call_against_the_latest_block_returns_the_result() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": seen[-1]["id"], "result": "0x" + "00" * 31 + "01"}
        )

    assert await chain(handler).call("0xabc", "0x08802374") == "0x" + "00" * 31 + "01"
    [body] = seen
    assert body["method"] == "eth_call" and body["params"] == [
        {"to": "0xabc", "data": "0x08802374"},
        "latest",
    ]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "execution reverted"}}
        ),
        httpx.Response(429, text="rate limited"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 1}),
    ],
)
async def test_any_failure_is_a_chain_error_without_the_url(response: httpx.Response) -> None:
    with pytest.raises(ChainError) as raised:
        await chain(lambda request: response).call("0xabc", "0x00")
    assert "key" not in str(raised.value)  # the RPC URL often carries the provider key


async def test_a_transport_failure_is_a_chain_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ChainError):
        await chain(handler).call("0xabc", "0x00")


async def test_a_json_rpc_error_keeps_its_code_and_drops_the_providers_text() -> None:
    """The text can echo the URL or key, and ChainError reaches /readyz (#46 review)."""
    error = {"code": -32000, "message": "execution reverted for https://arb.example/rpc/key"}
    response = httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": error})
    with pytest.raises(ChainError) as raised:
        await chain(lambda request: response).call("0xabc", "0x00")
    assert str(raised.value) == "RPC call failed: JSON-RPC error -32000"


async def test_the_rpc_url_never_reaches_the_log(capsys: pytest.CaptureFixture[str]) -> None:
    """httpx logs each request at INFO with its URL; the service's logging keeps httpx at WARNING."""
    from livepeer_builder.runtime import configure_logging

    configure_logging(json_lines=True)
    try:
        ok = httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x" + "00" * 32})
        await chain(lambda request: ok).call("0xabc", "0x00")
        assert "key" not in capsys.readouterr().err
    finally:
        logging.getLogger().handlers.clear()
