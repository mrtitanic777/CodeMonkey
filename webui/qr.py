"""A small QR code writer (standard library only), for the "open on your phone" link.

Byte mode, error-correction level L, versions 1 to 6 (up to 134 bytes), mask pattern 0. That is
all a short local address needs. The layout follows ISO/IEC 18004; the structure of the code
follows the well-known reference implementations.
"""

from __future__ import annotations

from typing import List

# version -> (error-correction codewords per block, number of blocks, data codewords per block)
_LEVEL_L = {1: (7, 1, 19), 2: (10, 1, 34), 3: (15, 1, 55), 4: (20, 1, 80), 5: (26, 1, 108), 6: (18, 2, 68)}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> List[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: List[int], divisor: List[int]) -> List[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def matrix(text: str) -> List[List[bool]]:
    """The QR code for *text* as rows of modules (True = dark)."""
    data = text.encode("utf-8")
    version = next((v for v, (_, blocks, per) in _LEVEL_L.items() if len(data) + 2 <= blocks * per), None)
    if version is None:
        raise ValueError("too long for a QR code here (134 bytes at most)")
    ec_len, blocks, per_block = _LEVEL_L[version]
    capacity = blocks * per_block

    bits: List[int] = [0, 1, 0, 0] + [(len(data) >> i) & 1 for i in range(7, -1, -1)]
    for byte in data:
        bits += [(byte >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, capacity * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(codewords) < capacity:
        codewords.append(pad)
        pad ^= 0xEC ^ 0x11

    divisor = _rs_divisor(ec_len)
    data_blocks = [codewords[i * per_block:(i + 1) * per_block] for i in range(blocks)]
    ec_blocks = [_rs_remainder(block, divisor) for block in data_blocks]
    stream = [block[i] for i in range(per_block) for block in data_blocks]
    stream += [block[i] for i in range(ec_len) for block in ec_blocks]

    size = 17 + 4 * version
    dark = [[False] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def put(x: int, y: int, value: bool) -> None:
        dark[y][x] = value
        fixed[y][x] = True

    for i in range(size):                      # timing patterns
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):     # finders with their separators
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    put(x, y, max(abs(dx), abs(dy)) not in (2, 4))
    centres = _ALIGN[version]
    for ay in centres:                         # alignment pattern (not over a finder)
        for ax in centres:
            if (ax, ay) in ((6, 6), (6, centres[-1]), (centres[-1], 6)):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    put(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)

    fmt = (1 << 3) | 0                         # level L, mask 0
    rem = fmt
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    fmt_bits = ((fmt << 10) | rem) ^ 0x5412

    def fbit(i: int) -> bool:
        return (fmt_bits >> i) & 1 == 1

    for i in range(6):
        put(8, i, fbit(i))
    put(8, 7, fbit(6))
    put(8, 8, fbit(7))
    put(7, 8, fbit(8))
    for i in range(9, 15):
        put(14 - i, 8, fbit(i))
    for i in range(8):
        put(size - 1 - i, 8, fbit(i))
    for i in range(8, 15):
        put(8, size - 15 + i, fbit(i))
    put(8, size - 8, True)

    i = 0
    total = len(stream) * 8
    right = size - 1
    while right >= 1:                          # zigzag up and down in column pairs
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                upward = ((right + 1) & 2) == 0
                y = size - 1 - vert if upward else vert
                if not fixed[y][x]:
                    bit = (stream[i >> 3] >> (7 - (i & 7))) & 1 == 1 if i < total else False
                    i += 1
                    dark[y][x] = bit ^ ((x + y) % 2 == 0)       # mask 0
        right -= 2
    return dark


def svg(text: str, scale: int = 6, border: int = 4) -> str:
    """The QR code as an SVG image: dark modules on white, with the quiet zone scanners need."""
    m = matrix(text)
    size = len(m) + border * 2
    path = "".join(
        f"M{x + border},{y + border}h1v1h-1z" for y, row in enumerate(m) for x, on in enumerate(row) if on
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size * scale}" '
        f'height="{size * scale}" shape-rendering="crispEdges"><rect width="100%" height="100%" fill="#fff"/>'
        f'<path d="{path}" fill="#000"/></svg>'
    )


def text_blocks(text: str) -> str:
    """The QR code for a terminal: two characters per module, light on dark."""
    m = matrix(text)
    blank = [False] * (len(m) + 4)
    rows = [blank, blank] + [[False, False] + row + [False, False] for row in m] + [blank, blank]
    return "\n".join("".join("  " if on else "██" for on in row) for row in rows)
