import sys
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from gc_core.config_modules.fs_tools import FsFindFilesByNameConfig
from gc_core.tools.fs.find_files_by_name import FindFilesByName
from gc_testkit.helpers.tool_ctx import with_local_ctx


def _tool(extra=None):
    cfg = {}
    if extra:
        cfg["fs_tools"] = {"find_files_by_name": extra}
    return with_local_ctx(FindFilesByName, config_dict=cfg)


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
    assert "type" not in m
    assert m["size_bytes"] == target.stat().st_size
    assert "is_binary" not in m
    assert "is_hidden" not in m
    assert "encoding_guess" not in m
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
    assert "type" not in tm
    assert tm["size_bytes"] == text.stat().st_size
    assert "is_binary" not in tm
    assert "is_hidden" not in tm
    assert "encoding_guess" not in tm

    bin_res = json.loads(_tool()(path=str(tmp_path), query="blob.bin"))
    assert bin_res["ok"] and bin_res["count"] == 1
    bm = bin_res["matches"][0]
    assert "type" not in bm
    assert bm["size_bytes"] == binary.stat().st_size
    assert bm["is_binary"] is True
    assert "is_hidden" not in bm
    assert "encoding_guess" not in bm

    dir_res = json.loads(_tool()(path=str(tmp_path), query="subdir"))
    assert dir_res["ok"] and dir_res["count"] >= 1
    dm = next(m for m in dir_res["matches"] if m.get("type") == "directory")
    assert Path(dm["path"]) == d.resolve()
    assert "size_bytes" not in dm
    assert "is_binary" not in dm
    assert "is_hidden" not in dm
    assert "encoding_guess" not in dm


def test_find_match_meta_hidden_file(tmp_path: Path):
    hidden = tmp_path / ".notes.txt"
    hidden.write_text("secret", encoding="utf-8")

    res = json.loads(_tool()(path=str(tmp_path), query=".notes.txt"))
    assert res["ok"] and res["count"] == 1
    hm = res["matches"][0]
    assert Path(hm["path"]) == hidden.resolve()
    assert hm["is_hidden"] is True
    assert hm["size_bytes"] == hidden.stat().st_size
    assert "type" not in hm
    assert "is_binary" not in hm


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


def test_find_skips_venv_hard_floor(tmp_path: Path):
    nest = tmp_path / "venv" / "lib"
    nest.mkdir(parents=True)
    (nest / "secret.py").write_text("x", encoding="utf-8")
    visible = tmp_path / "secret.py"
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
    assert "max_matches" in res["guidance"]
    assert "max_visited" not in res["guidance"]


def test_summary_for_max_visited_does_not_blame_max_matches(tmp_path: Path):
    raw = _tool()._result(
        path=str(tmp_path),
        query="*",
        case_insensitive=True,
        max_matches=100,
        ok=True,
        matches=[],
        truncated=True,
        walk_stats={"visited": 500_001, "stop_reason": "max_visited", "max_visited": 500_000},
    )
    res = json.loads(raw)
    assert "max_visited" in res["guidance"]
    assert "Increase max_matches" not in res["guidance"]


def test_find_max_matches_hard_cap_300(tmp_path: Path):
    for i in range(5):
        (tmp_path / f"f{i}.dat").write_text("x", encoding="utf-8")

    res = json.loads(_tool()(path=str(tmp_path), query="*.dat", max_matches=500))
    assert res["ok"] is True
    assert res["max_matches"] == 300
    assert res["count"] == 5
    assert res["truncated"] is False
    assert "guidance" not in res


def _write_dat_files(tmp_path: Path, n: int) -> None:
    for i in range(n):
        (tmp_path / f"f{i}.dat").write_text("x", encoding="utf-8")


def test_config_default_max_matches_applies_when_caller_omits(tmp_path: Path):
    _write_dat_files(tmp_path, 5)
    res = json.loads(_tool({"default_max_matches": 2})(path=str(tmp_path), query="*.dat"))
    assert res["ok"] is True
    assert res["count"] == 2
    assert res["max_matches"] == 2
    assert res["truncated"] is True
    assert res["walk_stats"]["stop_reason"] == "max_matches"


def test_explicit_max_matches_wins_over_config_default(tmp_path: Path):
    _write_dat_files(tmp_path, 5)
    res = json.loads(
        _tool({"default_max_matches": 2})(
            path=str(tmp_path), query="*.dat", max_matches=4
        )
    )
    assert res["ok"] is True
    assert res["count"] == 4
    assert res["max_matches"] == 4


def test_config_hard_max_matches_clamps_requested(tmp_path: Path):
    _write_dat_files(tmp_path, 5)
    res = json.loads(
        _tool({"default_max_matches": 2, "hard_max_matches": 2})(
            path=str(tmp_path), query="*.dat", max_matches=50
        )
    )
    assert res["ok"] is True
    assert res["max_matches"] == 2
    assert res["count"] == 2
    assert res["truncated"] is True
    assert res["walk_stats"]["stop_reason"] == "max_matches"


def test_config_max_visited_is_the_walk_cap(tmp_path: Path):
    _write_dat_files(tmp_path, 5)
    res = json.loads(_tool({"max_visited": 2})(path=str(tmp_path), query="*.dat"))
    assert res["ok"] is True
    assert res["truncated"] is True
    assert res["walk_stats"]["stop_reason"] == "max_visited"
    assert res["walk_stats"]["max_visited"] == 2
    assert res["walk_stats"]["visited"] > 2
    assert "max_visited" in res["guidance"]
    assert "Increase max_matches" not in res["guidance"]


