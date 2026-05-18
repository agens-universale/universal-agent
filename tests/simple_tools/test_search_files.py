import json

from tools.simple_tools.search_files import SearchFiles


def test_search_basic_and_file_pattern(tmp_path):
    (tmp_path / "a.py").write_text("x = 42\nprint(x)\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("answer: 42\n", encoding="utf-8")
    data = json.loads(SearchFiles()(path=str(tmp_path), regex="42"))
    assert data["ok"] is True
    assert data["total_matches"] == 2 and data["count"] == 2
    assert data["truncated"] is False and data["next_offset"] is None
    py = json.loads(SearchFiles()(path=str(tmp_path), regex="print", file_pattern="*.py"))
    assert py["ok"] is True and py["total_matches"] == 1 and py["count"] == 1
    assert py["matches"][0]["file"].endswith("a.py")


def test_search_pagination_and_offset_beyond(tmp_path):
    (tmp_path / "a.py").write_text("ALPHA\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("ALPHA\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("ALPHA\n", encoding="utf-8")

    tool = SearchFiles()
    p1 = json.loads(tool(path=str(tmp_path), regex="ALPHA", file_pattern="*.py", offset=1, limit=2))
    assert p1["ok"] and p1["total_matches"] == 3
    assert p1["count"] == 2 and len(p1["matches"]) == 2
    assert p1["truncated"] is True and p1["next_offset"] == 3

    p2 = json.loads(tool(path=str(tmp_path), regex="ALPHA", file_pattern="*.py", offset=3, limit=2))
    assert p2["ok"] and p2["total_matches"] == 3 and p2["count"] == 1
    assert p2["truncated"] is False and p2["next_offset"] is None

    empty_page = json.loads(tool(path=str(tmp_path), regex="ALPHA", offset=99, limit=10))
    assert empty_page["ok"] and empty_page["total_matches"] == 3
    assert empty_page["count"] == 0 and empty_page["matches"] == []


def test_search_invalid_pagination_offsets(tmp_path):
    (tmp_path / "z.txt").write_text("ALPHA\n", encoding="utf-8")
    tool = SearchFiles()
    bad_off = json.loads(tool(path=str(tmp_path), regex="ALPHA", offset=0, limit=1))
    assert bad_off["ok"] is False and bad_off["error"]["field"] == "offset"
    bad_lim = json.loads(tool(path=str(tmp_path), regex="ALPHA", offset=1, limit=0))
    assert bad_lim["ok"] is False and bad_lim["error"]["field"] == "limit"


def test_search_invalid_and_not_found(tmp_path):
    tool = SearchFiles()
    bad_regex = json.loads(tool(path=str(tmp_path), regex="("))
    # implementation swallows regex errors per-file, so ok remains true with zero matches
    assert bad_regex["ok"] is True and bad_regex["count"] == 0
    missing = json.loads(tool(path=str(tmp_path / "none"), regex="x"))
    assert missing["ok"] is False and missing["error"]["type"] == "not_found"
    bad_path = json.loads(tool(path=1, regex="x"))  # type: ignore[arg-type]
    assert bad_path["ok"] is False and bad_path["error"]["field"] == "path"


def test_search_validation_and_directory_error(tmp_path):
    tool = SearchFiles()
    bad_regex_type = json.loads(tool(path=str(tmp_path), regex=123))  # type: ignore[arg-type]
    assert bad_regex_type["ok"] is False and bad_regex_type["error"]["field"] == "regex"
    bad_file_pattern_type = json.loads(tool(path=str(tmp_path), regex="x", file_pattern=1))  # type: ignore[arg-type]
    assert bad_file_pattern_type["ok"] is False and bad_file_pattern_type["error"]["field"] == "file_pattern"
    f = tmp_path / "single.txt"
    f.write_text("x", encoding="utf-8")
    not_dir = json.loads(tool(path=str(f), regex="x"))
    assert not_dir["ok"] is False and not_dir["error"]["type"] == "not_a_directory"
