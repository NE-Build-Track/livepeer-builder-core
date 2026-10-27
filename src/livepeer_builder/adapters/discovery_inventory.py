"""Parse discovery inventories into engine contracts, independently of the SDK transport."""

from decimal import Decimal, InvalidOperation
from typing import Any

from livepeer_builder.contracts import NetworkRate, Runner, RunnerGPU


def runners_from_entries(entries: list[dict[str, Any]]) -> list[Runner]:
    runners: list[Runner] = []
    for entry in entries:
        orchestrator_url = _text(entry.get("address"))
        for raw in entry.get("runners") or ():
            runner = _runner_from_raw(raw, orchestrator_url)
            if runner is not None:
                runners.append(runner)
    return runners


def _runner_from_raw(raw: object, orchestrator_url: str | None) -> Runner | None:
    if not isinstance(raw, dict):
        return None
    url, app = _text(raw.get("url")), _text(raw.get("app"))
    if not url or not app:
        return None
    capacity = raw.get("capacity") if isinstance(raw.get("capacity"), int) else None
    sessions = raw.get("session_ids")
    available = capacity - len(sessions) if capacity is not None and isinstance(sessions, list) else None
    reported = raw.get("capacity_available")
    if isinstance(reported, int) and not isinstance(reported, bool):
        available = reported  # the node reports it directly; session_ids is the older form
    return Runner(
        url=url,
        app=app,
        runner_id=_text(raw.get("runner_id")),
        mode=_text(raw.get("mode")),
        orchestrator_url=orchestrator_url,
        capacity=capacity,
        capacity_available=available,
        rate=_rate(raw.get("price_info")),
        version=_text(raw.get("version")),
        metadata=_text(raw.get("metadata")),
        gpu=_gpu(raw.get("gpu")),
    )


def _gpu(value: object) -> RunnerGPU | None:
    if not isinstance(value, dict):
        return None
    vram = value.get("vram_mb")
    gpu = RunnerGPU(
        id=_text(value.get("id")),
        name=_text(value.get("name")),
        vram_mb=vram if isinstance(vram, int) and vram > 0 else None,
    )
    return gpu if (gpu.id or gpu.name or gpu.vram_mb) else None


def _rate(value: object) -> NetworkRate | None:
    if not isinstance(value, dict):
        return None
    try:
        amount = Decimal(str(value.get("price")))
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return NetworkRate(
        amount=amount,
        currency=str(value.get("currency") or "usd").lower(),
        unit=str(value.get("unit") or "hour").lower(),
    )


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
