import json
import os
from pathlib import Path

import pytest
from gc_core.tools.fs.list_files import ListFiles
from gc_testkit.helpers.tool_ctx import with_local_ctx
from gc_testkit.helpers.crossplatform import require_symlinks


def test_list_default_and_hidden(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".env").write_text("x", encoding="utf-8")
    (tmp_path / "dir").mkdir()
    res = json.loads(with_local_ctx(ListFiles)(path=str(tmp_path)))
    assert res["ok"] is True
    assert res["count"] == 3
    by_name = {Path(item["path"]).name: item for item in res["entries"]}
    assert by_name[".env"]["is_hidden"] is True
    assert by_name["dir"]["type"] == "directory"


def test_list_depth_glob_and_truncate(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("print(1)", encoding="utf-8")
    (tmp_path / "src" / "b.txt").write_text("x", encoding="utf-8")
    res = json.loads(
        with_local_ctx(ListFiles)(
            path=str(tmp_path), max_depth=2, glob="**/*.py"
        )
    )
    assert res["ok"] is True and res["count"] == 1
    assert (
        Path(res["entries"][0]["path"])
        == (tmp_path / "src" / "a.py").resolve()
    )
    assert "guidance" not in res
    trunc = json.loads(
        with_local_ctx(ListFiles)(
            path=str(tmp_path), max_depth=2, max_entries=1
        )
    )
    assert trunc["truncated"] is True and trunc["count"] == 1
    assert "max_entries" in trunc["guidance"]


def test_list_validation_and_not_found(tmp_path):
    tool = with_local_ctx(ListFiles)
    bad = json.loads(tool(path=123))  # type: ignore[arg-type]
    assert bad["ok"] is False and bad["error"]["field"] == "path"
    rel = json.loads(tool(path="relative/dir"))
    assert rel["ok"] is False and rel["error"]["field"] == "path"
    assert "absolute" in rel["error"].get("message", "").lower()
    missing = json.loads(tool(path=str(tmp_path / "missing")))
    assert missing["ok"] is False and missing["error"]["type"] == "not_found"


def test_list_extra_validation_and_not_a_directory(tmp_path):
    tool = with_local_ctx(ListFiles)
    # Non-string optional glob is coalesced to absent (same as omit / "").
    coalesced_glob = json.loads(tool(path=str(tmp_path), glob=1))  # type: ignore[arg-type]
    assert coalesced_glob["ok"] is True and "glob" not in coalesced_glob
    bad_depth = json.loads(tool(path=str(tmp_path), max_depth=-1))
    assert (
        bad_depth["ok"] is False and bad_depth["error"]["field"] == "max_depth"
    )
    bad_entries = json.loads(tool(path=str(tmp_path), max_entries=0))
    assert (
        bad_entries["ok"] is False
        and bad_entries["error"]["field"] == "max_entries"
    )
    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    not_dir = json.loads(tool(path=str(f)))
    assert (
        not_dir["ok"] is False
        and not_dir["error"]["type"] == "not_a_directory"
    )


def test_list_symlink_to_dir_depth0_and_depth1(tmp_path):
    require_symlinks(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    link = tmp_path / "linkdir"
    link.symlink_to(sub)
    tool = with_local_ctx(ListFiles)

    depth0 = json.loads(tool(path=str(tmp_path), max_depth=0))
    assert depth0["ok"] is True
    by_name = {Path(e["path"]).name: e for e in depth0["entries"]}
    assert by_name["linkdir"]["type"] == "file"

    depth1 = json.loads(tool(path=str(tmp_path), max_depth=1))
    assert depth1["ok"] is True
    by_name1 = {Path(e["path"]).name: e for e in depth1["entries"]}
    assert by_name1["linkdir"]["type"] == "file"


@pytest.mark.skipif(os.name == "nt", reason="POSIX chmod 000")
def test_list_depth0_includes_unreadable_subdir_name(tmp_path):
    open_dir = tmp_path / "open"
    open_dir.mkdir()
    (open_dir / "a.txt").write_text("x", encoding="utf-8")
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "hidden.txt").write_text("y", encoding="utf-8")
    old_mode = secret.stat().st_mode
    try:
        secret.chmod(0o000)
        res = json.loads(
            with_local_ctx(ListFiles)(path=str(tmp_path), max_depth=0)
        )
        assert res["ok"] is True
        names = {Path(e["path"]).name for e in res["entries"]}
        assert "secret" in names
        assert "open" in names
    finally:
        secret.chmod(old_mode)
