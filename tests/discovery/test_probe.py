"""Probing an orchestrator's GET /discovery: trusted TLS only, bounded, and its runners kept as they are (#45)."""

import asyncio
import json
from decimal import Decimal

import pytest
from aiohttp import web

from livepeer_builder.discovery import Orchestrator, Prober
from livepeer_builder.discovery.probe import public_address
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


async def test_a_runner_record_is_kept_as_the_orchestrator_spelled_it(serve: Serve, tls: Tls) -> None:
    """The service forwards the runner object. It does not parse its numbers and write them back."""
    runner = (
        '{"url":"https://orch/apps/r1/app","app":"example/echo",'
        '"price_info":{"price":4492197255,"price_usd":1e-999999}}'
    )
    payload = f'[{{"address":"https://claims-elsewhere","runners":[{runner}]}}]'.encode()

    async def handler(_: web.Request) -> web.StreamResponse:
        return web.Response(body=payload, content_type="application/json")

    url = await serve(handler, tls.trusted)
    probe = await Prober(ssl_context=tls.client).probe(Orchestrator(url=url))
    assert probe.failure is None and probe.entry == {"address": url, "runners": [runner]}


async def test_a_node_price_usd_stays_a_decimal_through_the_probe(serve: Serve, tls: Tls) -> None:
    payload = (
        b'[{"address":"https://claims-elsewhere","runners":[{"url":"https://orch/apps/r1/app",'
        b'"app":"example/echo","price_info":{"price":4492197255,'
        b'"price_usd":0.0000027777777777777777777777777777,"currency":"wei","unit":"seconds"}}]}]'
    )

    async def handler(_: web.Request) -> web.StreamResponse:
        return web.Response(body=payload, content_type="application/json")

    url = await serve(handler, tls.trusted)
    probe = await Prober(ssl_context=tls.client).probe(Orchestrator(url=url))
    assert probe.failure is None and probe.entry is not None
    (runner,) = probe.entry["runners"]
    price_usd = "0.0000027777777777777777777777777777"
    assert f'"price_usd":{price_usd}' in runner  # the token, not a float of it
    price_info = json.loads(runner, parse_float=Decimal)["price_info"]
    assert price_info["price"] == 4492197255
    assert price_info["price_usd"] == Decimal(price_usd)


async def test_a_trusted_orchestrator_yields_one_entry_under_its_probed_address(
    serve: Serve, tls: Tls
) -> None:
    other = {"url": "https://orch/apps/r2/app", "app": "other/app", "future_field": True}
    url = await serve(answering([{"address": "https://claims-elsewhere", "runners": [RUNNER]}, {"runners": [other]}]),
                      tls.trusted)  # fmt: skip
    probe = await Prober(ssl_context=tls.client).probe(Orchestrator(url=url))
    assert probe.failure is None and probe.entry is not None
    assert probe.entry["address"] == url  # the address we probed, not the one the body claimed
    assert [json.loads(runner) for runner in probe.entry["runners"]] == [RUNNER, other]


async def test_a_self_signed_orchestrator_is_excluded_as_untrusted(serve: Serve, tls: Tls) -> None:
    url = await serve(answering([{"address": "x", "runners": [RUNNER]}]), tls.self_signed)
    probe = await Prober(ssl_context=tls.client).probe(Orchestrator(url=url))
    assert (probe.entry, probe.failure) == (None, "untrusted_certificate")


async def test_nan_or_infinity_is_not_json_and_fails_the_probe(serve: Serve, tls: Tls) -> None:
    """``NaN`` and ``Infinity`` are not JSON. The probe refuses the body rather than forwarding them."""
    prober = Prober(ssl_context=tls.client)
    for token in (b"NaN", b"Infinity", b"-Infinity"):
        body = (
            b'[{"runners":[{"url":"https://orch/apps/r1/app","app":"a/b","price_info":{"price_usd":%s}}]}]'
            % token
        )

        async def handler(_: web.Request, body: bytes = body) -> web.StreamResponse:
            return web.Response(body=body, content_type="application/json")

        url = await serve(handler, tls.trusted)
        probe = await prober.probe(Orchestrator(url=url))
        assert (probe.entry, probe.failure) == (None, "failed"), token


