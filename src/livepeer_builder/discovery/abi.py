"""Just enough of the Ethereum ABI to read the Livepeer protocol: four view calls over ``eth_call``.

Selectors are the first four bytes of the Keccak-256 of each signature, written out here so there is
no web3 dependency; tests/discovery/test_onchain.py recomputes them. Contracts and signatures are from
livepeer/protocol (branch ``delta``, ``deployments/arbitrumMainnet``).
"""

from typing import Protocol

ZERO_ADDRESS = "0x" + "00" * 20

GET_TRANSCODER_POOL_SIZE = "0x2a4e0d55"  # BondingManager.getTranscoderPoolSize() -> uint256
GET_FIRST_TRANSCODER_IN_POOL = "0x88a6c749"  # BondingManager.getFirstTranscoderInPool() -> address
GET_NEXT_TRANSCODER_IN_POOL = "0x235c9603"  # BondingManager.getNextTranscoderInPool(address) -> address
IS_ACTIVE_TRANSCODER = "0x08802374"  # BondingManager.isActiveTranscoder(address) -> bool, current round
GET_SERVICE_URI = "0x214c2a4b"  # AIServiceRegistry.getServiceURI(address) -> string


class ChainError(Exception):
    """A chain read failed (transport, JSON-RPC error, or a result that does not decode)."""


class EthCall(Protocol):
    async def call(self, to: str, data: str) -> str:
        """``eth_call`` against the latest block; returns the hex result. Raises ``ChainError``."""
        ...


def encode_call(selector: str, *addresses: str) -> str:
    return selector + "".join(_address_word(address) for address in addresses)


def decode_address(result: str) -> str:
    return "0x" + _words(result, 1)[0][-40:]


def decode_bool(result: str) -> bool:
    return decode_uint(result) != 0


def decode_uint(result: str) -> int:
    return int(_words(result, 1)[0], 16)


def decode_string(result: str) -> str:
    raw = bytes.fromhex(_hex(result))
    try:
        offset = int.from_bytes(raw[0:32], "big")
        length = int.from_bytes(raw[offset : offset + 32], "big")
        data = raw[offset + 32 : offset + 32 + length]
        if len(data) != length:
            raise ValueError("truncated")
        return data.decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise ChainError(f"not an ABI string: {result[:80]}") from exc


def _address_word(address: str) -> str:
    body = address.lower().removeprefix("0x")
    if len(body) != 40 or any(c not in "0123456789abcdef" for c in body):
        raise ValueError(f"not an address: {address!r}")
    return "00" * 12 + body


def _hex(result: str) -> str:
    body = result.removeprefix("0x")
    if len(body) % 64 or any(c not in "0123456789abcdefABCDEF" for c in body):
        raise ChainError(f"not ABI-encoded words: {result[:80]}")
    return body


def _words(result: str, count: int) -> list[str]:
    body = _hex(result)
    if len(body) < 64 * count:
        raise ChainError(f"expected {count} word(s): {result[:80]}")
    return [body[i * 64 : (i + 1) * 64] for i in range(count)]
