"""The on-chain directory: the bonded active set, registered for AI services (#45)."""

import asyncio
import logging

import pytest

from livepeer_builder.discovery import OnChainDirectory, Orchestrator, abi
from tests.discovery.keccak import keccak256

BM = "0x35bcf3c30594191d53231e4ff333e8a770453e40"
REGISTRY = "0x04c0b249740175999e5bf5c9ac1da92431ef34c5"
A, B, C, D = (f"0x{n:040x}" for n in (0xA1, 0xB2, 0xC3, 0xD4))


def test_the_keccak_used_to_check_selectors_is_ethereums() -> None:
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert keccak256(b"transfer(address,uint256)")[:4].hex() == "a9059cbb"  # the ERC-20 selector


@pytest.mark.parametrize(
    ("selector", "signature"),
    [
        (abi.GET_FIRST_TRANSCODER_IN_POOL, "getFirstTranscoderInPool()"),
        (abi.GET_NEXT_TRANSCODER_IN_POOL, "getNextTranscoderInPool(address)"),
        (abi.GET_TRANSCODER_POOL_SIZE, "getTranscoderPoolSize()"),
        (abi.IS_ACTIVE_TRANSCODER, "isActiveTranscoder(address)"),
        (abi.GET_SERVICE_URI, "getServiceURI(address)"),
    ],
)
def test_each_selector_is_the_keccak_of_its_signature(selector: str, signature: str) -> None:
    assert selector == "0x" + keccak256(signature.encode())[:4].hex()


def test_calls_encode_addresses_and_results_decode() -> None:
    assert abi.encode_call(abi.IS_ACTIVE_TRANSCODER, A) == abi.IS_ACTIVE_TRANSCODER + "00" * 12 + A[2:]
    assert abi.decode_address("0x" + "00" * 12 + B[2:]) == B
    assert abi.decode_bool("0x" + "00" * 31 + "01") is True and abi.decode_bool("0x" + "00" * 32) is False
    assert abi.decode_uint("0x" + "00" * 31 + "64") == 100
    assert abi.decode_string(string_result("https://orch.example:8935")) == "https://orch.example:8935"
    assert abi.decode_string(string_result("")) == ""


def string_result(text: str) -> str:
    data = text.encode()
    padded = data + b"\x00" * (-len(data) % 32)
    return "0x" + (32).to_bytes(32, "big").hex() + len(data).to_bytes(32, "big").hex() + padded.hex()


def word(value: int | str | bool) -> str:
    if isinstance(value, str):
        return "0x" + "00" * 12 + value[2:]
    return "0x" + int(value).to_bytes(32, "big").hex()


class Chain:
    """A fake Arbitrum: a transcoder pool as a linked list, each orchestrator's activity and URI."""

    def __init__(
        self, pool: list[str], active: set[str], uris: dict[str, str], size: int | None = None
    ) -> None:
        self.pool, self.active, self.uris = pool, active, uris
        self.size = len(pool) if size is None else size
        self.calls = 0

    async def call(self, to: str, data: str) -> str:
        self.calls += 1
        await asyncio.sleep(0)  # a real RPC yields, so a test timeout can fire on a runaway walk
        selector, argument = data[:10], "0x" + data[-40:]
        if to == BM and selector == abi.GET_TRANSCODER_POOL_SIZE:
            return word(self.size)
        if to == BM and selector == abi.GET_FIRST_TRANSCODER_IN_POOL:
            return word(self.pool[0] if self.pool else abi.ZERO_ADDRESS)
        if to == BM and selector == abi.GET_NEXT_TRANSCODER_IN_POOL:
            index = self.pool.index(argument) + 1
            if index < len(self.pool):
                return word(self.pool[index])
            return word(self.pool[0] if self.size > len(self.pool) else abi.ZERO_ADDRESS)  # a bad list wraps
        if to == BM and selector == abi.IS_ACTIVE_TRANSCODER:
            return word(argument in self.active)
        if to == REGISTRY and selector == abi.GET_SERVICE_URI:
            return string_result(self.uris.get(argument, ""))
        raise AssertionError(f"unexpected call to {to}: {data}")


def directory(chain: Chain) -> OnChainDirectory:
    return OnChainDirectory(chain, bonding_manager=BM, ai_service_registry=REGISTRY)


async def test_active_registered_orchestrators_in_pool_order() -> None:
    uris = {A: "https://a.example:8935/", C: "https://c.example:8935", D: "https://d.example:8936"}
    chain = Chain([A, B, C, D], active={A, B, D}, uris=uris)
    assert await directory(chain).orchestrators() == (
        Orchestrator(
            url="https://a.example:8935", address=A
        ),  # B: active, not registered; C: registered, inactive
        Orchestrator(url="https://d.example:8936", address=D),
    )


async def test_not_registering_for_ai_is_not_an_error(caplog: pytest.LogCaptureFixture) -> None:
    chain = Chain([A], active={A}, uris={})  # bonded and active, offering transcoding only
    assert await directory(chain).orchestrators() == ()
    assert "discovery.service_uri_invalid" not in {getattr(r, "event", None) for r in caplog.records}


async def test_an_unusable_uri_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    chain = Chain([A], active={A}, uris={A: "ftp://a.example"})
    assert await directory(chain).orchestrators() == ()
    assert "discovery.service_uri_invalid" in {getattr(r, "event", None) for r in caplog.records}


async def test_an_unusable_service_uri_is_left_out() -> None:
    chain = Chain([A, B], active={A, B}, uris={A: "not a url", B: "https://b.example:8935"})
    assert [o.address for o in await directory(chain).orchestrators()] == [B]


async def test_a_network_modules_registry_manifest_is_skipped_with_its_own_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:  # Cloud-SPE livepeer-network-modules manifests are another protocol; this engine cannot pay them
    caplog.set_level(logging.INFO)
    manifest = "https://coordinator.example/.well-known/livepeer-registry.json"
    chain = Chain([A, B], active={A, B}, uris={A: manifest, B: "https://b.example:8935"})
    assert [o.address for o in await directory(chain).orchestrators()] == [B]
    events = {getattr(r, "event", None) for r in caplog.records}
    assert "discovery.registry_manifest_skipped" in events and "discovery.service_uri_invalid" not in events


async def test_the_walk_reads_no_further_than_the_pool_size() -> None:
    uris = {A: "https://a.example", B: "https://b.example", C: "https://c.example"}
    chain = Chain([A, B, C], active={A, B, C}, uris=uris, size=2)
    assert [o.address for o in await directory(chain).orchestrators()] == [A, B]


async def test_a_list_that_loops_cannot_loop_the_walk() -> None:
    uris = {A: "https://a.example", B: "https://b.example"}
    looping = Chain([A, B], active={A, B}, uris=uris, size=3)  # the pool claims 3, and next(B) wraps to A
    async with asyncio.timeout(2):
        found = await directory(looping).orchestrators()
    assert [o.address for o in found] == [A, B]


async def test_an_empty_pool_is_an_empty_directory() -> None:
    assert await directory(Chain([], active=set(), uris={})).orchestrators() == ()


async def test_a_chain_error_raises_so_the_previous_directory_is_kept() -> None:
    class Down:
        async def call(self, to: str, data: str) -> str:
            raise abi.ChainError("rpc unavailable")

    with pytest.raises(abi.ChainError):
        await OnChainDirectory(Down()).orchestrators()
