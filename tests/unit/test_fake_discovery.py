"""FakeDiscoveryService writes the optional runner fields the real reader stores."""

from datetime import UTC, datetime
from decimal import Decimal

from livepeer_builder.contracts import PriceInfo, Runner, RunnerGPU
from livepeer_builder.testing import FakeDiscoveryService

_STAMP = datetime(2026, 10, 22, 18, 5, tzinfo=UTC)


def test_optional_runner_fields_are_written_into_the_document() -> None:
    """version, metadata, capacity_used, gpu and price_usd are optional. A set field is served."""
    service = FakeDiscoveryService(
        [
            Runner(
                url="https://orch.example/app",
                app="a/b",
                mode="single-shot",
                orchestrator_url="https://orch.example",
                capacity=8,
                capacity_used=3,
                capacity_available=5,
                version="1.2.0",
                metadata='{"model_id":"qwen"}',
                gpu=RunnerGPU(id="0", name="H100", vram_mb=81920),
                price_info=PriceInfo(
                    price=Decimal(10),
                    price_usd=Decimal("0.0000027777777777777777777777777777"),
                    currency="wei",
                    unit="seconds",
                ),
                observed_at=_STAMP,
            )
        ]
    )
    (entry,) = service.document()
    (record,) = entry["runners"]
    assert entry["address"] == "https://orch.example"
    assert record["version"] == "1.2.0"
    assert record["metadata"] == '{"model_id":"qwen"}'
    assert record["capacity_used"] == 3
    assert record["gpu"] == {"id": "0", "name": "H100", "vram_mb": 81920}
    assert record["price_info"] == {
        "price": Decimal(10),
        "price_usd": Decimal("0.0000027777777777777777777777777777"),
        "currency": "wei",
        "unit": "seconds",
    }
