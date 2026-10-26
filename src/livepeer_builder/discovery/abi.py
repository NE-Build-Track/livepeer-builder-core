"""Just enough of the Ethereum ABI to read the Livepeer protocol: a few view calls over ``eth_call``.

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
GET_SERVICE_URI = "0x214c2a4b"  # ServiceRegistry.getServiceURI(address) -> string (both registries)
GET_CONTRACT = "0xe16c7d98"  # Controller.getContract(bytes32 id) -> address

# Controller ids: keccak256 of the contract's name. The AI Service Registry has none: it was deployed
# beside the Controller (livepeer/protocol deploy/deploy_ai_service_registry.ts), never registered in it.
BONDING_MANAGER_ID = "0x2517d59a36a86548e38734e8ab416f42afff4bca78706a66ad65750dae7f9e37"
SERVICE_REGISTRY_ID = "0x79c5d2a4a07754f4bacb0ffba18ac516030ee589ebc89db8627680c4d4cdb230"


class ChainError(Exception):
    """A chain read failed (transport, JSON-RPC error, or a result that does not decode).

    ``transient`` marks a failure of the RPC rather than an answer about the chain (a rate limit, a 5xx,
    the network): worth retrying, and never a reason to drop an orchestrator."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class EthCall(Protocol):
    async def call(self, to: str, data: str) -> str:
        """``eth_call`` against the latest block; returns the hex result. Raises ``ChainError``."""
        ...


def encode_call(selector: str, *addresses: str) -> str:
    return selector + "".join(_address_word(address) for address in addresses)


def encode_id_call(selector: str, contract_id: str) -> str:
    """A call taking one ``bytes32``, such as ``Controller.getContract``."""
    body = contract_id.lower().removeprefix("0x")
    if len(body) != 64 or any(c not in "0123456789abcdef" for c in body):
        raise ValueError(f"not a bytes32: {contract_id!r}")
    return selector + body


def decode_address(result: str) -> str:
    return "0x" + _words(result, 1)[0][-40:]


def decode_bool(result: str) -> bool:
    return decode_uint(result) != 0


def decode_uint(result: str) -> int:
    return int(_words(result, 1)[0], 16)


def decode_string(result: str) -> str:
    """An ABI ``string``. A result that is not one (``0x``, a short buffer, an offset or length past the
    end) is the RPC answering wrongly, so transient: never read as an empty string, which means "not
    registered" and would drop the orchestrator quietly (#46 review). Bytes that are not UTF-8 are the
    orchestrator's own record."""
    raw = bytes.fromhex(_hex(result))
    offset = int.from_bytes(raw[0:32], "big") if len(raw) >= 32 else None
    if offset is None or offset < 32 or offset + 32 > len(raw):  # the data follows the one-word head
        raise ChainError(f"not an ABI string: {result[:80]}", transient=True)
    length = int.from_bytes(raw[offset : offset + 32], "big")
    if offset + 32 + length > len(raw):
        raise ChainError(f"truncated ABI string: {result[:80]}", transient=True)
    try:
        return raw[offset + 32 : offset + 32 + length].decode()
    except UnicodeDecodeError as exc:
        raise ChainError(f"a string that is not UTF-8: {result[:80]}") from exc


def _address_word(address: str) -> str:
    body = address.lower().removeprefix("0x")
    if len(body) != 40 or any(c not in "0123456789abcdef" for c in body):
        raise ValueError(f"not an address: {address!r}")
    return "00" * 12 + body


def _hex(result: str) -> str:
    body = result.removeprefix("0x")
    if len(body) % 64 or any(c not in "0123456789abcdefABCDEF" for c in body):
        raise ChainError(f"not ABI-encoded words: {result[:80]}", transient=True)
    return body


def _words(result: str, count: int) -> list[str]:
    body = _hex(result)
    if len(body) < 64 * count:
        raise ChainError(f"expected {count} word(s): {result[:80]}", transient=True)
    return [body[i * 64 : (i + 1) * 64] for i in range(count)]
