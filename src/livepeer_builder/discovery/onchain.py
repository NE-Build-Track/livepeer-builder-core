"""The protocol's own answer to "which orchestrators are there": the bonded transcoder pool on
Arbitrum, kept to those active in the current round and registered in a service registry.

Both contracts are looked up in the protocol's Controller, so an upgrade is followed. The registry read is
the AI Service Registry by default: the Controller does not know it (it was deployed beside the Controller,
not registered in it), so it is an override of the Controller's ``ServiceRegistry``, the one the protocol
uses for transcoding. ``service_registry=None`` reads the Controller's.

A registration is not operator input. Each URI must be https and is marked ``public_only`` for the
prober. An orchestrator whose own record cannot be read (a revert, a URI that is not UTF-8) is skipped,
never the whole directory (#46 review). A failure of the RPC itself (a rate limit, a 5xx) is retried and,
if it persists, fails the load, so the previous complete directory is kept rather than a quietly smaller
one. The walk is capped at ``max_pool`` whatever size the pool reports.

An ``OrchestratorDirectory``, and a worked example of one that reads an external system of record.
"""

import asyncio
import logging

from livepeer_builder.discovery import abi
from livepeer_builder.discovery.directory import Orchestrator, base_url, unique

_LOG = logging.getLogger(__name__)

ARBITRUM_CONTROLLER = "0xd8e8328501e9645d16cf49539efc04f734606ee4"
ARBITRUM_AI_SERVICE_REGISTRY = "0x04c0b249740175999e5bf5c9ac1da92431ef34c5"
MAX_POOL = 1000  # the BondingManager pool is capped by governance (100 today); this bounds a bad answer
_CONCURRENT_READS = 8
# livepeer-network-modules (Cloud-SPE) orchestrators register a signed manifest here instead of a base URL.
# It is another protocol (capability tuples, its own settlement), not livepeer-node's /discovery inventory.
_REGISTRY_MANIFEST = "/.well-known/livepeer-registry.json"


class OnChainDirectory:
    def __init__(
        self,
        chain: abi.EthCall,
        *,
        controller: str = ARBITRUM_CONTROLLER,
        service_registry: str | None = ARBITRUM_AI_SERVICE_REGISTRY,
        max_pool: int = MAX_POOL,
        retry_delays_s: tuple[float, ...] = (0.5, 2.0),
    ) -> None:
        self._chain = chain
        self._controller = controller.lower()
        self._registry = service_registry.lower() if service_registry else None
        self._max_pool = max_pool
        self._retry_delays_s = retry_delays_s

    async def orchestrators(self) -> tuple[Orchestrator, ...]:
        """Raises ``ChainError`` only when the contracts or the pool cannot be read."""
        bonding_manager = await self._contract(abi.BONDING_MANAGER_ID, "BondingManager")
        registry = self._registry or await self._contract(abi.SERVICE_REGISTRY_ID, "ServiceRegistry")
        pool = await self._pool(bonding_manager)
        reads = asyncio.Semaphore(_CONCURRENT_READS)

        async def eligible(address: str) -> Orchestrator | None:
            async with reads:
                try:
                    return await self._eligible(bonding_manager, registry, address)
                except abi.ChainError as exc:
                    if exc.transient:
                        raise  # the RPC failed, not this orchestrator: the whole load fails
                    _LOG.warning(
                        "orchestrator left out: its on-chain record could not be read",
                        extra={
                            "event": "discovery.orchestrator_unreadable",
                            "orchestrator": address,
                            "error": str(exc),
                        },
                    )
                    return None

        try:
            async with asyncio.TaskGroup() as group:  # a persistent RPC failure stops the other reads too
                tasks = [group.create_task(eligible(address)) for address in pool]
        except ExceptionGroup as failed:
            first = next((e for e in failed.exceptions if isinstance(e, abi.ChainError)), None)
            if first is None:
                raise
            raise first from None
        found = [task.result() for task in tasks]
        return unique([orchestrator for orchestrator in found if orchestrator is not None])

    async def _contract(self, contract_id: str, name: str) -> str:
        result = await self._read(self._controller, abi.encode_id_call(abi.GET_CONTRACT, contract_id))
        address = abi.decode_address(result)
        if address == abi.ZERO_ADDRESS:
            raise abi.ChainError(f"the Controller has no {name}")
        return address

    async def _pool(self, bonding_manager: str) -> list[str]:
        """The pool is a linked list; walk at most its reported size, and never past ``max_pool``."""
        size = abi.decode_uint(await self._read(bonding_manager, abi.GET_TRANSCODER_POOL_SIZE))
        if size > self._max_pool:
            _LOG.warning(
                "the transcoder pool reports more orchestrators than the walk reads",
                extra={"event": "discovery.pool_truncated", "reported": str(size), "read": self._max_pool},
            )
        limit = min(size, self._max_pool)
        address = abi.decode_address(await self._read(bonding_manager, abi.GET_FIRST_TRANSCODER_IN_POOL))
        pool: list[str] = []
        while address != abi.ZERO_ADDRESS and len(pool) < limit:
            pool.append(address)
            data = abi.encode_call(abi.GET_NEXT_TRANSCODER_IN_POOL, address)
            address = abi.decode_address(await self._read(bonding_manager, data))
        return pool

    async def _eligible(self, bonding_manager: str, registry: str, address: str) -> Orchestrator | None:
        active = await self._read(bonding_manager, abi.encode_call(abi.IS_ACTIVE_TRANSCODER, address))
        if not abi.decode_bool(active):
            return None
        uri = abi.decode_string(await self._read(registry, abi.encode_call(abi.GET_SERVICE_URI, address)))
        if not uri:
            return None  # bonded and active, but not registered here
        if uri.rstrip("/").endswith(_REGISTRY_MANIFEST):
            _LOG.info(
                "orchestrator registered a network-modules manifest, which this service does not read",
                extra={"event": "discovery.registry_manifest_skipped", "orchestrator": address},
            )
            return None
        try:
            url = base_url(uri)
            if not url.startswith("https://"):
                raise ValueError("a registration must be https")
        except ValueError:
            _LOG.warning(
                "orchestrator registered an unusable service URI",
                extra={"event": "discovery.service_uri_invalid", "orchestrator": address},
            )
            return None
        return Orchestrator(url=url, address=address, public_only=True)

    async def _read(self, to: str, data: str) -> str:
        """One ``eth_call``, retried after each of ``retry_delays_s`` while the RPC fails transiently."""
        for delay in self._retry_delays_s:
            try:
                return await self._chain.call(to, data)
            except abi.ChainError as exc:
                if not exc.transient:
                    raise
                await asyncio.sleep(delay)
        return await self._chain.call(to, data)
