"""``FakeDiscoveryService`` serves a node-format document that the real reader reads back unchanged (D63)."""

from datetime import UTC, datetime
from decimal import Decimal

from livepeer_builder.adapters.sdk import HttpDiscoverySource
from livepeer_builder.contracts import PriceInfo, Runner, RunnerGPU
from livepeer_builder.testing import FAKE_DISCOVERY_URL, FakeDiscoveryService


async def test_every_runner_field_survives_the_document_and_the_reader() -> None:
    advertised = Runner(
        url="https://orch.example/apps/a/app",
        app="text-to-image",
        mode="single-shot",
        orchestrator_url="https://orch.example",
        capacity=4,
        capacity_used=1,
        capacity_available=3,
        price_info=PriceInfo(
            price=Decimal("9720691088783"),
            price_usd=Decimal("0.123456789012345678901234567890"),
            currency="wei",
            unit="fixed",
        ),
        observed_at=datetime(2026, 10, 22, 18, 5, 0, 123000, tzinfo=UTC),
        version="1.2.3",
        metadata='{"model":{"name":"example"}}',
        gpu=RunnerGPU(id="GPU-0", name="RTX 4090", vram_mb=24564),
    )
    client = FakeDiscoveryService([advertised]).client()
    try:
        (read,) = await HttpDiscoverySource(FAKE_DISCOVERY_URL, client=client).fetch()
    finally:
        await client.aclose()
    assert read == advertised


async def test_a_bare_runner_is_served_with_the_fields_the_schema_requires() -> None:
    client = FakeDiscoveryService([Runner(url="https://orch.example/app", app="chat")]).client()
    try:
        (read,) = await HttpDiscoverySource(FAKE_DISCOVERY_URL, client=client).fetch()
    finally:
        await client.aclose()
    assert (read.url, read.app, read.orchestrator_url) == (
        "https://orch.example/app",
        "chat",
        "https://orch.example",
    )
    assert read.observed_at is not None
