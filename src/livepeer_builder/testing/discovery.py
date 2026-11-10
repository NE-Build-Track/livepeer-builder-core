"""``FakeDiscoveryService``: a node-format ``/discovery`` document served to the real HTTP reader."""

import dataclasses
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from livepeer_builder.adapters.sdk import HttpDiscoverySource, memory_discovery_client
from livepeer_builder.contracts import PriceInfo, Runner
from livepeer_builder.runtime.assembly import Decorate, EngineParts

FAKE_DISCOVERY_URL = "http://discovery.test/discovery"
_STAMP = datetime(2026, 10, 22, 17, 5, 0, 123000, tzinfo=UTC)
_DECIMAL = re.compile(r'"__DEC__([^"]*)"')


def _token(value: Decimal) -> str:
    if value == value.to_integral_value():
        return format(value.quantize(Decimal(1)), "f")
    return format(value, "f")


def _dump(value: object) -> bytes:
    """JSON with each ``Decimal`` written as its exact token, never as a binary float."""

    def tag(item: object) -> object:
        if isinstance(item, Decimal):
            return f"__DEC__{_token(item)}"
        if isinstance(item, dict):
            return {key: tag(child) for key, child in item.items()}
        if isinstance(item, list):
            return [tag(child) for child in item]
        return item

    text = _DECIMAL.sub(r"\1", json.dumps(tag(value)))
    return text.encode()


class FakeDiscoveryService:
    """Serves ``runners`` as a schema-shaped document through an in-memory HTTP client.

    Schema-required fields, including ``observed_at``, are filled when a runner omits them. The real
    ``HttpDiscoverySource`` reads the document; this class is not a ``DiscoverySource``.
    """

    def __init__(self, runners: Sequence[Runner] = ()) -> None:
        self.runners = tuple(runners)

    def document(self) -> list[dict[str, object]]:
        grouped: dict[str, list[dict[str, object]]] = {}
        for item in self.runners:
            address = item.orchestrator_url or "https://orch.example"
            grouped.setdefault(address, []).append(_runner_record(item))
        return [{"address": address, "runners": records} for address, records in grouped.items()]

    def client(self) -> Any:
        return memory_discovery_client(_dump(self.document()))


def _runner_record(item: Runner) -> dict[str, object]:
    stamp = (
        item.observed_at.astimezone(UTC)
        if item.observed_at is not None and item.observed_at.utcoffset() is not None
        else _STAMP
    )
    record: dict[str, object] = {
        "url": item.url,
        "app": item.app,
        "observed_at": stamp.isoformat().replace("+00:00", "Z"),
    }
    if item.mode is not None:
        record["mode"] = item.mode
    if item.version is not None:
        record["version"] = item.version
    if item.metadata is not None:
        record["metadata"] = item.metadata
    if item.capacity is not None:
        record["capacity"] = item.capacity
    if item.capacity_used is not None:
        record["capacity_used"] = item.capacity_used
    if item.capacity_available is not None:
        record["capacity_available"] = item.capacity_available
    if item.gpu is not None:
        gpu: dict[str, object] = {}
        if item.gpu.id is not None:
            gpu["id"] = item.gpu.id
        if item.gpu.name is not None:
            gpu["name"] = item.gpu.name
        if item.gpu.vram_mb is not None:
            gpu["vram_mb"] = item.gpu.vram_mb
        record["gpu"] = gpu
    if item.price_info is not None:
        record["price_info"] = _price(item.price_info)
    return record


def _price(info: PriceInfo) -> dict[str, object]:
    advertised: dict[str, object] = {}
    if info.price is not None:
        advertised["price"] = info.price
    if info.price_usd is not None:
        advertised["price_usd"] = info.price_usd
    if info.currency is not None:
        advertised["currency"] = info.currency
    if info.unit is not None:
        advertised["unit"] = info.unit
    return advertised


def serve(runners: Sequence[Runner]) -> tuple[Decorate, Any]:
    """A decorate that installs the real reader over ``runners``, and the client the caller closes."""
    client = FakeDiscoveryService(runners).client()

    def decorate(parts: EngineParts) -> EngineParts:
        return dataclasses.replace(
            parts, discovery_source=HttpDiscoverySource(FAKE_DISCOVERY_URL, client=client)
        )

    return decorate, client
