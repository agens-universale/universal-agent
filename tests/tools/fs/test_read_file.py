import json

from gc_core.tools.fs.file_info import FileInfo
from gc_core.tools.fs.read_file import ReadFile, DEFAULT_READ_FILE_CHAR_LIMIT, DEFAULT_READ_FILE_MAX_BYTES
from gc_core.utils.file_text import sha256_hex_of_bytes


from tests.helpers.tool_ctx import with_local_ctx

def test_read_returns_sha256_for_full_file(tmp_path):
    text = "hello\nworld\n"
    p = tmp_path / "h.txt"
    p.write_text(text, encoding="utf-8")
    res = json.loads(with_local_ctx(ReadFile)(path=str(p), offset=1, limit=1))
    assert res["sha256"] == sha256_hex_of_bytes(text.encode("utf-8"))
    info = json.loads(with_local_ctx(FileInfo)(path=str(p), include_hash=True))
    assert res["sha256"] == info["sha256"]


def test_read_rejects_nul_binary_without_content(tmp_path):
    p = tmp_path / "blob.bin"
    p.write_bytes(b"hello\x00world")
    res = json.loads(with_local_ctx(ReadFile)(path=str(p)))
    assert res["ok"] is False
    assert res["error"]["type"] == "is_binary"
    assert res["content"] in ("", None)
    assert "\x00" not in (res.get("content") or "")


def test_read_utf16_le_bom_returns_text_and_encoding_used(tmp_path):
    p = tmp_path / "utf16.txt"
    p.write_bytes(b"\xff\xfeH\x00i\x00\n\x00")
    res = json.loads(with_local_ctx(ReadFile)(path=str(p)))
    assert res["ok"] is True
    assert res["encoding_used"] == "utf-16-le"
    assert res["content"] == "Hi\n"


