"""Reading each orchestrator's ``GET /discovery``, the inventory a livepeer-node orchestrator serves.

Only orchestrators whose certificate the system trust store accepts are kept: the engine calls their
runners over verified TLS, so one that is not trusted could never be used. Each answer becomes one
entry under the address we probed (an orchestrator cannot claim another's), with its runner records
passed on untouched for the engine to parse.
"""

import asyncio
import json
import logging
import ssl
from collections.abc import Sequence
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from livepeer_builder.discovery.directory import Orchestrator

_LOG = logging.getLogger(__name__)

Failure = Literal["untrusted_certificate", "failed"]


class Probe(BaseModel):
    """One orchestrator's answer: an entry, or why there is none."""

    model_config = ConfigDict(frozen=True)

    orchestrator: Orchestrator
    entry: dict[str, Any] | None = None
    failure: Failure | None = None


class Prober:
    def __init__(
        self,
        *,
        ssl_context: ssl.SSLContext | None = None,
        timeout_s: float = 5.0,
        max_bytes: int = 1 << 20,
        concurrency: int = 16,
    ) -> None:
        self._verify = ssl_context or ssl.create_default_context()  # the system trust store
        self._timeout_s = timeout_s
        self._max_bytes = max_bytes
        self._concurrency = concurrency

    async def probe_all(self, orchestrators: Sequence[Orchestrator]) -> list[Probe]:
        """Every orchestrator, at most ``concurrency`` at a time; results in the same order."""
        slots = asyncio.Semaphore(self._concurrency)
        async with self._client() as client:

            async def one(orchestrator: Orchestrator) -> Probe:
                async with slots:
                    return await self._probe(client, orchestrator)

            return list(await asyncio.gather(*(one(o) for o in orchestrators)))

    async def probe(self, orchestrator: Orchestrator) -> Probe:
        async with self._client() as client:
            return await self._probe(client, orchestrator)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(verify=self._verify, timeout=self._timeout_s, follow_redirects=False)

    async def _probe(self, client: httpx.AsyncClient, orchestrator: Orchestrator) -> Probe:
        try:
            async with asyncio.timeout(self._timeout_s):
                runners = await self._runners(client, orchestrator.url + "/discovery")
        except httpx.ConnectError as exc:
            return self._excluded(orchestrator, "untrusted_certificate" if _untrusted(exc) else "failed", exc)
        except (httpx.HTTPError, TimeoutError, ValueError) as exc:
            return self._excluded(orchestrator, "failed", exc)
        return Probe(orchestrator=orchestrator, entry={"address": orchestrator.url, "runners": runners})

    async def _runners(self, client: httpx.AsyncClient, url: str) -> list[dict[str, Any]]:
        async with client.stream("GET", url, headers={"Accept": "application/json"}) as response:
            if response.status_code != 200:
                raise ValueError(f"HTTP {response.status_code}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > self._max_bytes:
                    raise ValueError(f"inventory larger than {self._max_bytes} bytes")
        entries = json.loads(body)
        if not isinstance(entries, list):
            raise ValueError("inventory is not a JSON list")
        return [
            runner
            for entry in entries
            if isinstance(entry, dict)
            for runner in (entry.get("runners") or ())
            if isinstance(runner, dict)
        ]

    def _excluded(self, orchestrator: Orchestrator, reason: Failure, exc: BaseException) -> Probe:
        _LOG.warning(
            "orchestrator left out of discovery",
            extra={
                "event": "discovery.orchestrator_excluded",
                "orchestrator": orchestrator.url,
                "reason": reason,
                "error": f"{type(exc).__name__}: {exc}"[:200],
            },
        )
        return Probe(orchestrator=orchestrator, failure=reason)


def _untrusted(exc: BaseException) -> bool:
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, ssl.SSLCertVerificationError):
            return True
        seen = seen.__cause__ or seen.__context__
    return "CERTIFICATE_VERIFY_FAILED" in str(exc)