def test_find_files_by_name_config_rejects_non_positive_and_unknown_keys():
    for field in ("default_max_matches", "hard_max_matches", "max_visited"):
        with pytest.raises(ValidationError):
            FsFindFilesByNameConfig.model_validate({field: 0})
    with pytest.raises(ValidationError):
        FsFindFilesByNameConfig.model_validate({"default_max_visited": 10})
    with pytest.raises(ValidationError):
        FsFindFilesByNameConfig.model_validate(
            {"default_max_matches": 50, "hard_max_matches": 10}
        )


def test_tool_description_uses_live_config_limits():
    cfg = {
        "fs_tools": {
            "find_files_by_name": {
                "default_max_matches": 7,
                "hard_max_matches": 11,
            }
        }
    }
    spec = FindFilesByName.tool_description(config_dict=cfg)
    prompt = FindFilesByName.prompt_description(config_dict=cfg)
    mm = spec["parameters"]["properties"]["max_matches"]
    assert mm["maximum"] == 11
    assert "Default 7" in mm["description"]
    assert "maximum 11" in mm["description"]
    assert "default 7, maximum 11" in spec["description"]
    assert "max_matches (default 7, maximum 11)" in prompt


def test_builtin_tools_data_schema_uses_live_config():
    from gc_core.tools.registry import get_builtin_tools_data

    tools = get_builtin_tools_data(
        {
            "fs_tools": {
                "find_files_by_name": {
                    "default_max_matches": 7,
                    "hard_max_matches": 11,
                }
            }
        }
    )
    entry = tools["fs_find_files_by_name"]
    mm = entry["spec"]["parameters"]["properties"]["max_matches"]
    assert mm["maximum"] == 11
    assert "Default 7" in mm["description"]
    assert "max_matches (default 7, maximum 11)" in entry["description"]


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

    rel = json.loads(tool(path="relative/dir", query="x"))
    assert rel["ok"] is False and rel["error"]["field"] == "path"
    assert "absolute" in rel["error"].get("message", "").lower()

    missing = json.loads(tool(path=str(tmp_path / "nope"), query="x"))
    assert missing["ok"] is False and missing["error"]["type"] == "not_found"

    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    not_dir = json.loads(tool(path=str(f), query="x"))
    assert not_dir["ok"] is False and not_dir["error"]["type"] == "not_a_directory"


def test_registered_name():
    assert FindFilesByName.registered_tool_name() == "fs_find_files_by_name"


def test_glob_auto_detection_rule_is_documented():
    """Правило вывода режима не было объявлено нигде — модель не могла его знать.

    Само поведение (скобки = класс символов) осознанное, литерального режима у
    тула нет; значит модель должна хотя бы знать об этом из описания.
    """
    schema_desc = FindFilesByName.tool_description()["parameters"]["properties"]["query"][
        "description"
    ]
    prompt_desc = FindFilesByName.prompt_description()

    for text in (schema_desc, prompt_desc):
        assert "* ? [ ]" in text
        assert "config[1].yml" in text or "config1.yml" in text


# --- размер из обхода вместо getsize на совпадение ------------------------------

def test_size_from_walk_spares_getsize():
    import json

    from gc_testkit.harbor_fakes import FakeBackend, bind_os_backend
    from gc_core.tools.fs.find_files_by_name import FindFilesByName

    class _Counting(FakeBackend):
        def __init__(self, files):
            super().__init__(files)
            self.getsize_calls = 0

        def getsize(self, p):
            self.getsize_calls += 1
            return super().getsize(p)

    backend = _Counting({"/work/app.py": "print(1)\n"})
    tool = FindFilesByName.__new__(FindFilesByName)
    tool = bind_os_backend(tool, backend)

    data = json.loads(tool(path="/work", query="app"))

    assert data["matches"][0]["size_bytes"] == 9
    assert backend.getsize_calls == 0


def test_walk_prunes_hard_excludes_in_the_backend(tmp_path, monkeypatch):
    import json

    from gc_testkit.helpers.tool_ctx import with_local_ctx
    from gc_core.tools.fs.find_files_by_name import FindFilesByName

    (tmp_path / "app.py").write_text("x\n", encoding="utf-8")
    tool = with_local_ctx(FindFilesByName, config_dict={"launch_root": str(tmp_path)})
    backend_cls = type(tool._backend)
    seen = {}
    original = backend_cls.iter_walk_entries

    def spy(self, path, **kwargs):
        seen.update(kwargs)
        return original(self, path, **kwargs)

    monkeypatch.setattr(backend_cls, "iter_walk_entries", spy)

    data = json.loads(tool(path=str(tmp_path), query="app"))

    assert data["ok"] is True
    assert "node_modules" in seen["prune_names"]
    assert seen["prune_dot_dirs"] is True


def test_find_lists_excluded_dirs_by_name_but_not_their_contents(tmp_path: Path):
    """Жёсткие исключения — «не спускаться», а не «не показывать»: сам каталог
    находится по имени (как на develop), его содержимое — нет."""
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "src").mkdir()

    for query, expected in (("node_modules", "node_modules"), (".github", ".github")):
        res = json.loads(_tool()(path=str(tmp_path), query=query))
        assert res["ok"] is True, query
        assert [Path(m["path"]).name for m in res["matches"]] == [expected]
        assert res["matches"][0]["type"] == "directory"

    inside = json.loads(_tool()(path=str(tmp_path), query="workflows"))
    assert inside["count"] == 0
