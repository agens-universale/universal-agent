import hashlib

import pytest
from gc_core.utils.file_text import (
    BINARY_SAMPLE_BYTES,
    check_expected_file_sha256,
    classify_file_content,
    decode_text_bytes,
    encode_text_bytes,
    encode_text_for_append,
    read_text_for_edit,
    sha256_hex_of_bytes,
    sha256_hex_of_file_bytes,
    try_encode_text_bytes_for_append,
)


def test_classify_utf8_sample_boundary_cyrillic(tmp_path):
    prefix = ("а" * 4095 + "x").encode("utf-8")
    assert len(prefix) == 8191
    data = prefix + "а".encode()
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


def test_classify_utf16_le_bom_not_binary_despite_nuls(tmp_path):
    # UTF-16-LE "Hi\n" — ASCII code units embed NUL bytes after BOM.
    data = b"\xff\xfeH\x00i\x00\n\x00"
    path = tmp_path / "utf16.txt"
    path.write_bytes(data)

    classification = classify_file_content(str(path))
    assert classification.is_binary is False
    assert classification.encoding_guess == "utf-16-le"
    assert classification.binary_detection_reason is None


def test_classify_utf16_be_bom_not_binary_despite_nuls(tmp_path):
    data = b"\xfe\xff\x00H\x00i\x00\n"
    path = tmp_path / "utf16be.txt"
    path.write_bytes(data)

    classification = classify_file_content(str(path))
    assert classification.is_binary is False
    assert classification.encoding_guess == "utf-16-be"


def test_classify_utf8_bom_not_binary(tmp_path):
    data = b"\xef\xbb\xbfhello\n"
    path = tmp_path / "bom.txt"
    path.write_bytes(data)

    classification = classify_file_content(str(path))
    assert classification.is_binary is False
    assert classification.encoding_guess == "utf-8-sig"


def test_classify_legacy_bytes_not_binary(tmp_path):
    """cp1251 / non-UTF-8 without NUL is text for encoding-flow — not is_binary."""
    data = "Привет".encode("cp1251")
    path = tmp_path / "legacy.txt"
    path.write_bytes(data)

    classification = classify_file_content(str(path))
    assert classification.is_binary is False
    assert classification.binary_detection_reason is None
    assert classification.encoding_guess is None


def test_classify_invalid_utf8_sample_not_binary(tmp_path):
    invalid_sample = b"\xff" + b"a" * (BINARY_SAMPLE_BYTES - 1)
    path = tmp_path / "notes.txt"
    path.write_bytes(invalid_sample + b"tail")

    classification = classify_file_content(str(path))
    assert classification.is_binary is False
    assert classification.binary_detection_reason is None


def test_sha256_hex_of_bytes():
    data = b"payload\r\n"
    assert sha256_hex_of_bytes(data) == hashlib.sha256(data).hexdigest()


def test_sha256_hex_of_file_bytes(tmp_path):
    data = b"binary payload"
    path = tmp_path / "blob.bin"
    path.write_bytes(data)

    expected = hashlib.sha256(data).hexdigest()
    assert sha256_hex_of_file_bytes(str(path)) == expected
    assert sha256_hex_of_file_bytes(str(path)) == sha256_hex_of_bytes(data)


def test_check_expected_file_sha256_match_and_stale():
    data = b"live\r\n"
    actual = sha256_hex_of_bytes(data)
    assert check_expected_file_sha256(actual, None) is None
    assert check_expected_file_sha256(actual, actual) is None
    err = check_expected_file_sha256(actual, sha256_hex_of_bytes(b"other"))
    assert err is not None
    assert err["type"] == "stale_file"
    assert err["actual_file_sha256"] == actual


def test_read_text_for_edit_preserves_crlf_and_is_strict(tmp_path):
    path = tmp_path / "crlf.txt"
    path.write_bytes(b"a\r\nb\r\n")
    assert read_text_for_edit(str(path)) == "a\r\nb\r\n"

    bad = tmp_path / "bad.txt"
    bad.write_bytes(b"ok\xff")
    with pytest.raises(UnicodeDecodeError):
        read_text_for_edit(str(bad))


