"""Shared text read/hash helpers for filesystem edit tools."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Final

import chardet

EDIT_TEXT_ENCODING: Final[str] = "utf-8"
BINARY_SAMPLE_BYTES: Final[int] = 8192
# Head sample for append encoding check (do not load the whole file).
APPEND_HEAD_SAMPLE_BYTES: Final[int] = 100 * 1024
ENCODING_PREVIEW_CHARS: Final[int] = 500
ENCODING_GUESS_MIN_CONFIDENCE: Final[float] = 0.5
HEX_PREVIEW_BYTES: Final[int] = 64

TEXT_ENCODING_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "utf-8",
        "utf-8-sig",
        "cp1251",
        "koi8-r",
        "latin-1",
        "utf-16",
        "utf-16-le",
        "utf-16-be",
    }
)

# Input aliases → canonical allowlist name (responses / round-trip use canonical).
ENCODING_ALIASES: Final[dict[str, str]] = {
    "windows-1251": "cp1251",
    "iso-8859-1": "latin-1",
    "ascii": "utf-8",
}


@dataclass(frozen=True)
class FileContentClassification:
    is_binary: bool
    encoding_guess: str | None
    binary_detection_reason: str | None


def canonicalize_encoding_name(name: str | None) -> str | None:
    if not name or not isinstance(name, str):
        return None
    key = name.strip().lower().replace("_", "-")
    key = ENCODING_ALIASES.get(key, key)
    if key in TEXT_ENCODING_ALLOWLIST:
        return key
    return None


def guess_encoding(data: bytes) -> tuple[str | None, float | None]:
    """chardet hint filtered by allowlist + confidence threshold. Never writes."""
    if not data:
        return EDIT_TEXT_ENCODING, 1.0
    detected = chardet.detect(data) or {}
    raw_name = detected.get("encoding")
    confidence = detected.get("confidence")
    canonical = canonicalize_encoding_name(raw_name if isinstance(raw_name, str) else None)
    conf = float(confidence) if isinstance(confidence, (int, float)) else None
    if canonical is None or conf is None or conf < ENCODING_GUESS_MIN_CONFIDENCE:
        return None, conf
    return canonical, conf


def read_text_for_edit(path: str) -> str:
    """Decode local file via file_text canon (BOM / utf-8 / chardet allowlist).

    Preserves CRLF. No ``errors=ignore|replace``. Undecodable/binary → ``UnicodeDecodeError``.
    """
    with open(path, "rb") as f:
        data = f.read()
    text = decode_text_best_effort(data)
    if text is not None:
        return text
    raise UnicodeDecodeError(
        EDIT_TEXT_ENCODING,
        data,
        0,
        1,
        "undecodable or binary file",
    )


def sha256_hex_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
    actual_sha256: str,
    expected_file_sha256: str | None,
) -> dict[str, str] | None:
    """Stale-check against a bytes-hash hex (``sha256_hex_of_bytes`` / backend ``sha256_bytes``)."""
    if expected_file_sha256 is None:
        return None
    if actual_sha256.lower() == expected_file_sha256.lower():
        return None
    return stale_file_error(expected=expected_file_sha256, actual=actual_sha256)


def is_binary_error() -> dict[str, str]:
    return {
        "type": "is_binary",
        "message": "File looks binary (NUL byte in sample); not readable as text.",
    }


def encoding_required_error(
    data: bytes,
    *,
    include_preview: bool = False,
) -> dict[str, Any]:
    """Build encoding_required payload; optional read preview (never full silent decode for write)."""
    guess, confidence = guess_encoding(data)
    err: dict[str, Any] = {
        "type": "encoding_required",
        "message": ("File is not confident Unicode (no BOM / not strict UTF-8); pass encoding= explicitly."),
        "encoding_guess": guess,
        "encoding_confidence": confidence,
    }
    if not include_preview:
        return err

    preview: str | None = None
    preview_encoding: str | None = None
    preview_truncated = False
    if guess is not None:
        try:
            text = data.decode(guess)
            preview = text[:ENCODING_PREVIEW_CHARS]
            preview_encoding = guess
            preview_truncated = len(text) > ENCODING_PREVIEW_CHARS
        except UnicodeDecodeError:
            guess = None
            err["encoding_guess"] = None

    if preview is None:
        sample = data[:HEX_PREVIEW_BYTES]
        preview = sample.hex()
        preview_encoding = "hex"
        preview_truncated = len(data) > HEX_PREVIEW_BYTES
        err["encoding_guess"] = None

    err["preview"] = preview
    err["preview_encoding"] = preview_encoding
    err["preview_truncated"] = preview_truncated
    return err


@dataclass(frozen=True)
class TextDecodeResult:
    ok: bool
    text: str | None = None
    encoding_used: str | None = None
    error: dict[str, Any] | None = None


_BOM_UTF8: Final[bytes] = b"\xef\xbb\xbf"
_BOM_UTF16_LE: Final[bytes] = b"\xff\xfe"
_BOM_UTF16_BE: Final[bytes] = b"\xfe\xff"


def detect_unicode_bom(data: bytes) -> str | None:
    """Return canonical encoding for a leading Unicode BOM, else None."""
    if data.startswith(_BOM_UTF8):
        return "utf-8-sig"
    if data.startswith(_BOM_UTF16_LE):
        return "utf-16-le"
    if data.startswith(_BOM_UTF16_BE):
        return "utf-16-be"
    return None


def _decode_with_bom(data: bytes, bom_encoding: str) -> str:
    if bom_encoding == "utf-8-sig":
        return data.decode("utf-8-sig")
    if bom_encoding == "utf-16-le":
        return data[len(_BOM_UTF16_LE) :].decode("utf-16-le")
    if bom_encoding == "utf-16-be":
        return data[len(_BOM_UTF16_BE) :].decode("utf-16-be")
    raise ValueError(f"unsupported BOM encoding: {bom_encoding}")


def unsupported_encoding_error(name: str) -> dict[str, Any]:
    return {
        "type": "unsupported_encoding",
        "message": f"Unsupported encoding {name!r}.",
        "encoding": name,
    }


def encode_failed_error(encoding: str) -> dict[str, Any]:
    return {
        "type": "encode_failed",
        "message": f"Cannot encode text as {encoding}; file not modified.",
        "encoding": encoding,
    }


def resolve_encoding_arg(encoding: str | None) -> tuple[str | None, dict[str, Any] | None]:
    """Normalize optional encoding arg to allowlist canonical name."""
    if encoding is None or encoding == "":
        return None, None
    if not isinstance(encoding, str):
        return None, {
            "type": "invalid_argument",
            "field": "encoding",
            "message": "Argument 'encoding' must be a string or omitted.",
        }
    canonical = canonicalize_encoding_name(encoding)
    if canonical is None:
        return None, unsupported_encoding_error(encoding)
    return canonical, None


_UTF16_FAMILY: Final[frozenset[str]] = frozenset({"utf-16", "utf-16-le", "utf-16-be"})


def _decode_explicit(data: bytes, encoding: str) -> str:
    if encoding == "utf-16-le":
        if data.startswith(_BOM_UTF16_LE):
            return data[len(_BOM_UTF16_LE) :].decode("utf-16-le")
        return data.decode("utf-16-le")
    if encoding == "utf-16-be":
        if data.startswith(_BOM_UTF16_BE):
            return data[len(_BOM_UTF16_BE) :].decode("utf-16-be")
        return data.decode("utf-16-be")
    if encoding == "utf-16":
        return data.decode("utf-16")
    if encoding == "utf-8-sig":
        return data.decode("utf-8-sig")
    return data.decode(encoding)


def decode_text_bytes(
    data: bytes,
    *,
    encoding: str | None = None,
    include_preview: bool = False,
) -> TextDecodeResult:
    """Resolve text: optional explicit encoding, else BOM → NUL → utf-8 → encoding_required."""
    explicit, enc_err = resolve_encoding_arg(encoding)
    if enc_err is not None:
        return TextDecodeResult(ok=False, error=enc_err)
    if explicit is not None:
        if explicit not in _UTF16_FAMILY and b"\x00" in data[:BINARY_SAMPLE_BYTES]:
            return TextDecodeResult(ok=False, error=is_binary_error())
        try:
            text = _decode_explicit(data, explicit)
        except UnicodeDecodeError:
            return TextDecodeResult(
                ok=False,
                error={
                    "type": "undecodable_text",
                    "message": f"Failed to decode file as {explicit}.",
                    "encoding": explicit,
                },
            )
        return TextDecodeResult(ok=True, text=text, encoding_used=explicit)

    bom = detect_unicode_bom(data)
    if bom is not None:
        try:
            text = _decode_with_bom(data, bom)
        except UnicodeDecodeError:
            return TextDecodeResult(
                ok=False,
                error={
                    "type": "undecodable_text",
                    "message": f"Failed to decode file as {bom}.",
                },
            )
        return TextDecodeResult(ok=True, text=text, encoding_used=bom)

    if b"\x00" in data[:BINARY_SAMPLE_BYTES]:
        return TextDecodeResult(ok=False, error=is_binary_error())

    try:
        text = data.decode(EDIT_TEXT_ENCODING)
    except UnicodeDecodeError:
        return TextDecodeResult(
            ok=False,
            error=encoding_required_error(data, include_preview=include_preview),
        )
    return TextDecodeResult(ok=True, text=text, encoding_used=EDIT_TEXT_ENCODING)


def encode_text_bytes(text: str, encoding: str) -> bytes:
    """Strict encode for round-trip. utf-16-le/be re-add BOM (decode strips it)."""
    if encoding == "utf-16-le":
        return _BOM_UTF16_LE + text.encode("utf-16-le")
    if encoding == "utf-16-be":
        return _BOM_UTF16_BE + text.encode("utf-16-be")
    if encoding == "utf-16":
        return text.encode("utf-16")
    return text.encode(encoding)


def try_encode_text_bytes(
    text: str,
    encoding: str,
) -> tuple[bytes | None, dict[str, Any] | None]:
    canonical, err = resolve_encoding_arg(encoding)
    if err is not None:
        return None, err
    assert canonical is not None
    try:
        return encode_text_bytes(text, canonical), None
    except UnicodeEncodeError:
        return None, encode_failed_error(canonical)


def encode_text_for_write(content: str, encoding: str = EDIT_TEXT_ENCODING) -> bytes:
    """Strict allowlist encode for backend ``write``. Raises ``ValueError`` on fail."""
    data, err = try_encode_text_bytes(content, encoding)
    if err is not None:
        raise ValueError(err.get("message") or f"encode failed: {encoding}")
    assert data is not None
    return data


def try_encode_text_bytes_for_append(
    text: str,
    encoding: str,
) -> tuple[bytes | None, dict[str, Any] | None]:
    """Like :func:`try_encode_text_bytes` but WITHOUT a leading BOM for utf-16.

    Used by ``append``: the existing file already carries a BOM at offset 0, so the
    appended chunk must be pure payload. A second BOM in the middle of the file would
    decode as ``U+FEFF`` (ZERO WIDTH NO-BREAK SPACE) between the existing tail and the
    new content. Applies to both utf-16 family and utf-8-sig (EF BB BF).
    """
    canonical, err = resolve_encoding_arg(encoding)
    if err is not None:
        return None, err
    assert canonical is not None
    try:
        if canonical == "utf-16-le":
            return text.encode("utf-16-le"), None
        if canonical == "utf-16-be":
            return text.encode("utf-16-be"), None
        if canonical == "utf-16":
            # utf-16 canonical == little-endian with BOM (see encode_text_bytes);
            # existing file already has the BOM, so append LE payload without it.
            return text.encode("utf-16-le"), None
        if canonical == "utf-8-sig":
            # utf-8-sig file already carries an EF BB BF BOM at offset 0;
            # append plain utf-8 payload without re-emitting the BOM.
            return text.encode("utf-8"), None
        return encode_text_bytes(text, canonical), None
    except UnicodeEncodeError:
        return None, encode_failed_error(canonical)


def encode_text_for_append(content: str, encoding: str = EDIT_TEXT_ENCODING) -> bytes:
    """Strict allowlist encode for backend ``append``. Raises ``ValueError`` on fail.

    Unlike :func:`encode_text_for_write`, this never emits a leading BOM for utf-16
    or utf-8-sig encodings — appending a BOM in the middle of an existing file corrupts it.
    """
    data, err = try_encode_text_bytes_for_append(content, encoding)
    if err is not None:
        raise ValueError(err.get("message") or f"encode failed: {encoding}")
    assert data is not None
    return data


def count_lines_in_text(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def detect_line_ending(text: str) -> str | None:
    """Dominant newline style of ``text``: ``"\\r\\n"`` or ``"\\n"``; ``None`` without newlines."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf == 0 and lf == 0:
        return None
    return "\r\n" if crlf > lf else "\n"


