import json

from gc_core.tools.fs.file_info import FileInfo
from gc_core.tools.fs.read_file import ReadFile
from gc_core.utils.fs_size_limits import DEFAULT_FS_READ_MAX_BYTES
from gc_core.utils.file_text import sha256_hex_of_bytes
from gc_testkit.helpers.crossplatform import require_symlinks
from gc_testkit.helpers.tool_ctx import with_local_ctx

_CHAR_PAGING_KEYS = (
    "char_offset",
    "char_limit",
    "content_truncated",
    "next_char_offset",
)


def _read(tmp_path, text, **kwargs):
    p = tmp_path / "f.txt"
    p.write_text(
        text, encoding="utf-8", newline=""
    )  # иначе на Windows на диске CRLF
    return json.loads(with_local_ctx(ReadFile)(path=str(p), **kwargs)), p


def test_read_returns_sha256_for_full_file(tmp_path):
    text = "hello\nworld\n"
    p = tmp_path / "h.txt"
    p.write_text(text, encoding="utf-8", newline="")
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
    p.write_text("hello\n", encoding="utf-8", newline="")
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

    aliased = json.loads(
        with_local_ctx(ReadFile)(path=str(p), encoding="windows-1251")
    )
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
    lines = [f"line {i}" for i in range(1, 601)]
    text = "\n".join(lines)
    res, p = _read(tmp_path, text)
    assert res["file_size_bytes"] == p.stat().st_size
    assert res["start_line"] == 1
    assert res["end_line"] == 600
    assert res["total_lines"] == 600
    assert res["truncated"] is False
    assert res["next_offset"] is None
    assert res["content"] == text
    for key in _CHAR_PAGING_KEYS:
        assert key not in res
    assert "hint" not in res


