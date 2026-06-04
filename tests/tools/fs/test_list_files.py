import json
from pathlib import Path

from tools.fs.list_files import ListFiles


def test_list_default_and_hidden(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".env").write_text("x", encoding="utf-8")
    (tmp_path / "dir").mkdir()
    res = json.loads(ListFiles()(path=str(tmp_path)))
    assert res["ok"] is True
    assert res["count"] == 3
    by_name = {Path(item["path"]).name: item for item in res["entries"]}
    assert by_name[".env"]["is_hidden"] is True
    assert by_name["dir"]["type"] == "directory"


def test_list_depth_glob_and_truncate(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("print(1)", encoding="utf-8")
    (tmp_path / "src" / "b.txt").write_text("x", encoding="utf-8")
    res = json.loads(ListFiles()(path=str(tmp_path), max_depth=2, glob="**/*.py"))
    assert res["ok"] is True and res["count"] == 1
    assert Path(res["entries"][0]["path"]) == (tmp_path / "src" / "a.py").resolve()
    trunc = json.loads(ListFiles()(path=str(tmp_path), max_depth=2, max_entries=1))
    assert trunc["truncated"] is True and trunc["count"] == 1


def test_list_validation_and_not_found(tmp_path):
    tool = ListFiles()
    bad = json.loads(tool(path=123))  # type: ignore[arg-type]
    assert bad["ok"] is False and bad["error"]["field"] == "path"
    missing = json.loads(tool(path=str(tmp_path / "missing")))
    assert missing["ok"] is False and missing["error"]["type"] == "not_found"


def test_list_extra_validation_and_not_a_directory(tmp_path):
    tool = ListFiles()
    # Non-string optional glob is coalesced to absent (same as omit / "").
    coalesced_glob = json.loads(tool(path=str(tmp_path), glob=1))  # type: ignore[arg-type]
    assert coalesced_glob["ok"] is True and "glob" not in coalesced_glob
    bad_depth = json.loads(tool(path=str(tmp_path), max_depth=-1))
    assert bad_depth["ok"] is False and bad_depth["error"]["field"] == "max_depth"
    bad_entries = json.loads(tool(path=str(tmp_path), max_entries=0))
    assert bad_entries["ok"] is False and bad_entries["error"]["field"] == "max_entries"
    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    not_dir = json.loads(tool(path=str(f)))
    assert not_dir["ok"] is False and not_dir["error"]["type"] == "not_a_directory"
