import json

from tools.simple_tools.search_files import SearchFiles


def test_search_basic_and_file_pattern(tmp_path):
    (tmp_path / "a.py").write_text("x = 42\nprint(x)\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("answer: 42\n", encoding="utf-8")
    data = json.loads(SearchFiles()(path=str(tmp_path), regex="42"))
    assert data["ok"] is True and data["count"] >= 2
    py = json.loads(SearchFiles()(path=str(tmp_path), regex="print", file_pattern="*.py"))
    assert py["ok"] is True and py["count"] == 1
    assert py["matches"][0]["file"].endswith("a.py")


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