async def test_a_number_too_large_to_reparse_fails_only_that_orchestrator(serve: Serve, tls: Tls) -> None:
    """``1e1000000000000000000`` is ``inf`` as a float and not a ``Decimal``. Forwarding it would fail
    every engine refresh, so this orchestrator is left out and the next one is kept."""
    poison = b'[{"runners":[{"url":"https://orch/apps/r1/app","app":"a/b","price":1e1000000000000000000}]}]'

    async def poisoned(_: web.Request) -> web.StreamResponse:
        return web.Response(body=poison, content_type="application/json")

    bad = await serve(poisoned, tls.trusted)
    good = await serve(answering([{"runners": [RUNNER]}]), tls.trusted)
    probes = await Prober(ssl_context=tls.client).probe_all([Orchestrator(url=bad), Orchestrator(url=good)])
    assert (probes[0].entry, probes[0].failure) == (None, "failed")
    assert probes[1].failure is None and probes[1].entry is not None
    assert json.loads(probes[1].entry["runners"][0])["url"] == RUNNER["url"]


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


async def test_the_default_trust_store_is_the_deployments_so_pinned_certificates_count(
    serve: Serve, tls: Tls, monkeypatch: pytest.MonkeyPatch
) -> None:  # a deployment that pins orchestrator certificates (SSL_CERT_FILE / SSL_CERT_DIR) keeps them
    url = await serve(answering([{"runners": [RUNNER]}]), tls.trusted)
    assert (
        await Prober().probe(Orchestrator(url=url))
    ).failure == "untrusted_certificate"  # system roots only
    monkeypatch.setenv("SSL_CERT_FILE", str(tls.ca_file))
    assert (await Prober().probe(Orchestrator(url=url))).failure is None


# -- PR #46 review ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "runners", [1, True, False, "abc", {"url": "u"}], ids=["int", "true", "false", "string", "object"]
)
async def test_a_runners_value_that_is_not_a_list_fails_the_probe(
    serve: Serve, tls: Tls, runners: object
) -> None:
    """It must not abort the pass (an int raised TypeError), nor read as an empty inventory that replaces the
    orchestrator's last good one (a string or false did)."""
    url = await serve(answering([{"address": "x", "runners": runners}]), tls.trusted)
    probe = await Prober(ssl_context=tls.client).probe(Orchestrator(url=url))
    assert (probe.entry, probe.failure) == (None, "failed")


async def test_null_runners_is_an_orchestrator_with_none(serve: Serve, tls: Tls) -> None:
    url = await serve(
        answering([{"address": "x", "runners": None}]), tls.trusted
    )  # Go marshals a nil slice as null
    probe = await Prober(ssl_context=tls.client).probe(Orchestrator(url=url))
    assert probe.failure is None and probe.entry == {"address": url, "runners": []}


async def test_one_orchestrators_unexpected_error_never_aborts_the_pass(
    serve: Serve, tls: Tls, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = await serve(answering([{"runners": [RUNNER]}]), tls.trusted)
    bad = await serve(answering([{"runners": [RUNNER]}]), tls.trusted)
    real = Prober._runners

    async def broken(self: Prober, client, url: str, *rest):  # type: ignore[no-untyped-def]
        if url.startswith(bad):
            raise RuntimeError("something nobody expected")
        return await real(self, client, url, *rest)

    monkeypatch.setattr(Prober, "_runners", broken)
    probes = await Prober(ssl_context=tls.client).probe_all([Orchestrator(url=bad), Orchestrator(url=good)])
    assert [p.failure for p in probes] == ["failed", None]


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True), ("2606:4700:4700::1111", True),
        ("10.0.0.1", False), ("172.16.0.1", False), ("192.168.1.1", False), ("127.0.0.1", False),
        ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False), ("224.0.0.1", False),  # noqa: S104 - data, not a bind
        ("::1", False), ("fd12:3456::1", False), ("fe80::1", False), ("::ffff:10.0.0.1", False),
        ("64:ff9b::a9fe:a9fe", False), ("64:ff9b::7f00:1", False), ("64:ff9b::808:808", True),  # NAT64
        ("2002:a9fe:a9fe::1", False), ("2002:808:808::1", True),  # 6to4
        ("fec0::1", False), ("::7f00:1", False),  # site-local, IPv4-compatible
    ],
)  # fmt: skip
def test_only_public_addresses_are_probed_for_untrusted_sources(address: str, public: bool) -> None:
    assert public_address(address) is public


