"""Shared text read/hash helpers for filesystem edit tools."""

from __future__ import annotations

import hashlib
from typing import Final

EDIT_TEXT_ENCODING: Final[str] = "utf-8"
BINARY_SAMPLE_BYTES: Final[int] = 8192


def read_text_for_edit(path: str) -> str:
    with open(path, "r", encoding=EDIT_TEXT_ENCODING, errors="ignore") as f:
        return f.read()


def sha256_hex_of_text(text: str) -> str:
    return hashlib.sha256(text.encode(EDIT_TEXT_ENCODING)).hexdigest()


def sha256_hex_of_file(path: str) -> str:
    return sha256_hex_of_text(read_text_for_edit(path))


def is_valid_sha256_hex(value: str) -> bool:
    if len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def stale_file_error(*, expected: str, actual: str) -> dict[str, str]:
    return {
        "type": "stale_file",
        "expected_file_sha256": expected.lower(),
        "actual_file_sha256": actual,
        "message": "File content changed since the recorded SHA-256.",
    }


def check_expected_file_sha256(
    content: str,
    expected_file_sha256: str | None,
) -> dict[str, str] | None:
    if expected_file_sha256 is None:
        return None
    actual = sha256_hex_of_text(content)
    if actual.lower() == expected_file_sha256.lower():
        return None
    return stale_file_error(expected=expected_file_sha256, actual=actual)


def count_lines_in_text(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def guess_is_binary(path: str, sample_size: int = BINARY_SAMPLE_BYTES) -> bool:
    with open(path, "rb") as f:
        chunk = f.read(sample_size)
    if not chunk:
        return False
    if b"\x00" in chunk:
        return True
    try:
        chunk.decode(EDIT_TEXT_ENCODING)
    except UnicodeDecodeError:
        return True
    return False