def test_read_text_for_edit_cp1251_via_canon(tmp_path):
    legacy = (
        "Уважаемые коллеги! Настоящим сообщаем, что заседание рабочей группы "
        "по внедрению системы состоится 15 марта 2024 года в переговорной.\n"
    ) * 3
    path = tmp_path / "legacy.txt"
    path.write_bytes(legacy.encode("cp1251"))
    assert read_text_for_edit(str(path)) == legacy


def test_decode_text_bytes_utf8_and_bom_round_trip():
    utf8 = decode_text_bytes(b"hi\n")
    assert utf8.ok and utf8.encoding_used == "utf-8" and utf8.text == "hi\n"

    bom8 = decode_text_bytes(b"\xef\xbb\xbfhi\n")
    assert bom8.ok and bom8.encoding_used == "utf-8-sig" and bom8.text == "hi\n"
    assert encode_text_bytes(bom8.text, bom8.encoding_used).startswith(b"\xef\xbb\xbf")

    raw16 = b"\xff\xfeH\x00i\x00"
    d16 = decode_text_bytes(raw16)
    assert d16.ok and d16.encoding_used == "utf-16-le" and d16.text == "Hi"
    assert encode_text_bytes(d16.text, d16.encoding_used) == raw16


def test_decode_text_bytes_nul_and_legacy():
    assert decode_text_bytes(b"a\x00b").error["type"] == "is_binary"
    legacy = (
        "Уважаемые коллеги! Настоящим сообщаем, что заседание рабочей группы "
        "по внедрению системы состоится 15 марта 2024 года в переговорной.\n"
    ) * 3
    err = decode_text_bytes(legacy.encode("cp1251"), include_preview=True).error
    assert err["type"] == "encoding_required"
    assert err["encoding_guess"] == "cp1251"
    assert err["encoding_confidence"] >= 0.5
    assert "Уважаемые" in err["preview"]
    assert err["preview_encoding"] == "cp1251"


def test_decode_text_best_effort_utf8_and_cp1251():
    from gc_core.utils.file_text import decode_text_best_effort

    assert decode_text_best_effort(b"hi\n") == "hi\n"
    legacy = (
        "Уважаемые коллеги! Настоящим сообщаем, что заседание рабочей группы "
        "по внедрению системы состоится 15 марта 2024 года в переговорной.\n"
    ) * 3
    assert decode_text_best_effort(legacy.encode("cp1251")) == legacy
    assert decode_text_best_effort(b"a\x00b") is None


def test_decode_explicit_cp1251_and_unsupported():
    raw = "Привет".encode("cp1251")
    ok = decode_text_bytes(raw, encoding="windows-1251")
    assert ok.ok and ok.encoding_used == "cp1251" and ok.text == "Привет"
    bad = decode_text_bytes(raw, encoding="ibm866")
    assert bad.error["type"] == "unsupported_encoding"
    wrong = decode_text_bytes(raw, encoding="utf-8")
    assert wrong.error["type"] == "undecodable_text"


def test_try_encode_failed_and_round_trip():
    from gc_core.utils.file_text import try_encode_text_bytes

    data, err = try_encode_text_bytes("Привет", "cp1251")
    assert err is None and data == "Привет".encode("cp1251")
    data2, err2 = try_encode_text_bytes("Привет", "latin-1")
    assert data2 is None and err2["type"] == "encode_failed"


def test_check_encoding_on_file_head_truncation():
    from gc_core.utils.file_text import check_encoding_on_file_head

    # Incomplete trailing UTF-8 sequence at cut — should still pass when truncated.
    sample = b"hello " + b"\xd0"  # start of a 2-byte Cyrillic char
    assert check_encoding_on_file_head(sample, "utf-8", sample_is_complete_file=False) is None
    # Same bytes as a complete file would be invalid UTF-8.
    err = check_encoding_on_file_head(sample, "utf-8", sample_is_complete_file=True)
    assert err is not None and err["type"] == "undecodable_text"