def test_read_utf8_returns_encoding_used(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hello\n", encoding="utf-8")
    res = json.loads(with_local_ctx(ReadFile)(path=str(p)))
    assert res["ok"] is True
    assert res["encoding_used"] == "utf-8"
    assert res["content"] == "hello\n"


def test_read_cp1251_requires_encoding(tmp_path):
    text = (
        "Уважаемые коллеги! Настоящим сообщаем, что заседание рабочей группы "
        "по внедрению системы состоится 15 марта 2024 года в переговорной.\n"
    ) * 3
    p = tmp_path / "legacy.txt"
    p.write_bytes(text.encode("cp1251"))
    res = json.loads(with_local_ctx(ReadFile)(path=str(p)))
    assert res["ok"] is False
    assert res["error"]["type"] == "encoding_required"
    assert res["content"] == ""
    assert res["encoding_guess"] == "cp1251"
    assert res["encoding_confidence"] >= 0.5
    assert "Уважаемые" in res["preview"]
    assert res["preview_encoding"] == "cp1251"
    assert res["preview_truncated"] is False


def test_read_cp1251_with_explicit_encoding_and_alias(tmp_path):
    text = "Привет, мир\n"
    p = tmp_path / "legacy.txt"
    p.write_bytes(text.encode("cp1251"))
    res = json.loads(with_local_ctx(ReadFile)(path=str(p), encoding="cp1251"))
    assert res["ok"] is True
    assert res["encoding_used"] == "cp1251"
    assert res["content"] == text

    aliased = json.loads(with_local_ctx(ReadFile)(path=str(p), encoding="windows-1251"))
    assert aliased["ok"] is True
    assert aliased["encoding_used"] == "cp1251"
    assert aliased["content"] == text


def test_read_unsupported_encoding(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hi\n", encoding="utf-8")
    res = json.loads(with_local_ctx(ReadFile)(path=str(p), encoding="ibm866"))
    assert res["ok"] is False
    assert res["error"]["type"] == "unsupported_encoding"


def test_read_undecodable_text(tmp_path):
    p = tmp_path / "legacy.txt"
    p.write_bytes("Привет".encode("cp1251"))
    res = json.loads(with_local_ctx(ReadFile)(path=str(p), encoding="utf-8"))
    assert res["ok"] is False
    assert res["error"]["type"] == "undecodable_text"


def test_read_default_selects_whole_file(tmp_path):
    """Without offset/limit: whole file selected; preview is char_limit, not 500 lines."""
    lines = [f"line {i}" for i in range(1, 601)]
    text = "\n".join(lines)
    p = tmp_path / "big.txt"
    p.write_text(text, encoding="utf-8")
    res = json.loads(with_local_ctx(ReadFile)(path=str(p)))
    assert res["file_size_bytes"] == p.stat().st_size
    assert res["start_line"] == 1
    assert res["end_line"] == 600
    assert res["total_lines"] == 600
    assert res["truncated"] is False
    assert res["next_offset"] is None
    assert res["total_chars"] == len(text)
    # 600 short lines fit in default char_limit
    assert res["content_truncated"] is False
    assert res["next_char_offset"] is None
    assert res["content"] == text


def test_read_offset_without_limit_reads_to_eof(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("\n".join(f"line {i}" for i in range(1, 601)), encoding="utf-8")
    res = json.loads(with_local_ctx(ReadFile)(path=str(p), offset=501))
    assert res["start_line"] == 501
    assert res["end_line"] == 600
    assert res["truncated"] is False
    assert res["next_offset"] is None


def test_char_offset_reaches_past_former_500_line_cap(tmp_path):
    """Char pagination without line args must cover the whole file, including past line 500."""
    # Lines 1–500: 10 chars each ("aaaaaaaaa\n"); lines 501–516: marker prefix
    head = ["a" * 9 for _ in range(500)]
    tail = [f"TAIL{i:03d}" + "x" * 5 for i in range(16)]
    lines = head + tail
    text = "\n".join(lines) + "\n"
    p = tmp_path / "wide.jsonl"
    p.write_text(text, encoding="utf-8")

    head_chars = sum(len(l) + 1 for l in head)  # include newlines
    assert head_chars == 5000

    res = json.loads(
        with_local_ctx(ReadFile)(
            path=str(p), char_offset=head_chars, char_limit=50
        )
    )
    assert res["ok"] is True
    assert res["total_lines"] == 516
    assert res["end_line"] == 516
    assert res["truncated"] is False
    assert res["total_chars"] == len(text)
    assert res["content"].startswith("TAIL000")
    assert res["content"] == text[head_chars : head_chars + 50]


def test_explicit_line_slice_unchanged(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
    by_page = json.loads(with_local_ctx(ReadFile)(path=str(p), offset=2, limit=2))
    assert by_page["content"] == "b\nc\n"
    assert by_page["total_chars"] == 4  # "b\nc\n"
    assert by_page["truncated"] is True
    assert by_page["next_offset"] == 4


def test_read_errors_and_type_validation(tmp_path):
    tool = with_local_ctx(ReadFile)
    bad_type = json.loads(tool(path=123))  # type: ignore[arg-type]
    assert bad_type["ok"] is False and bad_type["error"]["field"] == "path"
    missing = json.loads(tool(path=str(tmp_path / "missing.txt")))
    assert missing["ok"] is False and missing["error"]["type"] == "not_found"
    d = tmp_path / "dir"
    d.mkdir()
    as_dir = json.loads(tool(path=str(d)))
    assert as_dir["ok"] is False and as_dir["error"]["type"] == "not_a_file"


def test_read_offset_limit_validation_and_clamp(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("1\n2\n3\n", encoding="utf-8")
    tool = with_local_ctx(ReadFile)
    bad_offset = json.loads(tool(path=str(p), offset="1"))  # type: ignore[arg-type]
    assert bad_offset["ok"] is False and bad_offset["error"]["field"] == "offset"
    bad_limit = json.loads(tool(path=str(p), limit="2"))  # type: ignore[arg-type]
    assert bad_limit["ok"] is False and bad_limit["error"]["field"] == "limit"
    clamped = json.loads(tool(path=str(p), offset=0, limit=0))
    assert clamped["start_line"] == 1 and clamped["end_line"] == 1


def test_single_huge_line_char_truncated(tmp_path):
    p = tmp_path / "one_line.log"
    oversized = DEFAULT_READ_FILE_CHAR_LIMIT + 50_000
    p.write_text("x" * oversized, encoding="utf-8")
    res = json.loads(with_local_ctx(ReadFile)(path=str(p)))
    assert res["total_lines"] == 1
    assert res["truncated"] is False
    assert res["total_chars"] == oversized
    assert res["returned_chars"] == DEFAULT_READ_FILE_CHAR_LIMIT
    assert len(res["content"]) == DEFAULT_READ_FILE_CHAR_LIMIT
    assert res["content_truncated"] is True
    assert res["next_char_offset"] == DEFAULT_READ_FILE_CHAR_LIMIT
    assert res["char_offset"] == 0
    assert res["char_limit"] == DEFAULT_READ_FILE_CHAR_LIMIT
    assert "returned 100000 of 150000" in res["hint"]
    assert "50000 remain" in res["hint"]
    assert f"char_offset={DEFAULT_READ_FILE_CHAR_LIMIT}" in res["hint"]


def test_mid_size_file_fits_default_char_limit(tmp_path):
    """Files in the old 10k–100k band must not be silently truncated."""
    p = tmp_path / "mid.txt"
    text = "y" * 50_000
    p.write_text(text, encoding="utf-8")
    res = json.loads(with_local_ctx(ReadFile)(path=str(p)))
    assert res["ok"] is True
    assert res["total_chars"] == 50_000
    assert res["returned_chars"] == 50_000
    assert res["content"] == text
    assert res["content_truncated"] is False
    assert res["next_char_offset"] is None
    assert res["char_limit"] == DEFAULT_READ_FILE_CHAR_LIMIT
    assert "hint" not in res


def test_char_offset_pagination_within_line_range(tmp_path):
    p = tmp_path / "one_line.log"
    p.write_text("a" * 25_000, encoding="utf-8")
    first = json.loads(with_local_ctx(ReadFile)(path=str(p), char_limit=10_000))
    second = json.loads(
        with_local_ctx(ReadFile)(path=str(p), char_offset=10_000, char_limit=10_000)
    )
    assert first["content"] == "a" * 10_000
    assert second["content"] == "a" * 10_000
    assert second["char_offset"] == 10_000
    assert second["content_truncated"] is True
    assert second["next_char_offset"] == 20_000
    third = json.loads(with_local_ctx(ReadFile)(path=str(p), char_offset=20_000, char_limit=10_000))
    assert third["content"] == "a" * 5_000
    assert third["content_truncated"] is False


def test_default_char_limit_constant():
    assert DEFAULT_READ_FILE_CHAR_LIMIT == 100_000


def test_char_limit_applies_after_line_selection(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("aa\n" + "b" * 20_000 + "\ncc\n", encoding="utf-8")
    res = json.loads(with_local_ctx(ReadFile)(path=str(p), offset=2, limit=1, char_limit=5_000))
    assert res["start_line"] == 2
    assert res["end_line"] == 2
    assert res["content"] == "b" * 5_000
    assert res["total_chars"] == 20_001  # line includes trailing newline from readlines()
    assert res["content_truncated"] is True


def test_char_offset_validation(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("hi", encoding="utf-8")
    tool = with_local_ctx(ReadFile)
    bad = json.loads(tool(path=str(p), char_offset="0"))  # type: ignore[arg-type]
    assert bad["ok"] is False and bad["error"]["field"] == "char_offset"
    bad_limit = json.loads(tool(path=str(p), char_limit=0))
    assert bad_limit["ok"] is False and bad_limit["error"]["field"] == "char_limit"


def test_rejects_file_over_max_bytes(tmp_path):
    p = tmp_path / "huge.txt"
    p.write_bytes(b"x" * 200)
    res = json.loads(
        with_local_ctx(ReadFile, config_dict={"fs_tools": {"max_read_bytes": 100}})(path=str(p))
    )
    assert res["ok"] is False
    assert res["error"]["type"] == "file_too_large"
    assert res["file_size_bytes"] == 200
    assert res["error"]["max_bytes"] == 100
    assert "200" in res["error"]["message"]
    assert "exec_python" not in res["error"]["message"].lower()
    assert "exec_command" not in res["error"]["message"].lower()


def test_reads_file_at_max_bytes_boundary(tmp_path):
    p = tmp_path / "ok.txt"
    p.write_bytes(b"y" * 100)
    res = json.loads(
        with_local_ctx(ReadFile, config_dict={"fs_tools": {"max_read_bytes": 100}})(path=str(p))
    )
    assert res["ok"] is True
    assert res["file_size_bytes"] == 100
    assert len(res["content"]) == 100


def test_file_size_bytes_on_not_found_and_success(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("ab", encoding="utf-8")
    ok = json.loads(with_local_ctx(ReadFile)(path=str(p)))
    assert ok["file_size_bytes"] == 2
    missing = json.loads(with_local_ctx(ReadFile)(path=str(tmp_path / "nope.txt")))
    assert missing["file_size_bytes"] is None


def test_read_file_via_symlink(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("hello via link", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    res = json.loads(with_local_ctx(ReadFile)(path=str(link)))
    assert res["ok"] is True
    assert "hello via link" in res["content"]


def test_default_max_bytes_constant_is_modest():
    assert DEFAULT_READ_FILE_MAX_BYTES == 8 * 1024 * 1024


def test_unicode_char_slice(tmp_path):
    p = tmp_path / "u.txt"
    p.write_text("A😀" * 3, encoding="utf-8")
    res = json.loads(with_local_ctx(ReadFile)(path=str(p), char_limit=2))
    assert res["content"] == "A😀"
    assert res["total_chars"] == 6
