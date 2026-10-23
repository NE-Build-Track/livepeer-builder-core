"""Map a `/discover-orchestrators` document onto `Runner`.

Kept out of ``adapters/sdk.py`` so the gateway adapter stays within the file-size limit. This module
imports no gateway or HTTP client.
"""

import logging
from collections.abc import Sequence

from pydantic import ValidationError

from livepeer_builder.contracts import (
    DiscoveredOrchestrator,
    DiscoveredPriceInfo,
    DiscoveredRunner,
    NetworkRate,
    Runner,
    RunnerGPU,
)

_LOG = logging.getLogger(__name__)


class DiscoveryContractError(Exception):
    """Named orchestrators were present and none of them matched the contract."""


def runners_from_entries(entries: Sequence[object]) -> list[Runner]:
    """Map a `/discover-orchestrators` list onto `Runner`, one orchestrator at a time.

    Entries without an address are skipped. An orchestrator that does not match the contract is
    skipped and logged, and the rest are kept. Raises only when entries name orchestrators and none of
    them parses, so the caller keeps its last good snapshot.
    """
    runners: list[Runner] = []
    named = parsed = 0
    first_error: ValidationError | None = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        address = entry.get("address")
        if not isinstance(address, str) or not address.strip():
            continue
        named += 1
        try:
            discovered = DiscoveredOrchestrator.model_validate(entry)
        except ValidationError as error:
            first_error = first_error or error
            _LOG.warning(
                "orchestrator entry does not match the discovery contract",
                extra={
                    "event": "discovery.orchestrator_invalid",
                    "orchestrator": address.strip(),
                    "error": str(error),
                },
            )
            continue
        parsed += 1
        for raw in discovered.runners:
            runner = _runner_from_discovered(raw, address.strip())
            if runner is not None:
                runners.append(runner)
    if named and not parsed:
        raise DiscoveryContractError(f"no orchestrator entry matches the discovery contract: {first_error}")
    return runners


def _runner_from_discovered(raw: DiscoveredRunner, orchestrator_url: str) -> Runner | None:
    url, app = _text(raw.url), _text(raw.app)
    if not url or not app:
        return None
    available = raw.capacity_available
    if available is None and raw.capacity is not None and raw.session_ids is not None:
        available = raw.capacity - len(raw.session_ids)  # session_ids is the older form
    return Runner(
        url=url,
        app=app,
        runner_id=_text(raw.runner_id),
        mode=_text(raw.mode),
        orchestrator_url=orchestrator_url,
        capacity=raw.capacity,
        capacity_available=available,
        rate=_rate(raw.price_info),
        version=_text(raw.version),
        metadata=_text(raw.metadata),
        gpu=_gpu(raw.gpu),
    )


def _gpu(value: RunnerGPU | None) -> RunnerGPU | None:
    if value is None:
        return None
    gpu = RunnerGPU(
        id=_text(value.id),
        name=_text(value.name),
        vram_mb=value.vram_mb if isinstance(value.vram_mb, int) and value.vram_mb > 0 else None,
    )
    return gpu if (gpu.id or gpu.name or gpu.vram_mb) else None


def _rate(value: DiscoveredPriceInfo | None) -> NetworkRate | None:
    if value is None:
        return None
    return NetworkRate(amount=value.price, currency=value.currency, unit=value.unit)


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
