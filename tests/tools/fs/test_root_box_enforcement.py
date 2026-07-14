import json

from tools.fs.append_to_file import AppendToFile
from tools.fs.search_and_replace import SearchAndReplace
from tools.fs.write_to_file_tool import WriteToFile


from tests.helpers.tool_ctx import with_local_ctx

def _config(launch_root, extra_paths=None, enabled=True):
    return {
        "launch_root": str(launch_root),
        "write_guard": {
            "enabled": enabled,
            "extra_paths": [str(path) for path in (extra_paths or [])],
        },
    }


def test_write_to_file_blocks_outside_launch_root(tmp_path):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    outside = tmp_path / "outside.txt"

    result = json.loads(
        with_local_ctx(WriteToFile, config_dict=_config(launch_root))(
            path=str(outside),
            content="blocked",
        )
    )

    assert result["ok"] is False
    assert result["tool"] == "fs_write_file"
    assert result["error"]["type"] == "write_guard_violation"
    assert not outside.exists()


def test_write_to_file_allows_launch_root_and_extra_paths(tmp_path):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    extra_root = tmp_path / "cache"
    extra_root.mkdir()

    inside = launch_root / "inside.txt"
    extra = extra_root / "artifact.txt"
    tool = with_local_ctx(WriteToFile, config_dict=_config(launch_root, [extra_root]))

    inside_result = json.loads(tool(path=str(inside), content="inside"))
    extra_result = json.loads(tool(path=str(extra), content="extra"))

    assert inside_result["ok"] is True
    assert extra_result["ok"] is True
    assert inside.read_text(encoding="utf-8") == "inside"
    assert extra.read_text(encoding="utf-8") == "extra"


def test_append_to_file_blocks_outside_launch_root(tmp_path):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    outside = tmp_path / "note.txt"
    outside.write_text("start", encoding="utf-8")

    result = json.loads(
        with_local_ctx(AppendToFile, config_dict=_config(launch_root))(
            path=str(outside),
            content="blocked",
        )
    )

    assert result["ok"] is False
    assert result["tool"] == "fs_append_file"
    assert result["error"]["type"] == "write_guard_violation"
    assert outside.read_text(encoding="utf-8") == "start"


def test_search_and_replace_blocks_outside_launch_root(tmp_path):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    outside = tmp_path / "code.py"
    outside.write_text("print('old')", encoding="utf-8")

    result = json.loads(
        with_local_ctx(SearchAndReplace, config_dict=_config(launch_root))(
            path=str(outside),
            search="old",
            replace="new",
        )
    )

    assert result["ok"] is False
    assert result["tool"] == "fs_search_and_replace"
    assert result["error"]["type"] == "write_guard_violation"
    assert outside.read_text(encoding="utf-8") == "print('old')"


def test_root_box_can_be_disabled_for_file_tools(tmp_path):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    outside = tmp_path / "outside.txt"

    result = json.loads(
        with_local_ctx(WriteToFile, config_dict=_config(launch_root, enabled=False))(
            path=str(outside),
            content="allowed",
        )
    )

    assert result["ok"] is True
    assert outside.read_text(encoding="utf-8") == "allowed"
