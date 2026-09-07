import base64
import json

from gc_core.tools.fs.append_to_file import AppendToFile
from gc_core.tools.fs.file_info import FileInfo
from gc_core.tools.fs.save_base64_file import SaveBase64File
from gc_core.tools.fs.search_and_replace import SearchAndReplace
from gc_core.tools.fs.write_to_file_tool import WriteToFile


from gc_testkit.helpers.tool_ctx import with_local_ctx

def _config(launch_root, extra_paths=None, enabled=True):
    return {
        "launch_root": str(launch_root),
        "write_guard": {
            "enabled": enabled,
            "extra_paths": [str(path) for path in (extra_paths or [])],
        },
    }


def _read_config(launch_root, enabled=True):
    return {
        "launch_root": str(launch_root),
        "read_guard": {
            "enabled": enabled,
            "mode": "deny",
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


def test_save_base64_file_blocks_outside_launch_root(tmp_path):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    outside = tmp_path / "payload.bin"
    data = base64.b64encode(b"blocked").decode("ascii")

    result = json.loads(
        with_local_ctx(SaveBase64File, config_dict=_config(launch_root))(
            path=str(outside),
            data=data,
        )
    )

    assert result["ok"] is False
    assert result["tool"] == "fs_save_base64_file"
    assert result["error"]["type"] == "write_guard_violation"
    assert not outside.exists()


def test_save_base64_file_allows_inside_launch_root(tmp_path):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    inside = launch_root / "ok.bin"
    raw = b"\x00ok"
    data = base64.b64encode(raw).decode("ascii")

    result = json.loads(
        with_local_ctx(SaveBase64File, config_dict=_config(launch_root))(
            path=str(inside),
            data=data,
        )
    )

    assert result["ok"] is True
    assert inside.read_bytes() == raw


def test_file_info_blocks_outside_launch_root(tmp_path):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    result = json.loads(
        with_local_ctx(FileInfo, config_dict=_read_config(launch_root))(
            path=str(outside),
            include_hash=True,
        )
    )

    assert result["ok"] is False
    assert result["tool"] == "fs_file_info"
    assert result["error"]["type"] == "read_guard_violation"
    assert result.get("sha256") is None


def test_file_info_allows_inside_launch_root(tmp_path):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    inside = launch_root / "a.txt"
    inside.write_text("hello\n", encoding="utf-8")

    result = json.loads(
        with_local_ctx(FileInfo, config_dict=_read_config(launch_root))(
            path=str(inside),
        )
    )

    assert result["ok"] is True
    assert result["exists"] is True
    assert result["type"] == "file"
