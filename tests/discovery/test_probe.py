"""Probing an orchestrator's GET /discovery: trusted TLS only, bounded, and its runners kept as they are (#45)."""

import asyncio
import json

from aiohttp import web

from livepeer_builder.discovery import Orchestrator, Prober
from tests.discovery.conftest import Handler, Serve, Tls

RUNNER = {
    "url": "https://orch/apps/r1/app",
    "app": "example/echo",
    "capacity": 2,
    "price_info": {"price": "10"},
}


def answering(body: object, status: int = 200) -> Handler:
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.json_response(body, status=status)

    return handler


async def test_a_trusted_orchestrator_yields_one_entry_under_its_probed_address(
    serve: Serve, tls: Tls
) -> None:
    other = {"url": "https://orch/apps/r2/app", "app": "other/app", "future_field": True}
    url = await serve(answering([{"address": "https://claims-elsewhere", "runners": [RUNNER]}, {"runners": [other]}]),
                      tls.trusted)  # fmt: skip
    probe = await Prober(ssl_context=tls.client).probe(Orchestrator(url=url))
    assert probe.failure is None
    assert probe.entry == {
        "address": url,
        "runners": [RUNNER, other],
    }  # our address; runner records untouched


async def test_a_self_signed_orchestrator_is_excluded_as_untrusted(serve: Serve, tls: Tls) -> None:
    url = await serve(answering([{"address": "x", "runners": [RUNNER]}]), tls.self_signed)
    probe = await Prober(ssl_context=tls.client).probe(Orchestrator(url=url))
    assert (probe.entry, probe.failure) == (None, "untrusted_certificate")


async def test_bad_answers_fail(serve: Serve, tls: Tls) -> None:
    async def slow(request: web.Request) -> web.StreamResponse:
        await asyncio.sleep(1)
        return web.json_response([])

    async def huge(request: web.Request) -> web.StreamResponse:
        return web.Response(body=json.dumps([{"runners": [RUNNER] * 20000}]), content_type="application/json")

    async def garbage(request: web.Request) -> web.StreamResponse:
        return web.Response(text="<html>", content_type="text/html")

    prober = Prober(ssl_context=tls.client, timeout_s=0.3, max_bytes=64 * 1024)
    for handler in (answering([], status=500), answering({"runners": []}), slow, huge, garbage):
        url = await serve(handler, tls.trusted)
        probe = await prober.probe(Orchestrator(url=url))
        assert (probe.entry, probe.failure) == (None, "failed"), handler


async def test_an_empty_inventory_is_a_success(serve: Serve, tls: Tls) -> None:
    url = await serve(answering([{"address": "x", "runners": []}]), tls.trusted)
    probe = await Prober(ssl_context=tls.client).probe(Orchestrator(url=url))
    assert probe.failure is None and probe.entry == {"address": url, "runners": []}


async def test_unreachable_is_a_failure_and_probing_many_keeps_their_order(serve: Serve, tls: Tls) -> None:
    good = await serve(answering([{"runners": [RUNNER]}]), tls.trusted)
    down = Orchestrator(url="https://127.0.0.1:9")  # nothing listens on the discard port
    probes = await Prober(ssl_context=tls.client, timeout_s=1).probe_all([down, Orchestrator(url=good)])
    assert [(p.orchestrator.url, p.failure) for p in probes] == [(down.url, "failed"), (good, None)]
