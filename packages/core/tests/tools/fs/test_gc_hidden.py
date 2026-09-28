import json
from pathlib import Path

from gc_core.tools.fs.list_files import ListFiles
from gc_testkit.helpers.tool_ctx import with_local_ctx


def _mkproject(tmp_path):
    """Project with the agent's own .gc scaffolding plus real project files."""
    (tmp_path / "a.txt").write_text("needle here", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("x", encoding="utf-8")
    gc = tmp_path / ".gc"
    gc.mkdir()
    (gc / "config.yml").write_text("needle here", encoding="utf-8")
    skills = gc / "skills"
    skills.mkdir()
    (skills / "s.md").write_text("needle here", encoding="utf-8")
    return tmp_path


def test_list_depth0_hides_gc_but_keeps_other_dotfiles(tmp_path):
    _mkproject(tmp_path)
    res = json.loads(
        with_local_ctx(ListFiles)(path=str(tmp_path), max_depth=0)
    )
    assert res["ok"] is True
    names = {Path(e["path"]).name for e in res["entries"]}
    assert ".gc" not in names
    # Not a blanket dotfile filter: other hidden entries stay visible.
    assert ".gitignore" in names
    assert "a.txt" in names


def test_list_deep_hides_gc_and_its_contents(tmp_path):
    _mkproject(tmp_path)
    res = json.loads(
        with_local_ctx(ListFiles)(path=str(tmp_path), max_depth=5)
    )
    assert res["ok"] is True
    for e in res["entries"]:
        assert ".gc" not in Path(e["path"]).parts
    names = {Path(e["path"]).name for e in res["entries"]}
    assert ".gitignore" in names


def test_exact_match_only_gcignore_and_suffix_stay_visible(tmp_path):
    (tmp_path / ".gcignore").write_text("x", encoding="utf-8")
    (tmp_path / "foo.gc").mkdir()
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    res = json.loads(
        with_local_ctx(ListFiles)(path=str(tmp_path), max_depth=0)
    )
    names = {Path(e["path"]).name for e in res["entries"]}
    assert ".gcignore" in names
    assert "foo.gc" in names