def convert_line_endings(text: str, eol: str) -> str:
    """Rewrite every ``\\r\\n`` / ``\\n`` in ``text`` as ``eol``.

    A bare ``\\r`` is data, not a line break (progress bars, protocol fixtures),
    so it is left alone — unlike ``fs_write_to_file``'s explicit ``newline=lf|crlf``,
    which is a request to normalize everything.
    """
    return text.replace("\r\n", "\n").replace("\n", eol)


def has_mixed_line_endings(text: str) -> bool:
    crlf = text.count("\r\n")
    return crlf > 0 and text.count("\n") > crlf


def line_ending_at(text: str, pos: int) -> str | None:
    """Line ending of the line containing ``pos`` (the last line borrows the one
    before it); ``None`` when the text has no line break around ``pos``."""
    nl = text.find("\n", pos)
    if nl == -1:
        nl = text.rfind("\n", 0, pos)
        if nl == -1:
            return None
    return "\r\n" if nl > 0 and text[nl - 1] == "\r" else "\n"


def decode_text_best_effort(data: bytes) -> str | None:
    """Best-effort decode for metadata/display (not for write). Never raises on codec fail.

    Confident Unicode first; else chardet allowlist guess. Binary / undecodable → ``None``.
    """
    result = decode_text_bytes(data)
    if result.ok:
        return result.text
    if (result.error or {}).get("type") == "is_binary":
        return None
    guess, _ = guess_encoding(data)
    if guess is None:
        return None
    guessed = decode_text_bytes(data, encoding=guess)
    return guessed.text if guessed.ok else None


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


