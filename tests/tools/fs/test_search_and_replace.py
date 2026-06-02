import json

from tools.fs.search_and_replace import SearchAndReplace


def test_replace_first_occurrence_only(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("a b a", encoding="utf-8")
    res = json.loads(SearchAndReplace()(path=str(p), search="a", replace="X"))
    assert res["ok"] is True and res["replaced"] == 1
    assert p.read_text(encoding="utf-8") == "X b a"


def test_replace_multiline_and_not_found(tmp_path):
    p = tmp_path / "m.txt"
    p.write_text("line1\nline2\nline3\n", encoding="utf-8")
    ok = json.loads(SearchAndReplace()(path=str(p), search="line1\nline2", replace="top"))
    assert ok["ok"] is True
    assert p.read_text(encoding="utf-8") == "top\nline3\n"
    miss = json.loads(SearchAndReplace()(path=str(p), search="absent", replace="x"))
    assert miss["ok"] is False and miss["matched"] is False and miss["error"] is None


def test_replace_validation(tmp_path):
    tool = SearchAndReplace()
    target = tmp_path / "x.txt"
    target.write_text("abc", encoding="utf-8")
    empty = json.loads(tool(path=str(target), search="", replace="x"))
    assert empty["ok"] is False and empty["error"]["field"] == "search"
    nf = json.loads(tool(path=str(tmp_path / "none.txt"), search="a", replace="b"))
    assert nf["ok"] is False and nf["error"]["type"] == "not_found"
