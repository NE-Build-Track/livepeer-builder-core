"""The protocol's own answer to "which orchestrators are there": the bonded transcoder pool on
Arbitrum, kept to those active in the current round and registered in the AI Service Registry.

An ``OrchestratorDirectory``, and a worked example of one that reads an external system of record.
"""

import asyncio
import logging

from livepeer_builder.discovery import abi
from livepeer_builder.discovery.directory import Orchestrator, base_url, unique

_LOG = logging.getLogger(__name__)

ARBITRUM_BONDING_MANAGER = "0x35bcf3c30594191d53231e4ff333e8a770453e40"  # BondingManager proxy
ARBITRUM_AI_SERVICE_REGISTRY = "0x04c0b249740175999e5bf5c9ac1da92431ef34c5"
_CONCURRENT_READS = 8
# livepeer-network-modules (Cloud-SPE) orchestrators register a signed manifest here instead of a base URL.
# It is another protocol (capability tuples, its own settlement), not livepeer-node's /discovery inventory.
_REGISTRY_MANIFEST = "/.well-known/livepeer-registry.json"


class OnChainDirectory:
    def __init__(
        self,
        chain: abi.EthCall,
        *,
        bonding_manager: str = ARBITRUM_BONDING_MANAGER,
        ai_service_registry: str = ARBITRUM_AI_SERVICE_REGISTRY,
    ) -> None:
        self._chain = chain
        self._bonding_manager = bonding_manager.lower()
        self._registry = ai_service_registry.lower()

    async def orchestrators(self) -> tuple[Orchestrator, ...]:
        pool = await self._pool()
        reads = asyncio.Semaphore(_CONCURRENT_READS)

        async def eligible(address: str) -> Orchestrator | None:
            async with reads:
                return await self._eligible(address)

        found = await asyncio.gather(*(eligible(address) for address in pool))
        return unique([orchestrator for orchestrator in found if orchestrator is not None])

    async def _pool(self) -> list[str]:
        """The pool is a linked list; walk at most its reported size, so a bad list cannot loop."""
        size = abi.decode_uint(await self._read(self._bonding_manager, abi.GET_TRANSCODER_POOL_SIZE))
        address = abi.decode_address(
            await self._read(self._bonding_manager, abi.GET_FIRST_TRANSCODER_IN_POOL)
        )
        pool: list[str] = []
        while address != abi.ZERO_ADDRESS and len(pool) < size:
            pool.append(address)
            data = abi.encode_call(abi.GET_NEXT_TRANSCODER_IN_POOL, address)
            address = abi.decode_address(await self._read(self._bonding_manager, data))
        return pool

    async def _eligible(self, address: str) -> Orchestrator | None:
        active = await self._read(self._bonding_manager, abi.encode_call(abi.IS_ACTIVE_TRANSCODER, address))
        if not abi.decode_bool(active):
            return None
        uri = abi.decode_string(
            await self._read(self._registry, abi.encode_call(abi.GET_SERVICE_URI, address))
        )
        if not uri:
            return None  # bonded and active, but not offering AI services
        if uri.rstrip("/").endswith(_REGISTRY_MANIFEST):
            _LOG.info(
                "orchestrator registered a network-modules manifest, which this service does not read",
                extra={"event": "discovery.registry_manifest_skipped", "orchestrator": address},
            )
            return None
        try:
            return Orchestrator(url=base_url(uri), address=address)
        except ValueError:
            _LOG.warning(
                "orchestrator registered an unusable AI service URI",
                extra={"event": "discovery.service_uri_invalid", "orchestrator": address},
            )
            return None

    async def _read(self, to: str, data: str) -> str:
        return await self._chain.call(to, data)
