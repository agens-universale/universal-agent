import json

from tools.simple_tools.append_to_file import AppendToFile


def test_append_success_existing_file(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("start", encoding="utf-8")
    res = json.loads(AppendToFile()(path=str(p), content="next"))
    assert res["ok"] is True
    assert res["created"] is False
    assert res["separator"] == "\n\n"
    assert p.read_text(encoding="utf-8") == "start\n\nnext"


def test_append_create_missing_with_dirs(tmp_path):
    p = tmp_path / "a" / "b" / "c.txt"
    res = json.loads(
        AppendToFile()(path=str(p), content="hello", create_if_missing=True, make_dirs=True, separator="")
    )
    assert res["ok"] is True
    assert res["created"] is True
    assert p.read_text(encoding="utf-8") == "hello"


def test_append_errors(tmp_path):
    tool = AppendToFile()
    rel = json.loads(tool(path="rel.txt", content="x"))
    assert rel["ok"] is False and rel["error"]["field"] == "path"
    miss = json.loads(tool(path=str(tmp_path / "missing.txt"), content="x"))
    assert miss["ok"] is False and miss["error"]["type"] == "not_found"
    bad_sep = json.loads(tool(path=str(tmp_path / "x.txt"), content="x", separator=123))
    assert bad_sep["ok"] is False and bad_sep["error"]["field"] == "separator"
