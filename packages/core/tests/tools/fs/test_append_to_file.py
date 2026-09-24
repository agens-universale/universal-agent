import copy
import json

import pytest

from gc_core.tools.arguments_validation import WrongFunctionArgumentsException, parse_and_validate_tool_arguments
from gc_core.tools.fs.append_to_file import AppendToFile
from gc_core.tools.schema_optional import apply_openai_strict_parameters


from gc_testkit.helpers.tool_ctx import with_local_ctx

def test_append_success_existing_file(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("start", encoding="utf-8")
    res = json.loads(with_local_ctx(AppendToFile)(path=str(p), content="next"))
    assert res["ok"] is True
    assert res["created"] is False
    assert res["separator"] == "\n\n"
    assert p.read_text(encoding="utf-8") == "start\n\nnext"


def test_append_create_missing_with_dirs(tmp_path):
    p = tmp_path / "a" / "b" / "c.txt"
    res = json.loads(
        with_local_ctx(AppendToFile)(path=str(p), content="hello", create_if_missing=True, make_dirs=True, separator="")
    )
    assert res["ok"] is True
    assert res["created"] is True
    assert p.read_text(encoding="utf-8") == "hello"


def test_parse_preserves_empty_separator_non_strict():
    spec = AppendToFile.tool_description()
    args = parse_and_validate_tool_arguments(
        tool_name="fs_append_file",
        arguments={"path": "/tmp/note.txt", "content": "x", "separator": ""},
        tool_spec=spec,
        allow_empty_for_empty_schema=False,
    )
    assert args["separator"] == ""


def test_parse_rejects_omitted_separator():
    spec = AppendToFile.tool_description()
    assert "separator" in spec["parameters"]["required"]
    with pytest.raises(WrongFunctionArgumentsException):
        parse_and_validate_tool_arguments(
            tool_name="fs_append_file",
            arguments={"path": "/tmp/note.txt", "content": "x"},
            tool_spec=spec,
            allow_empty_for_empty_schema=False,
        )


def test_parse_preserves_empty_separator_strict():
    spec = copy.deepcopy(AppendToFile.tool_description())
    apply_openai_strict_parameters(spec["parameters"])
    args = parse_and_validate_tool_arguments(
        tool_name="fs_append_file",
        arguments={
            "path": "/tmp/note.txt",
            "content": "x",
            "separator": "",
            "create_if_missing": False,
            "make_dirs": False,
            "encoding": "utf-8",
        },
        tool_spec=spec,
        allow_empty_for_empty_schema=False,
    )
    assert args["separator"] == ""


def test_append_empty_separator_after_validation(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("Line alpha\n", encoding="utf-8")
    spec = AppendToFile.tool_description()
    args = parse_and_validate_tool_arguments(
        tool_name="fs_append_file",
        arguments={"path": str(p), "content": "Line gamma", "separator": ""},
        tool_spec=spec,
        allow_empty_for_empty_schema=False,
    )
    res = json.loads(with_local_ctx(AppendToFile)(**args))
    assert res["ok"] is True
    assert res["separator"] == ""
    assert p.read_text(encoding="utf-8") == "Line alpha\nLine gamma"


def test_append_errors(tmp_path):
    tool = with_local_ctx(AppendToFile)
    rel = json.loads(tool(path="rel.txt", content="x"))
    assert rel["ok"] is False and rel["error"]["field"] == "path"
    miss = json.loads(tool(path=str(tmp_path / "missing.txt"), content="x"))
    assert miss["ok"] is False and miss["error"]["type"] == "not_found"
    bad_sep = json.loads(tool(path=str(tmp_path / "x.txt"), content="x", separator=123))
    assert bad_sep["ok"] is False and bad_sep["error"]["field"] == "separator"


def test_append_cp1251_round_trip(tmp_path):
    p = tmp_path / "ru.txt"
    p.write_bytes("Привет".encode("cp1251"))
    res = json.loads(
        with_local_ctx(AppendToFile)(
            path=str(p),
            content=" мир",
            separator="",
            encoding="cp1251",
        )
    )
    assert res["ok"] is True
    assert res["encoding_used"] == "cp1251"
    assert p.read_bytes() == "Привет мир".encode("cp1251")


def test_append_unsupported_encoding(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    res = json.loads(
        with_local_ctx(AppendToFile)(
            path=str(p), content="y", separator="", encoding="shift_jis"
        )
    )
    assert res["ok"] is False
    assert res["error"]["type"] == "unsupported_encoding"
    assert p.read_text(encoding="utf-8") == "x"


def test_append_encode_failed_keeps_file(tmp_path):
    p = tmp_path / "a.txt"
    original = "Hi".encode("latin-1")
    p.write_bytes(original)
    res = json.loads(
        with_local_ctx(AppendToFile)(
            path=str(p), content="Привет", separator="", encoding="latin-1"
        )
    )
    assert res["ok"] is False
    assert res["error"]["type"] == "encode_failed"
    assert p.read_bytes() == original


def test_append_wrong_encoding_keeps_file(tmp_path):
    p = tmp_path / "ru.txt"
    original = "Привет".encode("cp1251")
    p.write_bytes(original)
    res = json.loads(
        with_local_ctx(AppendToFile)(
            path=str(p), content=" x", separator="", encoding="utf-8"
        )
    )
    assert res["ok"] is False
    assert res["error"]["type"] == "undecodable_text"
    assert p.read_bytes() == original


def test_append_rejects_nul_binary(tmp_path):
    p = tmp_path / "blob.bin"
    original = b"aaa\x00bbb"
    p.write_bytes(original)
    res = json.loads(
        with_local_ctx(AppendToFile)(
            path=str(p), content="x", separator="", encoding="utf-8"
        )
    )
    assert res["ok"] is False
    assert res["error"]["type"] == "is_binary"
    assert p.read_bytes() == original


def test_append_large_file_without_full_read(tmp_path):
    """Append must not require loading the whole file (head-check + end write)."""
    from gc_core.utils.file_text import APPEND_HEAD_SAMPLE_BYTES

    p = tmp_path / "big.log"
    # Larger than former mutate cap and larger than head sample.
    body = ("line\n" * 30_000).encode("utf-8")
    assert len(body) > APPEND_HEAD_SAMPLE_BYTES
    p.write_bytes(body)
    res = json.loads(
        with_local_ctx(AppendToFile)(
            path=str(p), content="TAIL", separator="\n", encoding="utf-8"
        )
    )
    assert res["ok"] is True
    assert p.read_bytes().endswith(b"\nTAIL")
    assert p.read_bytes().startswith(body[:64])
    assert res["sha256"]


def test_append_head_truncated_utf8_still_ok(tmp_path):
    """Multi-byte char spanning the head cut must not false-fail encoding check."""
    from gc_core.utils.file_text import APPEND_HEAD_SAMPLE_BYTES

    # Fill so that a 2-byte Cyrillic char sits across the sample boundary.
    prefix = b"a" * (APPEND_HEAD_SAMPLE_BYTES - 1)
    # "я" is D1 8F in utf-8 — first byte at last head slot, second beyond sample.
    rest = "я".encode("utf-8") + b" more text\n"
    p = tmp_path / "cut.txt"
    p.write_bytes(prefix + rest)
    res = json.loads(
        with_local_ctx(AppendToFile)(
            path=str(p), content="ok", separator="", encoding="utf-8"
        )
    )
    assert res["ok"] is True
    assert p.read_bytes().endswith(b"ok")


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_append_utf16_no_mid_bom(tmp_path, encoding):
    """Appending to a utf-16 file must NOT insert a BOM (U+FEFF) between lines.

    Regression for FS_ENCODING_PROBLEMS.md case #1: ``fs_append_file`` inserted a
    BOM in the middle of an existing utf-16 file, corrupting it with
    ZERO WIDTH NO-BREAK SPACE (U+FEFF) between the existing tail and the new chunk.
    """
    from gc_core.utils.file_text import decode_text_bytes
    from gc_core.tools.fs.write_to_file_tool import WriteToFile

    p = tmp_path / "u16.txt"
    wres = json.loads(
        with_local_ctx(WriteToFile)(
            path=str(p),
            content="Строка первая\nСтрока вторая",
            mode="overwrite",
            encoding=encoding,
        )
    )
    assert wres["ok"], wres

    res = json.loads(
        with_local_ctx(AppendToFile)(
            path=str(p),
            content="Строка третья",
            separator="",
            encoding=encoding,
        )
    )
    assert res["ok"], res

    raw = p.read_bytes()
    # The leading BOM (written by fs_write_file) must still be present at offset 0.
    expected_bom = b"\xff\xfe" if encoding in ("utf-16", "utf-16-le") else b"\xfe\xff"
    assert raw[:2] == expected_bom, f"{encoding}: start BOM lost: {raw[:2]!r}"

    # Decoded text must NOT contain U+FEFF anywhere — decode_text_bytes strips the
    # leading BOM, so any remaining U+FEFF is a spurious mid-file BOM (the bug).
    decoded = decode_text_bytes(raw, encoding=encoding)
    assert decoded.ok, f"{encoding}: decode failed: {decoded.error}"
    assert "\ufeff" not in decoded.text, (
        f"{encoding}: spurious mid-file BOM (U+FEFF): {decoded.text!r}"
    )
    assert decoded.text == "Строка первая\nСтрока втораяСтрока третья"

    # bytes_appended must count the payload without the BOM (26 bytes = 13 chars * 2).
    assert res["bytes_appended"] == 26, res["bytes_appended"]


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_append_utf16_create_missing_has_bom(tmp_path, encoding):
    """create_if_missing path must still write a BOM for a brand-new utf-16 file."""
    from gc_core.utils.file_text import decode_text_bytes

    p = tmp_path / "created.txt"
    res = json.loads(
        with_local_ctx(AppendToFile)(
            path=str(p),
            content="Hello",
            separator="",
            encoding=encoding,
            create_if_missing=True,
            make_dirs=True,
        )
    )
    assert res["ok"] and res["created"], res
    raw = p.read_bytes()
    expected_bom = b"\xff\xfe" if encoding in ("utf-16", "utf-16-le") else b"\xfe\xff"
    assert raw[:2] == expected_bom, f"{encoding}: start BOM missing: {raw[:2]!r}"
    decoded = decode_text_bytes(raw, encoding=encoding)
    assert decoded.ok and decoded.text == "Hello"


def test_append_utf8_sig_no_mid_bom(tmp_path):
    """Appending to a utf-8-sig file must NOT re-emit EF BB BF in the middle.

    Regression for the same mid-file BOM bug extended to utf-8-sig:
    ``text.encode(\"utf-8-sig\")`` always prepends EF BB BF, so the append path must
    encode as plain utf-8 (BOM lives only at file offset 0).
    """
    from gc_core.utils.file_text import decode_text_bytes
    from gc_core.tools.fs.write_to_file_tool import WriteToFile

    p = tmp_path / "sig.txt"
    wres = json.loads(
        with_local_ctx(WriteToFile)(
            path=str(p), content="first line\nsecond line",
            mode="overwrite", encoding="utf-8-sig",
        )
    )
    assert wres["ok"], wres

    res = json.loads(
        with_local_ctx(AppendToFile)(
            path=str(p), content="third line", separator="", encoding="utf-8-sig",
        )
    )
    assert res["ok"], res

    raw = p.read_bytes()
    # Start BOM preserved by fs_write_file.
    assert raw[:3] == b"\xef\xbb\xbf", f"start BOM lost: {raw[:3]!r}"
    # No second BOM in the middle of the byte stream.
    assert b"\xef\xbb\xbf" not in raw[3:], f"spurious mid-file utf-8-sig BOM: {raw!r}"
    decoded = decode_text_bytes(raw, encoding="utf-8-sig")
    assert decoded.ok and "\ufeff" not in decoded.text
    assert decoded.text == "first line\nsecond linethird line"
    # bytes_appended = len("third line") = 10 (plain utf-8, no BOM).
    assert res["bytes_appended"] == 10, res["bytes_appended"]


def test_append_utf8_sig_create_missing_has_bom(tmp_path):
    """create_if_missing for utf-8-sig must write the EF BB BF BOM at offset 0."""
    from gc_core.utils.file_text import decode_text_bytes

    p = tmp_path / "created_sig.txt"
    res = json.loads(
        with_local_ctx(AppendToFile)(
            path=str(p), content="Hello", separator="",
            encoding="utf-8-sig", create_if_missing=True, make_dirs=True,
        )
    )
    assert res["ok"] and res["created"], res
    raw = p.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf", f"start BOM missing: {raw[:3]!r}"
    decoded = decode_text_bytes(raw, encoding="utf-8-sig")
    assert decoded.ok and decoded.text == "Hello"


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be", "utf-8-sig"])
def test_append_to_empty_existing_file_writes_bom(tmp_path, encoding):
    """An existing but empty (size==0) file takes the create PATH: BOM emitted.

    Without this, appending utf-16/utf-8-sig to a zero-byte file would produce a
    BOM-less file that later decodes incorrectly. size==0 → create-path (write_bytes
    with BOM) rather than append-path (no BOM).

    Путь записи — ещё не история пути: файл СУЩЕСТВОВАЛ, и ``created`` о нём
    врать не должен. Тот же смысл, что у соседа по неймспейсу
    (``write_to_file`` отдаёт ``created=not exists``), и та же величина, по
    которой журнал решает породу записи: назвав пустой файл созданным, откат
    получал право УДАЛИТЬ файл, который у пользователя уже был.
    """
    from gc_core.utils.file_text import decode_text_bytes

    p = tmp_path / "empty.dat"
    p.write_bytes(b"")  # exists but size == 0
    assert p.exists() and p.stat().st_size == 0

    res = json.loads(
        with_local_ctx(AppendToFile)(
            path=str(p), content="Hello", separator="", encoding=encoding,
        )
    )
    assert res["ok"], res
    assert res["created"] is False, res      # файл существовал — не создание
    raw = p.read_bytes()
    if encoding in ("utf-16", "utf-16-le"):
        assert raw[:2] == b"\xff\xfe", f"{encoding}: BOM missing: {raw[:2]!r}"
    elif encoding == "utf-16-be":
        assert raw[:2] == b"\xfe\xff", f"{encoding}: BOM missing: {raw[:2]!r}"
    else:  # utf-8-sig
        assert raw[:3] == b"\xef\xbb\xbf", f"{encoding}: BOM missing: {raw[:3]!r}"
    decoded = decode_text_bytes(raw, encoding=encoding)
    assert decoded.ok and decoded.text == "Hello"


# --- CRLF preservation (fix #2) ---

def test_append_to_crlf_file_preserves_crlf(tmp_path):
    """Appending \\n-delimited text to a CRLF file must not mix line endings.

    Regression for FS_UTF8_PROBLEMS.md case #2: ``fs_append_file`` wrote the chunk
    bytes as-is, so appending LF text to a CRLF file produced mixed endings
    (some \\r\\n, some lone \\n). Now the head is scanned for \\r\\n and the chunk
    is normalized to CRLF when the existing file uses CRLF.
    """
    p = tmp_path / "crlf.txt"
    p.write_bytes(b"line1\r\nline2\r\n")
    res = json.loads(
        with_local_ctx(AppendToFile)(
            path=str(p), content="added line3\nmore text", separator="\n\n",
        )
    )
    assert res["ok"], res
    raw = p.read_bytes()
    crlf = raw.count(b"\r\n")
    lone_lf = raw.count(b"\n") - crlf
    assert lone_lf == 0, f"mixed endings: lone LF={lone_lf}, raw={raw!r}"
    assert crlf == 5, f"unexpected CRLF count: {crlf}, raw={raw!r}"
    assert raw == b"line1\r\nline2\r\n\r\n\r\nadded line3\r\nmore text"


def test_append_to_crlf_file_reports_normalized_bytes(tmp_path):
    """bytes_appended must count what actually landed on disk.

    When the chunk is normalized from LF to CRLF (see test above), the byte
    count written is larger than the raw UTF-8 payload before normalization.
    The tool's report must match the real delta, not the pre-normalization
    payload length.
    """
    p = tmp_path / "crlf_report.txt"
    p.write_bytes(b"line1\r\nline2\r\n")
    size_before = p.stat().st_size
    res = json.loads(
        with_local_ctx(AppendToFile)(
            path=str(p), content="added line3\nmore text", separator="\n\n",
        )
    )
    assert res["ok"], res
    size_after = p.stat().st_size
    assert res["bytes_appended"] == size_after - size_before, res["bytes_appended"]


def test_append_to_lf_file_keeps_lf(tmp_path):
    """Appending to an LF file must NOT introduce CRLF endings."""
    p = tmp_path / "lf.txt"
    p.write_bytes(b"line1\nline2\n")
    res = json.loads(
        with_local_ctx(AppendToFile)(
            path=str(p), content="added line3\nmore text", separator="\n\n",
        )
    )
    assert res["ok"], res
    raw = p.read_bytes()
    crlf = raw.count(b"\r\n")
    lone_lf = raw.count(b"\n") - crlf
    assert crlf == 0, f"CRLF introduced into LF file: {crlf}, raw={raw!r}"
    assert lone_lf == 5
    assert raw == b"line1\nline2\n\n\nadded line3\nmore text"


def test_append_crlf_empty_separator(tmp_path):
    """CRLF preservation works with an empty separator too."""
    p = tmp_path / "crlf3.txt"
    p.write_bytes(b"a\r\nb\r\n")
    res = json.loads(
        with_local_ctx(AppendToFile)(
            path=str(p), content="c\nd", separator="",
        )
    )
    assert res["ok"], res
    raw = p.read_bytes()
    lone_lf = raw.count(b"\n") - raw.count(b"\r\n")
    assert lone_lf == 0, f"mixed endings: {raw!r}"
    assert raw == b"a\r\nb\r\nc\r\nd"