# --- append encode (no mid-file BOM) -------------------------------------------


@pytest.mark.parametrize(
    "encoding,expected_payload",
    [
        # utf-16 family: payload WITHOUT BOM (BOM lives at file offset 0).
        ("utf-16", "Ab".encode("utf-16-le")),
        ("utf-16-le", "Ab".encode("utf-16-le")),
        ("utf-16-be", "Ab".encode("utf-16-be")),
        # utf-8-sig: payload WITHOUT BOM (EF BB BF lives at file offset 0).
        ("utf-8-sig", b"Ab"),
        # plain utf-8 / single-byte: no BOM either way.
        ("utf-8", b"Ab"),
        ("cp1251", "\u0410b".encode("cp1251")),
    ],
)
def test_try_encode_text_bytes_for_append_no_bom(encoding, expected_payload):
    data, err = try_encode_text_bytes_for_append("Ab" if encoding != "cp1251" else "\u0410b", encoding)
    assert err is None, err
    assert data == expected_payload
    # Crucially: no BOM anywhere in the appended payload.
    assert not data.startswith(b"\xef\xbb\xbf")  # utf-8-sig BOM
    assert not data.startswith(b"\xff\xfe")  # utf-16-le BOM
    assert not data.startswith(b"\xfe\xff")  # utf-16-be BOM


def test_try_encode_text_bytes_for_append_empty_string():
    # Empty content → empty payload for every BOM-bearing encoding (no spurious BOM).
    for enc in ("utf-16", "utf-16-le", "utf-16-be", "utf-8-sig", "utf-8"):
        data, err = try_encode_text_bytes_for_append("", enc)
        assert err is None, (enc, err)
        assert data == b"", (enc, data)


def test_try_encode_text_bytes_for_append_unsupported():
    data, err = try_encode_text_bytes_for_append("x", "ibm866")
    assert data is None and err["type"] == "unsupported_encoding"


def test_try_encode_text_bytes_for_append_encode_fail():
    # cp1251 cannot encode the surrogate/non-cp1251 char here is tricky; use a char
    # outside latin-1 to force an encode failure on latin-1.
    data, err = try_encode_text_bytes_for_append("\u0410", "latin-1")
    assert data is None and err["type"] == "encode_failed"


def test_encode_text_for_append_raises_and_returns():
    assert encode_text_for_append("Ab", "utf-8-sig") == b"Ab"
    assert encode_text_for_append("", "utf-16") == b""
    with pytest.raises(ValueError):
        encode_text_for_append("\u0410", "latin-1")


def test_encode_text_for_append_utf8_sig_no_mid_bom_regression():
    # The exact bug: append-payload for utf-8-sig must NOT start with EF BB BF.
    data = encode_text_for_append("hello", "utf-8-sig")
    assert data == b"hello"
    assert data[:3] != b"\xef\xbb\xbf"


# --- line endings -----------------------------------------------------------

from gc_core.utils.file_text import convert_line_endings, detect_line_ending


def test_detect_line_ending_crlf():
    assert detect_line_ending("a\r\nb\r\n") == "\r\n"


def test_detect_line_ending_lf():
    assert detect_line_ending("a\nb\n") == "\n"


def test_detect_line_ending_none_without_newlines():
    assert detect_line_ending("single line") is None
    assert detect_line_ending("") is None


def test_detect_line_ending_mixed_picks_dominant():
    assert detect_line_ending("a\r\nb\r\nc\n") == "\r\n"
    assert detect_line_ending("a\nb\nc\r\n") == "\n"


def test_convert_line_endings_to_crlf_and_to_lf():
    assert convert_line_endings("a\nb\r\nc", "\r\n") == "a\r\nb\r\nc"
    assert convert_line_endings("a\r\nb\nc", "\n") == "a\nb\nc"


def test_convert_line_endings_leaves_bare_cr_alone():
    assert convert_line_endings("a\rb\n", "\r\n") == "a\rb\r\n"