def _trim_head_sample_for_decode(sample: bytes, encoding: str) -> bytes:
    """Drop a possibly incomplete trailing code unit so head decode can be strict."""
    if encoding in ("utf-8", "utf-8-sig"):
        return _strip_incomplete_utf8_tail(sample)
    if encoding in ("utf-16-le", "utf-16-be", "utf-16"):
        # Avoid cutting mid code unit (2 bytes). Surrogate pairs may still split —
        # rare at a 100 KiB boundary; fail closed via decode error if so.
        return sample if len(sample) % 2 == 0 else sample[:-1]
    return sample


def check_encoding_on_file_head(
    sample: bytes,
    encoding: str,
    *,
    sample_is_complete_file: bool,
) -> dict[str, Any] | None:
    """Validate that a file head matches ``encoding`` without loading the whole file.

    Returns an error dict (``is_binary`` / ``undecodable_text`` / …) or ``None`` if OK.
    When ``sample_is_complete_file`` is false, trims an incomplete trailing UTF-8/16
    sequence at the cut point before decoding.
    """
    canonical, enc_err = resolve_encoding_arg(encoding)
    if enc_err is not None:
        return enc_err
    assert canonical is not None
    data = sample
    if not sample_is_complete_file:
        data = _trim_head_sample_for_decode(sample, canonical)
    decoded = decode_text_bytes(data, encoding=canonical)
    if not decoded.ok:
        return decoded.error
    return None


