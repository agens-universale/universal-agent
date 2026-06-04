import hashlib

from utils.file_text import (
    BINARY_SAMPLE_BYTES,
    classify_file_content,
    sha256_hex_of_file_bytes,
    sha256_hex_of_text,
)


def test_classify_utf8_sample_boundary_cyrillic(tmp_path):
    prefix = ("а" * 4095 + "x").encode("utf-8")
    assert len(prefix) == 8191
    data = prefix + "а".encode("utf-8")
    assert len(data) > BINARY_SAMPLE_BYTES

    path = tmp_path / "report.md"
    path.write_bytes(data)

    classification = classify_file_content(str(path))
    assert classification.is_binary is False
    assert classification.encoding_guess == "utf-8"
    assert classification.binary_detection_reason is None


def test_classify_nul_byte_is_binary(tmp_path):
    path = tmp_path / "data.bin"
    path.write_bytes(b"hello\x00world")

    classification = classify_file_content(str(path))
    assert classification.is_binary is True
    assert classification.binary_detection_reason == "nul_byte"


def test_classify_text_extension_full_utf8_fallback(tmp_path):
    invalid_sample = b"\xff" + b"a" * (BINARY_SAMPLE_BYTES - 1)
    path = tmp_path / "notes.txt"
    path.write_bytes(invalid_sample + b"tail")

    classification = classify_file_content(str(path))
    assert classification.is_binary is True
    assert classification.binary_detection_reason == "utf8_decode_error"


def test_sha256_hex_of_file_bytes(tmp_path):
    data = b"binary payload"
    path = tmp_path / "blob.bin"
    path.write_bytes(data)

    expected = hashlib.sha256(data).hexdigest()
    assert sha256_hex_of_file_bytes(str(path)) == expected
