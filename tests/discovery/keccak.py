"""Keccak-256 (Ethereum's, not NIST SHA3-256), written out so the tests can check the hard-coded call
selectors without a dependency. Slow and only for tests."""

_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000, 0x000000000000808B,
    0x0000000080000001, 0x8000000080008081, 0x8000000000008009, 0x000000000000008A, 0x0000000000000088,
    0x0000000080008009, 0x000000008000000A, 0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
    0x8000000000008003, 0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]  # fmt: skip
_ROT = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]
_MASK = (1 << 64) - 1


def _rol(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (64 - shift))) & _MASK if shift else value


def _permute(state: list[list[int]]) -> list[list[int]]:
    for rc in _RC:
        c = [state[x][0] ^ state[x][1] ^ state[x][2] ^ state[x][3] ^ state[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        state = [[state[x][y] ^ d[x] for y in range(5)] for x in range(5)]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(state[x][y], _ROT[x][y])
        state = [[b[x][y] ^ (~b[(x + 1) % 5][y] & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
        state[0][0] ^= rc
    return state


def keccak256(data: bytes) -> bytes:
    rate = 136
    padded = bytearray(data) + b"\x01"
    padded += b"\x00" * (-len(padded) % rate)
    padded[-1] |= 0x80
    state = [[0] * 5 for _ in range(5)]
    for offset in range(0, len(padded), rate):
        for i in range(rate // 8):
            state[i % 5][i // 5] ^= int.from_bytes(padded[offset + 8 * i : offset + 8 * i + 8], "little")
        state = _permute(state)
    return b"".join(state[i % 5][i // 5].to_bytes(8, "little") for i in range(4))