def _try_decode_utf8_strict(data: bytes) -> bool:
    try:
        data.decode(EDIT_TEXT_ENCODING)
    except UnicodeDecodeError:
        return False
    return True


def _classify_bytes_sample(chunk: bytes) -> FileContentClassification:
    """BOM before NUL: utf-16 text has embedded NULs and must not be marked binary."""
    bom_encoding = detect_unicode_bom(chunk)
    if bom_encoding is not None:
        return FileContentClassification(
            is_binary=False,
            encoding_guess=bom_encoding,
            binary_detection_reason=None,
        )
    if b"\x00" in chunk:
        return FileContentClassification(
            is_binary=True,
            encoding_guess=None,
            binary_detection_reason="nul_byte",
        )
    if not chunk:
        return FileContentClassification(
            is_binary=False,
            encoding_guess=EDIT_TEXT_ENCODING,
            binary_detection_reason=None,
        )
    sample = _strip_incomplete_utf8_tail(chunk)
    if sample and _try_decode_utf8_strict(sample):
        return FileContentClassification(
            is_binary=False,
            encoding_guess=EDIT_TEXT_ENCODING,
            binary_detection_reason=None,
        )
    return FileContentClassification(
        is_binary=False,
        encoding_guess=None,
        binary_detection_reason=None,
    )


def classify_content_from_sample(path: str, sample: bytes) -> FileContentClassification:
    """Classify from a byte sample. ``path`` kept for call-site stability; NUL-only rule."""
    _ = path
    return _classify_bytes_sample(sample)


def classify_file_content(path: str, sample_size: int = BINARY_SAMPLE_BYTES) -> FileContentClassification:
    with open(path, "rb") as f:
        chunk = f.read(sample_size)
    return classify_content_from_sample(path, chunk)
