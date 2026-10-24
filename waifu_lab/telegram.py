from __future__ import annotations

ALBUM_MAX = 10


def split_balanced(items: list, max_size: int = ALBUM_MAX) -> list[list]:
    """Split into ceil(n/max_size) near-equal chunks, order preserved (11 -> 6+5, 23 -> 8+8+7)."""
    n = len(items)
    if n <= max_size:
        return [items]
    parts = -(-n // max_size)
    base, extra = divmod(n, parts)
    out, i = [], 0
    for j in range(parts):
        size = base + (1 if j < extra else 0)
        out.append(items[i : i + size])
        i += size
    return out