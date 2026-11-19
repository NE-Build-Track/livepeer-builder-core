"""A clearinghouse's REST app on a loopback socket (#166), so the gateway SDK's own HTTP client can call
it as it calls a signer. An aiohttp server hands every request to the engine's app, in process, and
returns its answer. ``seen`` records what arrived, in order."""

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
from aiohttp import web
from aiohttp.test_utils import TestServer
from fastapi import FastAPI
from multidict import CIMultiDict


@dataclass
class Listening:
    url: str
    seen: list[tuple[str, CIMultiDict[str]]] = field(default_factory=list)  # (path, headers) of each request


@contextlib.asynccontextmanager
async def listening(app: FastAPI) -> AsyncIterator[Listening]:
    served = Listening(url="")
    inside = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://clearinghouse")

    async def handle(request: web.Request) -> web.Response:
        served.seen.append((request.path, CIMultiDict(request.headers)))
        headers = [(name, value) for name, value in request.headers.items() if name.lower() != "host"]
        answer = await inside.request(
            request.method, request.raw_path, content=await request.read(), headers=headers
        )
        kept = {name: value for name, value in answer.headers.items() if name.lower() != "content-length"}
        return web.Response(status=answer.status_code, body=answer.content, headers=kept)

    outside = web.Application()
    outside.router.add_route("*", "/{path:.*}", handle)
    async with inside, TestServer(outside) as server:
        served.url = str(server.make_url("")).rstrip("/")
        yield served
