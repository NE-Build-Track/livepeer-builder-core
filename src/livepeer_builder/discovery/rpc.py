"""``eth_call`` over JSON-RPC (``abi.EthCall``). Errors never include the RPC URL: providers put the
API key in it."""

import itertools

import httpx

from livepeer_builder.discovery.abi import ChainError


class JsonRpcChain:
    def __init__(self, url: str, *, client: httpx.AsyncClient | None = None, timeout_s: float = 10.0) -> None:
        self._url = url
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._ids = itertools.count(1)

    async def call(self, to: str, data: str) -> str:
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": "eth_call",
                "params": [{"to": to, "data": data}, "latest"]}  # fmt: skip
        try:
            response = await self._client.post(self._url, json=body)
        except httpx.HTTPError as exc:
            raise ChainError(f"RPC request failed: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise ChainError(f"RPC returned HTTP {response.status_code}")
        try:
            reply = response.json()
        except ValueError:
            raise ChainError("RPC returned a body that is not JSON") from None
        if not isinstance(reply, dict) or "error" in reply or not isinstance(reply.get("result"), str):
            error = reply.get("error") if isinstance(reply, dict) else None
            message = error.get("message") if isinstance(error, dict) else "no result"
            raise ChainError(f"RPC call failed: {message}")
        return str(reply["result"])

    async def aclose(self) -> None:
        await self._client.aclose()
