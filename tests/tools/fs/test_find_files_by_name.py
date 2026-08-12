import json
from pathlib import Path

from tools.fs.find_files_by_name import FindFilesByName
from tests.helpers.tool_ctx import with_local_ctx


def _tool():
    return with_local_ctx(FindFilesByName)


def test_find_by_basename_anywhere(tmp_path: Path):
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    target = nested / "list_files.py"
    target.write_text("x", encoding="utf-8")
    (tmp_path / "other.txt").write_text("y", encoding="utf-8")

    res = json.loads(_tool()(path=str(tmp_path), query="list_files.py"))
    assert res["ok"] is True
    assert res["tool"] == "fs_find_files_by_name"
    assert res["count"] == 1
    m = res["matches"][0]
    assert Path(m["path"]) == target.resolve()
    assert m["type"] == "file"
    assert m["size_bytes"] == target.stat().st_size
    assert m["is_binary"] is False
    assert m["encoding_guess"] in ("utf-8", "ascii", None) or isinstance(
        m["encoding_guess"], str
    )
    assert res["truncated"] is False
    assert res["walk_stats"]["stop_reason"] == "completed"


def test_find_match_meta_binary_and_directory(tmp_path: Path):
    text = tmp_path / "note.txt"
    text.write_text("hello", encoding="utf-8")
    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\x00\x01\x02\xffbinary")
    d = tmp_path / "subdir"
    d.mkdir()

    text_res = json.loads(_tool()(path=str(tmp_path), query="note.txt"))
    assert text_res["ok"] and text_res["count"] == 1
    tm = text_res["matches"][0]
    assert tm["type"] == "file"
    assert tm["size_bytes"] == text.stat().st_size
    assert tm["is_binary"] is False
    assert tm["encoding_guess"] is not None

    bin_res = json.loads(_tool()(path=str(tmp_path), query="blob.bin"))
    assert bin_res["ok"] and bin_res["count"] == 1
    bm = bin_res["matches"][0]
    assert bm["type"] == "file"
    assert bm["size_bytes"] == binary.stat().st_size
    assert bm["is_binary"] is True
    assert bm["encoding_guess"] is None

    dir_res = json.loads(_tool()(path=str(tmp_path), query="subdir"))
    assert dir_res["ok"] and dir_res["count"] >= 1
    dm = next(m for m in dir_res["matches"] if m["type"] == "directory")
    assert Path(dm["path"]) == d.resolve()
    assert dm["size_bytes"] is None
    assert dm["is_binary"] is None
    assert dm["encoding_guess"] is None


def test_find_by_name_fragment_and_path_fragment(tmp_path: Path):
    d = tmp_path / "tools" / "fs"
    d.mkdir(parents=True)
    f = d / "find_files_by_name.py"
    f.write_text("x", encoding="utf-8")

    by_frag = json.loads(_tool()(path=str(tmp_path), query="find_files"))
    assert by_frag["ok"] and by_frag["count"] >= 1
    assert any(Path(m["path"]) == f.resolve() for m in by_frag["matches"])

    by_path = json.loads(_tool()(path=str(tmp_path), query="tools/fs"))
    assert by_path["ok"] and by_path["count"] >= 1
    paths = {Path(m["path"]) for m in by_path["matches"]}
    assert f.resolve() in paths or d.resolve() in paths


def test_find_glob_recursive_basename(tmp_path: Path):
    (tmp_path / "src").mkdir()
    py = tmp_path / "src" / "a.py"
    py.write_text("print(1)", encoding="utf-8")
    (tmp_path / "src" / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "root.py").write_text("x", encoding="utf-8")

    res = json.loads(_tool()(path=str(tmp_path), query="*.py"))
    assert res["ok"] is True
    names = {Path(m["path"]).name for m in res["matches"]}
    assert names == {"a.py", "root.py"}


def test_find_skips_excluded_dirs(tmp_path: Path):
    hidden_nest = tmp_path / "node_modules" / "pkg"
    hidden_nest.mkdir(parents=True)
    (hidden_nest / "secret.py").write_text("x", encoding="utf-8")
    visible = tmp_path / "app" / "secret.py"
    visible.parent.mkdir(parents=True)
    visible.write_text("x", encoding="utf-8")

    res = json.loads(_tool()(path=str(tmp_path), query="secret.py"))
    assert res["ok"] is True
    assert res["count"] == 1
    assert Path(res["matches"][0]["path"]) == visible.resolve()


def test_find_skips_nested_dot_dirs(tmp_path: Path):
    dot = tmp_path / ".cache" / "x"
    dot.mkdir(parents=True)
    (dot / "hidden_name.py").write_text("x", encoding="utf-8")
    ok = tmp_path / "hidden_name.py"
    ok.write_text("x", encoding="utf-8")

    res = json.loads(_tool()(path=str(tmp_path), query="hidden_name.py"))
    assert res["ok"] is True
    assert res["count"] == 1
    assert Path(res["matches"][0]["path"]) == ok.resolve()


def test_find_max_matches_truncates(tmp_path: Path):
    for i in range(5):
        (tmp_path / f"f{i}.dat").write_text("x", encoding="utf-8")

    res = json.loads(_tool()(path=str(tmp_path), query="*.dat", max_matches=2))
    assert res["ok"] is True
    assert res["count"] == 2
    assert res["truncated"] is True
    assert res["walk_stats"]["stop_reason"] == "max_matches"


def test_find_max_matches_hard_cap_200(tmp_path: Path):
    for i in range(5):
        (tmp_path / f"f{i}.dat").write_text("x", encoding="utf-8")

    res = json.loads(_tool()(path=str(tmp_path), query="*.dat", max_matches=500))
    assert res["ok"] is True
    assert res["max_matches"] == 200
    assert res["count"] == 5
    assert res["truncated"] is False


def test_find_case_insensitive(tmp_path: Path):
    f = tmp_path / "ReadMe.MD"
    f.write_text("x", encoding="utf-8")

    # Default is case-insensitive.
    default = json.loads(_tool()(path=str(tmp_path), query="readme.md"))
    assert default["ok"] is True
    assert default["case_insensitive"] is True
    assert default["count"] == 1
    assert Path(default["matches"][0]["path"]) == f.resolve()

    cs = json.loads(_tool()(path=str(tmp_path), query="readme.md", case_insensitive=False))
    assert cs["ok"] is True
    assert cs["count"] == 0

    ci = json.loads(_tool()(path=str(tmp_path), query="readme.md", case_insensitive=True))
    assert ci["ok"] is True
    assert ci["count"] == 1
    assert Path(ci["matches"][0]["path"]) == f.resolve()


def test_find_validation(tmp_path: Path):
    tool = _tool()
    empty = json.loads(tool(path=str(tmp_path), query="  "))
    assert empty["ok"] is False and empty["error"]["field"] == "query"

    missing = json.loads(tool(path=str(tmp_path / "nope"), query="x"))
    assert missing["ok"] is False and missing["error"]["type"] == "not_found"

    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    not_dir = json.loads(tool(path=str(f), query="x"))
    assert not_dir["ok"] is False and not_dir["error"]["type"] == "not_a_directory"


def test_registered_name():
    assert FindFilesByName.registered_tool_name() == "fs_find_files_by_name"
