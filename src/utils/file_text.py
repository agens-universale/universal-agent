"""Shared text read/hash helpers for filesystem edit tools."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Final

EDIT_TEXT_ENCODING: Final[str] = "utf-8"
BINARY_SAMPLE_BYTES: Final[int] = 8192

TEXT_FILE_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".json",
        ".py",
        ".yaml",
        ".yml",
        ".toml",
        ".xml",
        ".html",
        ".htm",
        ".css",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".sql",
        ".sh",
        ".bash",
        ".zsh",
        ".rs",
        ".go",
        ".java",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".rb",
        ".php",
        ".swift",
        ".kt",
        ".scala",
        ".r",
        ".lua",
        ".ini",
        ".cfg",
        ".conf",
        ".csv",
        ".tsv",
        ".rst",
        ".tex",
        ".log",
    }
)


@dataclass(frozen=True)
class FileContentClassification:
    is_binary: bool
    encoding_guess: str | None
    binary_detection_reason: str | None


def read_text_for_edit(path: str) -> str:
    with open(path, "r", encoding=EDIT_TEXT_ENCODING, errors="ignore") as f:
        return f.read()


def sha256_hex_of_text(text: str) -> str:
    return hashlib.sha256(text.encode(EDIT_TEXT_ENCODING)).hexdigest()


def sha256_hex_of_file(path: str) -> str:
    return sha256_hex_of_text(read_text_for_edit(path))


def sha256_hex_of_file_bytes(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


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


def _strip_incomplete_utf8_tail(chunk: bytes) -> bytes:
    if not chunk:
        return chunk
    index = len(chunk) - 1
    while index >= 0 and (chunk[index] & 0xC0) == 0x80:
        index -= 1
    if index < 0:
        return b""
    lead = chunk[index]
    if lead < 0x80:
        return chunk
    if lead < 0xE0:
        need = 2
    elif lead < 0xF0:
        need = 3
    elif lead < 0xF8:
        need = 4
    else:
        return chunk[:index]
    if len(chunk) - index < need:
        return chunk[:index]
    return chunk


def _try_decode_utf8_strict(data: bytes) -> bool:
    try:
        data.decode(EDIT_TEXT_ENCODING)
    except UnicodeDecodeError:
        return False
    return True


def _classify_bytes_sample(chunk: bytes) -> FileContentClassification:
    if not chunk:
        return FileContentClassification(
            is_binary=False,
            encoding_guess=EDIT_TEXT_ENCODING,
            binary_detection_reason=None,
        )
    if b"\x00" in chunk:
        return FileContentClassification(
            is_binary=True,
            encoding_guess=None,
            binary_detection_reason="nul_byte",
        )
    sample = _strip_incomplete_utf8_tail(chunk)
    if _try_decode_utf8_strict(sample):
        return FileContentClassification(
            is_binary=False,
            encoding_guess=EDIT_TEXT_ENCODING,
            binary_detection_reason=None,
        )
    return FileContentClassification(
        is_binary=True,
        encoding_guess=None,
        binary_detection_reason="utf8_decode_error",
    )


def _try_classify_text_extension(path: str) -> FileContentClassification | None:
    extension = os.path.splitext(path)[1].lower()
    if extension not in TEXT_FILE_EXTENSIONS:
        return None
    try:
        with open(path, "r", encoding=EDIT_TEXT_ENCODING) as f:
            f.read()
    except UnicodeDecodeError:
        return FileContentClassification(
            is_binary=True,
            encoding_guess=None,
            binary_detection_reason="utf8_decode_error",
        )
    except OSError:
        return None
    return FileContentClassification(
        is_binary=False,
        encoding_guess=EDIT_TEXT_ENCODING,
        binary_detection_reason="text_extension_full_utf8",
    )


def classify_content_from_sample(path: str, sample: bytes) -> FileContentClassification:
    """Pure classification: byte-sample analysis first; the text-extension
    full-file re-check only kicks in as a fallback when the sample looks like
    a truncated UTF-8 boundary (``utf8_decode_error``) — same precedence as
    the previous inline ``classify_file_content`` body."""
    classification = _classify_bytes_sample(sample)
    if classification.is_binary and classification.binary_detection_reason == "utf8_decode_error":
        text_extension = _try_classify_text_extension(path)
        if text_extension is not None:
            return text_extension
    return classification


def classify_file_content(path: str, sample_size: int = BINARY_SAMPLE_BYTES) -> FileContentClassification:
    with open(path, "rb") as f:
        chunk = f.read(sample_size)
    return classify_content_from_sample(path, chunk)


def guess_is_binary(path: str, sample_size: int = BINARY_SAMPLE_BYTES) -> bool:
    return classify_file_content(path, sample_size).is_binary
