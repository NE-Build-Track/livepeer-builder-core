"""An engine whose discovery source answers with documents a test writes, over HTTP (#114).

The tests built on this module state what the engine offers and does with an untrusted ``/discovery``
answer: the catalog, the runners an actor is given, health and the log. They name no reader function,
so they hold when the reader behind ``LPB_DISCOVERY_URL`` is replaced (ISS-48): only ``wire_engine``
wires the source.
"""

import dataclasses
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx

from livepeer_builder import BuilderEngine, EngineParts, EngineSupply
from livepeer_builder.adapters.sdk import HttpDiscoverySource
from livepeer_builder.contracts import ActorContext
from livepeer_builder.testing import FAKE_DISCOVERY_URL, make_engine

ORCHESTRATOR = "https://orch.example"
STAMP = "2026-10-22T17:05:00Z"
ALICE = ActorContext(actor_id="alice", permissions=frozenset({"discover", "jobs.run", "jobs.read"}))


def record(app: str = "good/app", url: str = f"{ORCHESTRATOR}/apps/good", **fields: Any) -> dict[str, Any]:
    """One runner record as livepeer/node serves it, with any other field given by name."""
    return {"url": url, "app": app, "observed_at": STAMP, **fields}


def document(*records: Any, address: str = ORCHESTRATOR) -> bytes:
    """One orchestrator entry holding ``records``. ``json.dumps`` escapes a lone surrogate as ``\\udXXX``,
    which is valid JSON, and a parser hands it back as a lone surrogate."""
    return json.dumps([{"address": address, "runners": list(records)}]).encode()


@asynccontextmanager
async def wire_engine(
    *answers: bytes, supply: EngineSupply | None = None, **settings: Any
) -> AsyncIterator[BuilderEngine]:
    """A real engine that has refreshed once from the first of ``answers``.

    Each later ``engine.discovery.refresh()`` reads the next answer; the last one repeats.
    """
    remaining = list(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=remaining.pop(0) if len(remaining) > 1 else remaining[0])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

        def over_the_wire(parts: EngineParts) -> EngineParts:
            source = HttpDiscoverySource(FAKE_DISCOVERY_URL, client=client)
            return dataclasses.replace(parts, discovery_source=source)

        async with make_engine(supply=supply, decorate=over_the_wire, **settings) as engine:
            yield engine


def skipped(records: Sequence[Any]) -> list[tuple[int, int]]:
    """(entries, runners) of every ``discovery.inventory_skipped`` line among the log ``records``."""
    return [
        (item.entries, item.runners)
        for item in records
        if getattr(item, "event", "") == "discovery.inventory_skipped"
    ]
