"""The discovery service's settings, HTTP routes and composition root (#45)."""

import asyncio
import socket
from pathlib import Path
from typing import Any

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


def test_the_settings_choose_the_directory(tmp_path: Path) -> None:
    assert isinstance(directory_from(DiscoverySettings(orchestrators="https://a.example")), StaticDirectory)
    onchain = DiscoverySettings(directory="onchain", arbitrum_rpc_url="https://arb.example")
    assert isinstance(directory_from(onchain), OnChainDirectory)


class Fixed:
    def __init__(self, *runners: dict[str, Any]) -> None:
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
    catalog = Catalog(StaticDirectory(["https://a.example"]), Fixed(runner))
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

    task = asyncio.create_task(serve(settings, directory=One(), prober=Fixed({"url": "u", "app": "a"})))
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
