from __future__ import annotations

import re

_CELL_RE = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")


def _col_letters_to_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def _col_index_to_letters(idx: int) -> str:
    if idx < 1:
        raise ValueError(f"column index must be >= 1, got {idx}")
    out = ""
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def _parse_cell(s: str) -> tuple[int, int]:
    m = _CELL_RE.match(s)
    if not m:
        raise ValueError(f"invalid A1 cell: {s!r}")
    col, row = m.group(1), int(m.group(2))
    return _col_letters_to_index(col), row


def parse_a1_range(s: str) -> tuple[int, int, int, int]:
    """Parse A1 range into (col_min, row_min, col_max, row_max) 1-based inclusive."""
    if not s:
        raise ValueError("empty A1 range")
    parts = s.split(":")
    if len(parts) == 1:
        c, r = _parse_cell(parts[0])
        return (c, r, c, r)
    if len(parts) == 2:
        c1, r1 = _parse_cell(parts[0])
        c2, r2 = _parse_cell(parts[1])
        return (min(c1, c2), min(r1, r2), max(c1, c2), max(r1, r2))
    raise ValueError(f"invalid A1 range: {s!r}")


def a1_cell(col: int, row: int) -> str:
    return f"{_col_index_to_letters(col)}{row}"