async def test_an_on_chain_uri_resolving_to_a_private_address_is_never_contacted(
    serve: Serve, tls: Tls
) -> None:
    hits: list[str] = []

    async def counted(request: web.Request) -> web.StreamResponse:
        hits.append(request.path)
        return web.json_response([{"runners": [RUNNER]}])

    url = await serve(counted, tls.trusted)  # https://127.0.0.1:port, a private address
    probe = await Prober(ssl_context=tls.client).probe(Orchestrator(url=url, public_only=True))
    assert (probe.entry, probe.failure, hits) == (None, "private_address", [])


async def test_an_on_chain_uri_is_fetched_from_the_address_that_was_checked(serve: Serve, tls: Tls) -> None:
    """The name is resolved once, checked, and that address is the one connected to (no second lookup to
    rebind), with TLS still verified against the name."""
    hosts: list[str] = []

    async def seen(request: web.Request) -> web.StreamResponse:
        hosts.append(request.headers["Host"])
        return web.json_response([{"runners": [RUNNER]}])

    port = (await serve(seen, tls.named)).rsplit(":", 1)[1]  # its certificate names orch.test, no address
    looked_up: list[str] = []

    async def resolve(host: str, port: int) -> list[str]:
        looked_up.append(host)
        return ["127.0.0.1"]  # orch.test exists only here; the certificate names it

    prober = Prober(ssl_context=tls.client, resolve=resolve, address_allowed=lambda address: True)
    url = f"https://orch.test:{port}"
    probe = await prober.probe(Orchestrator(url=url, public_only=True))
    assert probe.failure is None and probe.entry is not None
    assert probe.entry["address"] == url
    assert [json.loads(runner) for runner in probe.entry["runners"]] == [RUNNER]
    assert looked_up == ["orch.test"] and hosts == [f"orch.test:{port}"]


async def test_a_name_that_does_not_resolve_fails(tls: Tls) -> None:
    async def nothing(host: str, port: int) -> list[str]:
        raise OSError("no such host")

    probe = await Prober(ssl_context=tls.client, resolve=nothing).probe(
        Orchestrator(url="https://gone.example", public_only=True)
    )
    assert probe.failure == "failed"


@pytest.mark.parametrize(
    "addresses", [["127.0.0.2", "127.0.0.1"], ["127.0.0.1", "127.0.0.2"]], ids=["dead first", "dead last"]
)
async def test_each_checked_address_is_tried_in_order_and_no_other(
    serve: Serve, tls: Tls, addresses: list[str]
) -> None:
    """A dead address (often an AAAA listed first) must not fail an orchestrator another address serves."""
    port = (await serve(answering([{"runners": [RUNNER]}]), tls.named)).rsplit(":", 1)[1]

    async def resolve(host: str, port: int) -> list[str]:
        return addresses  # nothing listens on .2, so it refuses at once

    prober = Prober(ssl_context=tls.client, resolve=resolve, address_allowed=lambda address: True)
    probe = await prober.probe(Orchestrator(url=f"https://orch.test:{port}", public_only=True))
    assert probe.failure is None


async def test_one_non_public_address_refuses_the_name_even_beside_public_ones(tls: Tls) -> None:
    async def resolve(host: str, port: int) -> list[str]:
        return ["8.8.8.8", "169.254.169.254"]

    probe = await Prober(ssl_context=tls.client, resolve=resolve).probe(
        Orchestrator(url="https://orch.test", public_only=True)
    )
    assert probe.failure == "private_address"


async def test_a_silent_first_address_does_not_use_up_the_probe(serve: Serve, tls: Tls) -> None:
    """A dead AAAA usually drops packets rather than refusing. The next checked address must still be
    tried within the probe's time (#46 review)."""
    url = await serve(answering([{"runners": [RUNNER]}]), tls.named)
    port = int(url.rsplit(":", 1)[1])

    async def hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(30)  # accepts, then never answers the TLS handshake

    silent = await asyncio.start_server(hold, "127.0.0.3", port)

    async def resolve(host: str, port: int) -> list[str]:
        return ["127.0.0.3", "127.0.0.1"]

    prober = Prober(
        ssl_context=tls.client, timeout_s=3, resolve=resolve, address_allowed=lambda address: True
    )
    try:
        async with asyncio.timeout(10):
            probe = await prober.probe(Orchestrator(url=f"https://orch.test:{port}", public_only=True))
    finally:
        silent.close()
    assert probe.failure is None
