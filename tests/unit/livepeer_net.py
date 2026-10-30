"""A loopback Live Runner network for transport tests.

One aiohttp server plays the runner behind its orchestrator (a 402 challenge until a request carries a
payment) and the remote signer, so the engine's own call path (adapters/runner_call.py) and the SDK's
signer client run for real. ``log`` records what each party saw, in order.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer
from multidict import CIMultiDict

from livepeer_builder.contracts import NetworkRate, Runner

# What the signer does with one /generate-live-payment request: "ok", an HTTP status, "malformed" (no
# segCreds), "drop" (the connection closes before the reply) or a number of seconds to hang.
SignStep = str | int | float


@dataclass
class Reply:
    """What the runner answers a paid (or free) request with."""

    status: int = 200
    body: bytes = b'{"ok": true}'
    content_type: str = "application/json"
    chunks: tuple[bytes, ...] = ()  # streamed with a pause between them, after the headers
    hang_s: float = 0.0  # before the headers
    drop_after_chunks: bool = False
    drop_before_headers: bool = False  # the connection closes before any answer
    headers: dict[str, str] = field(default_factory=dict)
    header_pairs: tuple[tuple[str, str], ...] = ()  # repeated names allowed, on every kind of reply


@dataclass
class Net:
    server: TestServer
    paid: bool = True  # the runner answers 402 until a request carries a payment
    challenge_paid: bool = False  # answer 402 even to a paid request
    challenge_body: bytes | None = None  # replaces the 402's payment contract
    renewal_status: int = 200  # 404 says the orchestrator released the session
    sign: list[SignStep] = field(default_factory=list)
    # /sign-orchestrator-info: "ok", an HTTP status, "drop", "malformed" (no signature), or seconds to hang
    info: list[int | float | str] = field(default_factory=list)
    replies: list[Reply] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    bodies: list[Any] = field(default_factory=list)  # what each runner request carried, as JSON
    raw: list[bytes] = field(default_factory=list)  # ... and as the bytes on the wire
    headers: list[CIMultiDict[str]] = field(default_factory=list)  # ... and its request headers
    signer_requests: list[dict[str, Any]] = field(default_factory=list)
    release: asyncio.Event = field(default_factory=asyncio.Event)  # ends every hang at teardown
    on_challenge: Callable[[], Awaitable[None]] | None = None  # runs before each 402 challenge is sent
    _challenges: int = 0

    async def hang(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.release.wait(), timeout=seconds)

    @property
    def url(self) -> str:
        return str(self.server.make_url("")).rstrip("/")

    def runner(
        self, unit: str = "fixed", price: str = "0.10", orchestrator_url: str | None = None, name: str = ""
    ) -> Runner:
        return Runner(
            url=f"{self.url}/app" + (f"/{name}" if name else ""), app="example/echo", runner_id=name or "r1",
            mode="single-shot",
            orchestrator_url=orchestrator_url or self.url, capacity=1, capacity_available=1,
            rate=NetworkRate(amount=Decimal(price), currency="usd", unit=unit),
        )  # fmt: skip

    async def _sign_info(self, request: web.Request) -> web.Response:
        self.log.append("signer info")
        step = self.info.pop(0) if self.info else "ok"
        if isinstance(step, float):
            await self.hang(step)
            step = "ok"
        if step == "drop":
            assert request.transport is not None
            request.transport.close()
            return web.Response()
        if isinstance(step, int):
            return web.Response(status=step, text="UPSTREAM-INFO: signer auth webhook failed")
        if step == "malformed":
            return web.json_response({"address": "0x" + "ab" * 20})
        return web.json_response({"address": "0x" + "ab" * 20, "signature": "0xsig"})

    async def _generate(self, request: web.Request) -> web.StreamResponse:
        payload = await request.json()
        self.signer_requests.append(payload)
        manifest = payload["ManifestID"]
        step = self.sign.pop(0) if self.sign else "ok"
        self.log.append(f"sign {manifest}")
        if step == "malformed":
            return web.json_response({"payment": "p", "state": {"n": 1}})
        if step == "drop":
            assert request.transport is not None
            request.transport.close()
            return web.Response()
        if isinstance(step, int) and not isinstance(step, bool):
            return web.Response(status=step, text="UPSTREAM-SIGN: signer auth rejected request")
        if isinstance(step, float):
            await self.hang(step)
        return web.json_response(
            {"payment": f"pay-{manifest}", "segCreds": f"seg-{manifest}", "state": {"n": 1}}
        )

    async def _renewal(self, request: web.Request) -> web.Response:
        self.log.append(f"renewal {request.match_info['manifest']}")
        return web.Response(status=self.renewal_status)

    async def _app(self, request: web.Request) -> web.StreamResponse:
        raw = await request.read()
        self.raw.append(raw)
        self.headers.append(CIMultiDict(request.headers))
        try:
            self.bodies.append(json.loads(raw) if raw else None)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.bodies.append(None)
        payment = request.headers.get("Livepeer-Payment")
        if self.paid and (not payment or self.challenge_paid):
            if self.challenge_body is not None:
                return web.Response(status=402, body=self.challenge_body, content_type="application/json")
            if self.on_challenge is not None:
                await self.on_challenge()
            self._challenges += 1
            manifest = f"m-{self._challenges}"
            self.log.append(f"challenge {manifest}")
            body = {
                "payment_params": "params",
                "manifest_id": manifest,
                "payment_url": f"{self.url}/pay/{manifest}",
            }
            return web.json_response(body, status=402)
        self.log.append(f"request {payment or 'free'}")
        reply = self.replies.pop(0) if self.replies else Reply()
        if reply.hang_s:
            await self.hang(reply.hang_s)
        if reply.drop_before_headers:
            assert request.transport is not None
            request.transport.close()
            return web.Response()
        headers = CIMultiDict(
            [("Content-Type", reply.content_type), *reply.headers.items(), *reply.header_pairs]
        )
        if not reply.chunks:
            return web.Response(status=reply.status, body=reply.body, headers=headers)
        response = web.StreamResponse(status=reply.status, headers=headers)
        await response.prepare(request)
        for chunk in reply.chunks:
            await response.write(chunk)
            await asyncio.sleep(0.01)
        if reply.drop_after_chunks:
            assert request.transport is not None
            request.transport.close()
            return response
        await response.write_eof()
        return response


async def start() -> AsyncIterator[Net]:
    app = web.Application()
    holder: list[Net] = []

    def route(name: str) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
        async def handle(request: web.Request) -> web.StreamResponse:
            response: web.StreamResponse = await getattr(holder[0], name)(request)
            return response

        return handle

    app.router.add_post("/sign-orchestrator-info", route("_sign_info"))
    app.router.add_post("/generate-live-payment", route("_generate"))
    app.router.add_post("/pay/{manifest}", route("_renewal"))
    app.router.add_route("*", "/app{path:.*}", route("_app"))
    async with TestServer(app) as server:
        holder.append(Net(server))
        try:
            yield holder[0]
        finally:
            holder[0].release.set()


@contextlib.asynccontextmanager
async def identity_only_signer() -> AsyncIterator[TestServer]:
    """A signer that answers ``/sign-orchestrator-info`` until it is closed; after ``close()`` its port
    refuses connections, as a signer that went down after the engine cached its identity."""
    app = web.Application()

    async def info(_: web.Request) -> web.Response:
        return web.json_response({"address": "0x" + "cd" * 20, "signature": "0xsig"})

    app.router.add_post("/sign-orchestrator-info", info)
    async with TestServer(app) as server:
        yield server
