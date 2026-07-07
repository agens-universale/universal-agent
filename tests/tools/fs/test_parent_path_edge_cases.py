"""Parent path edge cases after FsBackend migration (is_dir vs exists)."""
import json

from tools.fs.append_to_file import AppendToFile
from tools.fs.write_to_file_tool import WriteToFile


def _parent_file_tree(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("I am a file", encoding="utf-8")
    return str(blocker / "child.txt")


def test_append_parent_is_file_without_make_dirs(tmp_path):
    path = _parent_file_tree(tmp_path)
    res = json.loads(
        AppendToFile()(
            path=path,
            content="x",
            create_if_missing=True,
            make_dirs=False,
        )
    )
    assert res["ok"] is False
    assert res["error"]["type"] == "parent_not_found"


def test_append_parent_is_file_with_make_dirs(tmp_path):
    path = _parent_file_tree(tmp_path)
    res = json.loads(
        AppendToFile()(
            path=path,
            content="x",
            create_if_missing=True,
            make_dirs=True,
        )
    )
    assert res["ok"] is False
    assert res["error"]["type"] == "mkdir_failed"


def test_write_parent_is_file_without_make_dirs(tmp_path):
    path = _parent_file_tree(tmp_path)
    res = json.loads(WriteToFile()(path=path, content="new", make_dirs=False))
    assert res["ok"] is False
    assert res["error"]["type"] == "parent_not_found"


def test_write_parent_is_file_with_make_dirs(tmp_path):
    path = _parent_file_tree(tmp_path)
    res = json.loads(WriteToFile()(path=path, content="new", make_dirs=True))
    assert res["ok"] is False
    assert res["error"]["type"] == "mkdir_failed"
