import json

from tools.simple_tools.read_file import ReadFile


def test_read_default_preview_and_pagination(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("\n".join(f"line {i}" for i in range(1, 601)), encoding="utf-8")
    res = json.loads(ReadFile()(path=str(p)))
    assert res["start_line"] == 1
    assert res["end_line"] == 500
    assert res["truncated"] is True
    assert res["next_offset"] == 501


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
