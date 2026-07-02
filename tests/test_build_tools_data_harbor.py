from tools import build_tools_data
from tools.exec.execute_command import ExecuteCommand
from tools.exec.run_python_code import RunPythonCode
from tools.fs.apply_file_edits import ApplyFileEdits
from tools.fs.read_file import ReadFile
from tools.fs.write_to_file_tool import WriteToFile


def test_harbor_pack_off_by_default():
    cfg = {"backend_api_mode": "chat_completions"}
    tools_data, _, _, _ = build_tools_data(cfg)
    assert tools_data["fs_read_file"]["tool_fabric"] is ReadFile
    assert "download_file" in tools_data


def test_harbor_flag_keeps_base_tools_and_drops_download_file():
    # fs/exec tools now follow ctx.harbor_backend at call time (AbstractSimpleTool._backend)
    # instead of being swapped for shim classes; only download_file (host-only) is dropped.
    cfg = {"backend_api_mode": "chat_completions", "use_harbor_tools": True}
    tools_data, _, _, _ = build_tools_data(cfg)
    assert tools_data["fs_read_file"]["tool_fabric"] is ReadFile
    assert tools_data["fs_write_file"]["tool_fabric"] is WriteToFile
    assert tools_data["fs_apply_file_edits"]["tool_fabric"] is ApplyFileEdits
    assert tools_data["exec_command"]["tool_fabric"] is ExecuteCommand
    assert tools_data["exec_python"]["tool_fabric"] is RunPythonCode
    assert "download_file" not in tools_data
