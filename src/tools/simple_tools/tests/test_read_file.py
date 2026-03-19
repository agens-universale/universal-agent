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


def test_read_offset_limit_and_view_range(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
    by_page = json.loads(ReadFile()(path=str(p), offset=2, limit=2))
    assert by_page["content"] == "b\nc\n"
    by_range = json.loads(ReadFile()(path=str(p), view_range="3,10"))
    assert by_range["content"] == "c\nd\ne\n"


def test_read_errors_and_type_validation(tmp_path):
    tool = ReadFile()
    assert "must be a string" in tool(path=123)  # type: ignore[arg-type]
    assert "File not found" in tool(path=str(tmp_path / "missing.txt"))
    d = tmp_path / "dir"
    d.mkdir()
    assert "is a directory" in tool(path=str(d))


def test_read_view_range_validation_and_clamp(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("1\n2\n3\n", encoding="utf-8")
    tool = ReadFile()
    assert "exactly two comma-separated" in tool(path=str(p), view_range="1")
    assert "must be integers" in tool(path=str(p), view_range="a,b")
    clamped = json.loads(tool(path=str(p), view_range="0,0"))
    assert clamped["start_line"] == 1 and clamped["end_line"] == 1