def test_read_offset_without_limit_reads_to_eof(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text(
        "\n".join(f"line {i}" for i in range(1, 601)), encoding="utf-8"
    )
    res = json.loads(with_local_ctx(ReadFile)(path=str(p), offset=501))
    assert res["start_line"] == 501
    assert res["end_line"] == 600
    assert res["truncated"] is False
    assert res["next_offset"] is None


def test_explicit_line_slice_unchanged(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("a\nb\nc\nd\ne\n", encoding="utf-8", newline="")
    by_page = json.loads(
        with_local_ctx(ReadFile)(path=str(p), offset=2, limit=2)
    )
    assert by_page["content"] == "b\nc\n"
    assert by_page["truncated"] is True
    assert by_page["next_offset"] == 4


def test_line_window_sixty_short_lines_is_inline_json(tmp_path):
    lines = [f"code {i}" for i in range(1, 121)]
    text = "\n".join(lines) + "\n"
    p = tmp_path / "mod.py"
    p.write_text(
        text, encoding="utf-8", newline=""
    )  # иначе на Windows на диске CRLF
    res = json.loads(
        with_local_ctx(ReadFile)(path=str(p), offset=10, limit=60)
    )
    assert res["ok"] is True
    assert res["start_line"] == 10
    assert res["end_line"] == 69
    assert res["total_lines"] == 120
    assert res["truncated"] is True
    assert res["next_offset"] == 70
    assert res["content"] == "\n".join(lines[9:69]) + "\n"
    assert "type" not in res
    for key in _CHAR_PAGING_KEYS:
        assert key not in res


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
    assert (
        bad_offset["ok"] is False and bad_offset["error"]["field"] == "offset"
    )
    bad_limit = json.loads(tool(path=str(p), limit="2"))  # type: ignore[arg-type]
    assert bad_limit["ok"] is False and bad_limit["error"]["field"] == "limit"
    clamped = json.loads(tool(path=str(p), offset=0, limit=0))
    # offset=0 клампится к 1; limit=0 — «не задан» (до конца файла), не одна строка
    assert clamped["start_line"] == 1 and clamped["end_line"] == 3


def test_single_huge_line_not_sliced_by_read(tmp_path):
    p = tmp_path / "one_line.log"
    text = "x" * 150_000
    p.write_text(text, encoding="utf-8")
    res = json.loads(with_local_ctx(ReadFile)(path=str(p)))
    assert res["ok"] is True
    assert res["total_lines"] == 1
    assert res["truncated"] is False
    assert res["next_offset"] is None
    assert res["content"] == text
    assert res["returned_chars"] == 150_000
    for key in _CHAR_PAGING_KEYS:
        assert key not in res
    assert "hint" not in res


def test_rejects_file_over_max_bytes(tmp_path):
    p = tmp_path / "huge.txt"
    p.write_bytes(b"x" * 200)
    res = json.loads(
        with_local_ctx(ReadFile, {"fs_tools": {"max_read_bytes": 100}})(
            path=str(p)
        )
    )
    assert res["ok"] is False
    assert res["error"]["type"] == "file_too_large"
    assert res["file_size_bytes"] == 200
    assert res["error"]["max_bytes"] == 100
    assert "200" in res["error"]["message"]
    assert "exec_python" not in res["error"]["message"].lower()
    assert "exec_command" not in res["error"]["message"].lower()
    for key in _CHAR_PAGING_KEYS:
        assert key not in res


def test_reads_file_at_max_bytes_boundary(tmp_path):
    p = tmp_path / "ok.txt"
    p.write_bytes(b"y" * 100)
    res = json.loads(
        with_local_ctx(ReadFile, {"fs_tools": {"max_read_bytes": 100}})(
            path=str(p)
        )
    )
    assert res["ok"] is True
    assert res["file_size_bytes"] == 100
    assert len(res["content"]) == 100


def test_file_size_bytes_on_not_found_and_success(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("ab", encoding="utf-8")
    ok = json.loads(with_local_ctx(ReadFile)(path=str(p)))
    assert ok["file_size_bytes"] == 2
    missing = json.loads(
        with_local_ctx(ReadFile)(path=str(tmp_path / "nope.txt"))
    )
    assert missing["file_size_bytes"] is None


def test_read_file_via_symlink(tmp_path):
    require_symlinks(tmp_path)
    real = tmp_path / "real.txt"
    real.write_text("hello via link", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    res = json.loads(with_local_ctx(ReadFile)(path=str(link)))
    assert res["ok"] is True
    assert "hello via link" in res["content"]


def test_default_max_bytes_constant_is_modest():
    assert DEFAULT_FS_READ_MAX_BYTES == 8 * 1024 * 1024


def test_unicode_content_not_char_sliced(tmp_path):
    p = tmp_path / "u.txt"
    text = "A😀" * 3
    p.write_text(text, encoding="utf-8")
    res = json.loads(with_local_ctx(ReadFile)(path=str(p)))
    assert res["content"] == text
    assert res["returned_chars"] == 6


def test_schema_and_prompt_have_line_axis_only():
    spec = ReadFile.tool_description()
    props = spec["parameters"]["properties"]
    assert set(props) == {"path", "offset", "limit", "encoding"}
    assert spec["parameters"]["required"] == ["path"]
    blob = json.dumps(spec) + ReadFile.prompt_description()
    for token in _CHAR_PAGING_KEYS:
        assert token not in blob
    assert "next_char_offset" not in blob
    prompt = ReadFile.prompt_description()
    assert "truncated" in prompt
    assert "next_offset" in prompt
    assert "session_read_stored_tool_output" not in prompt


def test_limit_zero_means_unset_not_one_line(tmp_path):
    """limit=0 — «не задан» (до конца файла): strict-схема заставляет модель слать
    все поля, и «не использую» она говорит нулём. Кламп до одной строки отдавал
    успех с неверными данными — модель считала файл однострочным."""
    p = tmp_path / "f.txt"
    p.write_text("l1\nl2\nl3\n", encoding="utf-8", newline="")
    tool = with_local_ctx(ReadFile)

    zero = json.loads(tool(path=str(p), limit=0))
    assert zero["ok"] is True
    assert zero["end_line"] == 3 and zero["content"] == "l1\nl2\nl3\n"

    negative = json.loads(tool(path=str(p), offset=2, limit=-1))
    assert negative["ok"] is True
    assert negative["start_line"] == 2 and negative["end_line"] == 3
