"""The discovery service's settings, HTTP routes and composition root (#45)."""

import asyncio
import json
import socket
from pathlib import Path

import httpx
import pytest

from livepeer_builder.discovery import Catalog, OnChainDirectory, Orchestrator, Probe, StaticDirectory
from livepeer_builder.discovery.server import create_app, directory_from, serve
from livepeer_builder.discovery.settings import DiscoverySettings


def test_settings_come_from_lpd_variables_with_defaults() -> None:
    settings = DiscoverySettings.from_env({"LPD_ORCHESTRATORS": "https://a.example", "LPD_PROBE_INTERVAL_S": "5",
                                           "LPB_DISCOVERY_URL": "ignored: the engine's"})  # fmt: skip
    assert (settings.directory, settings.orchestrators, settings.probe_interval_s) == (
        "static",
        "https://a.example",
        5,
    )
    assert (settings.directory_interval_s, settings.max_failed_refreshes, settings.http_port) == (
        3600,
        6,
        8939,
    )


def test_each_directory_needs_its_input() -> None:
    with pytest.raises(ValueError, match="LPD_ORCHESTRATORS"):
        DiscoverySettings.from_env({})
    with pytest.raises(ValueError, match="LPD_ARBITRUM_RPC_URL"):
        DiscoverySettings.from_env({"LPD_DIRECTORY": "onchain"})
    with pytest.raises(ValueError, match="directory"):
        DiscoverySettings.from_env({"LPD_DIRECTORY": "cmdb", "LPD_ORCHESTRATORS": "https://a.example"})


def test_the_rpc_url_is_secret() -> None:
    settings = DiscoverySettings.from_env(
        {"LPD_DIRECTORY": "onchain", "LPD_ARBITRUM_RPC_URL": "https://arb/key"}
    )
    assert "key" not in repr(settings)


def test_the_on_chain_contracts_default_to_the_controller_and_the_ai_registry() -> None:
    settings = DiscoverySettings(directory="onchain", arbitrum_rpc_url="https://arb.example")
    assert settings.controller == "0xd8e8328501e9645d16cf49539efc04f734606ee4"
    assert (
        settings.service_registry == "0x04c0b249740175999e5bf5c9ac1da92431ef34c5"
    )  # the AI registry override
    moved = DiscoverySettings.from_env(
        {"LPD_DIRECTORY": "onchain", "LPD_ARBITRUM_RPC_URL": "https://arb", "LPD_SERVICE_REGISTRY": "controller",
         "LPD_CONTROLLER": "0xD8E8328501E9645d16Cf49539efC04f734606ee4"}
    )  # fmt: skip
    assert (moved.controller, moved.service_registry) == (
        "0xd8e8328501e9645d16cf49539efc04f734606ee4",
        "controller",
    )
    for bad in (
        {"controller": "controller"},
        {"service_registry": "0x123"},
        {"controller": "not an address"},
    ):
        with pytest.raises(ValueError, match="address"):
            DiscoverySettings(directory="onchain", arbitrum_rpc_url="https://arb", **bad)


def test_the_settings_choose_the_directory(tmp_path: Path) -> None:
    assert isinstance(directory_from(DiscoverySettings(orchestrators="https://a.example")), StaticDirectory)
    onchain = DiscoverySettings(directory="onchain", arbitrum_rpc_url="https://arb.example")
    assert isinstance(directory_from(onchain), OnChainDirectory)


class Fixed:
    """A prober whose runners are the JSON object text a real probe would have stored."""

    def __init__(self, *runners: str) -> None:
        self.runners = list(runners)

    async def probe_all(self, orchestrators):  # type: ignore[no-untyped-def]
        return [
            Probe(orchestrator=o, entry={"address": o.url, "runners": self.runners}) for o in orchestrators
        ]


def client(catalog: Catalog) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(catalog)), base_url="http://discovery"
    )


async def test_discovery_serves_the_inventory_and_readiness_follows_the_catalog() -> None:
    runner = {"url": "https://a.example/apps/r1/app", "app": "example/echo"}
    catalog = Catalog(StaticDirectory(["https://a.example"]), Fixed(json.dumps(runner)))
    async with client(catalog) as http:
        assert (await http.get("/healthz")).status_code == 200
        not_ready = await http.get("/readyz")
        assert not_ready.status_code == 503 and not_ready.json()["ready"] is False
        assert (await http.get("/discovery")).json() == []
        await catalog.load_directory()
        await catalog.probe()
        ready = await http.get("/readyz")
        assert ready.status_code == 200 and ready.json()["serving"] == 1
        assert (await http.get("/discovery")).json() == [
            {"address": "https://a.example", "runners": [runner]}
        ]


async def test_discovery_serves_the_stored_runner_text_unchanged() -> None:
    """Number tokens stored from the probe are written back as those tokens, not quoted or rewritten."""
    price_usd = "0.0000027777777777777777777777777777"
    runner = (
        '{"url":"https://a.example/apps/r1/app","app":"example/echo","price_info":'
        f'{{"price":4492197255,"price_usd":{price_usd},"tiny":1e-999999}}}}'
    )
    catalog = Catalog(StaticDirectory(["https://a.example"]), Fixed(runner))
    await catalog.load_directory()
    await catalog.probe()
    async with client(catalog) as http:
        body = (await http.get("/discovery")).content
    assert f'"price_usd":{price_usd}'.encode() in body
    assert b'"tiny":1e-999999' in body
    (entry,) = json.loads(body)
    assert entry["address"] == "https://a.example"
    assert entry["runners"] == [json.loads(runner)]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def test_serve_runs_the_catalog_and_the_listener_until_cancelled() -> None:
    port = free_port()
    settings = DiscoverySettings(
        orchestrators="https://unused.example", http_port=port, probe_interval_s=0.05
    )

    class One:
        async def orchestrators(self) -> tuple[Orchestrator, ...]:
            return (Orchestrator(url="https://a.example"),)

    task = asyncio.create_task(
        serve(settings, directory=One(), prober=Fixed(json.dumps({"url": "u", "app": "a"})))
    )
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http, asyncio.timeout(5):
            while True:
                try:
                    if (await http.get("/readyz")).status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                await asyncio.sleep(0.05)
            assert (await http.get("/discovery")).json() == [
                {"address": "https://a.example", "runners": [{"url": "u", "app": "a"}]}
            ]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_readiness_fails_once_the_refresh_task_has_stopped() -> None:
    """A stopped refresh would serve a frozen inventory while /readyz said 200 (#46 review)."""
    catalog = Catalog(StaticDirectory(["https://a.example"]), Fixed(json.dumps({"url": "u", "app": "a"})))
    await catalog.load_directory()
    await catalog.probe()

    async def crashes() -> None:
        raise RuntimeError("the loop died")

    refresh = asyncio.create_task(crashes())
    await asyncio.sleep(0)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(catalog, refresh=refresh)), base_url="http://discovery"
    ) as http:
        answer = await http.get("/readyz")
    assert answer.status_code == 503 and answer.json()["refreshing"] is False
    assert (await http_status(catalog)) == 200  # without a refresh task to watch, the catalog decides


async def http_status(catalog: Catalog) -> int:
    async with client(catalog) as http:
        return (await http.get("/readyz")).status_code
