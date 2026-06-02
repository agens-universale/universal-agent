import json

from tools.fs.file_info import FileInfo
from tools.fs.read_file import (
    ReadFile,
    DEFAULT_READ_FILE_CHAR_LIMIT,
    DEFAULT_READ_FILE_MAX_BYTES,
)
from utils.file_text import sha256_hex_of_text


def test_read_returns_sha256_for_full_file(tmp_path):
    text = "hello\nworld\n"
    p = tmp_path / "h.txt"
    p.write_text(text, encoding="utf-8")
    res = json.loads(ReadFile()(path=str(p), offset=1, limit=1))
    assert res["sha256"] == sha256_hex_of_text(text)
    info = json.loads(FileInfo()(path=str(p), include_hash=True))
    assert res["sha256"] == info["sha256"]


def test_read_default_preview_and_pagination(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("\n".join(f"line {i}" for i in range(1, 601)), encoding="utf-8")
    res = json.loads(ReadFile()(path=str(p)))
    assert res["file_size_bytes"] == p.stat().st_size
    assert res["start_line"] == 1
    assert res["end_line"] == 500
    assert res["truncated"] is True
    assert res["next_offset"] == 501
    assert res["content_truncated"] is False
    assert res["next_char_offset"] is None


def test_read_offset_limit(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
    by_page = json.loads(ReadFile()(path=str(p), offset=2, limit=2))
    assert by_page["content"] == "b\nc\n"


def test_read_errors_and_type_validation(tmp_path):
    tool = ReadFile()
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
    tool = ReadFile()
    bad_offset = json.loads(tool(path=str(p), offset="1"))  # type: ignore[arg-type]
    assert bad_offset["ok"] is False and bad_offset["error"]["field"] == "offset"
    bad_limit = json.loads(tool(path=str(p), limit="2"))  # type: ignore[arg-type]
    assert bad_limit["ok"] is False and bad_limit["error"]["field"] == "limit"
    clamped = json.loads(tool(path=str(p), offset=0, limit=0))
    assert clamped["start_line"] == 1 and clamped["end_line"] == 1


def test_single_huge_line_char_truncated(tmp_path):
    p = tmp_path / "one_line.log"
    p.write_text("x" * 50_000, encoding="utf-8")
    res = json.loads(ReadFile()(path=str(p)))
    assert res["total_lines"] == 1
    assert res["truncated"] is False
    assert res["total_chars"] == 50_000
    assert len(res["content"]) == DEFAULT_READ_FILE_CHAR_LIMIT
    assert res["content_truncated"] is True
    assert res["next_char_offset"] == DEFAULT_READ_FILE_CHAR_LIMIT
    assert res["char_offset"] == 0
    assert res["char_limit"] == DEFAULT_READ_FILE_CHAR_LIMIT


def test_char_offset_pagination_within_line_range(tmp_path):
    p = tmp_path / "one_line.log"
    p.write_text("a" * 25_000, encoding="utf-8")
    first = json.loads(ReadFile()(path=str(p), char_limit=10_000))
    second = json.loads(
        ReadFile()(path=str(p), char_offset=10_000, char_limit=10_000)
    )
    assert first["content"] == "a" * 10_000
    assert second["content"] == "a" * 10_000
    assert second["char_offset"] == 10_000
    assert second["content_truncated"] is True
    assert second["next_char_offset"] == 20_000
    third = json.loads(ReadFile()(path=str(p), char_offset=20_000, char_limit=10_000))
    assert third["content"] == "a" * 5_000
    assert third["content_truncated"] is False


def test_char_limit_applies_after_line_selection(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("aa\n" + "b" * 20_000 + "\ncc\n", encoding="utf-8")
    res = json.loads(ReadFile()(path=str(p), offset=2, limit=1, char_limit=5_000))
    assert res["start_line"] == 2
    assert res["end_line"] == 2
    assert res["content"] == "b" * 5_000
    assert res["total_chars"] == 20_001  # line includes trailing newline from readlines()
    assert res["content_truncated"] is True


def test_char_offset_validation(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("hi", encoding="utf-8")
    tool = ReadFile()
    bad = json.loads(tool(path=str(p), char_offset="0"))  # type: ignore[arg-type]
    assert bad["ok"] is False and bad["error"]["field"] == "char_offset"
    bad_limit = json.loads(tool(path=str(p), char_limit=0))
    assert bad_limit["ok"] is False and bad_limit["error"]["field"] == "char_limit"


def test_rejects_file_over_max_bytes(tmp_path):
    p = tmp_path / "huge.txt"
    p.write_bytes(b"x" * 200)
    res = json.loads(
        ReadFile(config_dict={"read_file_max_bytes": 100})(path=str(p))
    )
    assert res["ok"] is False
    assert res["error"]["type"] == "file_too_large"
    assert res["file_size_bytes"] == 200
    assert res["error"]["max_bytes"] == 100
    assert "200" in res["error"]["message"]
    assert "run_python_code" not in res["error"]["message"].lower()
    assert "execute_command" not in res["error"]["message"].lower()


def test_reads_file_at_max_bytes_boundary(tmp_path):
    p = tmp_path / "ok.txt"
    p.write_bytes(b"y" * 100)
    res = json.loads(
        ReadFile(config_dict={"read_file_max_bytes": 100})(path=str(p))
    )
    assert res["ok"] is True
    assert res["file_size_bytes"] == 100
    assert len(res["content"]) == 100


def test_file_size_bytes_on_not_found_and_success(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("ab", encoding="utf-8")
    ok = json.loads(ReadFile()(path=str(p)))
    assert ok["file_size_bytes"] == 2
    missing = json.loads(ReadFile()(path=str(tmp_path / "nope.txt")))
    assert missing["file_size_bytes"] is None


def test_default_max_bytes_constant_is_modest():
    assert DEFAULT_READ_FILE_MAX_BYTES == 8 * 1024 * 1024


def test_unicode_char_slice(tmp_path):
    p = tmp_path / "u.txt"
    p.write_text("A😀" * 3, encoding="utf-8")
    res = json.loads(ReadFile()(path=str(p), char_limit=2))
    assert res["content"] == "A😀"
    assert res["total_chars"] == 6
