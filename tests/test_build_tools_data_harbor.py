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
    cfg = {"backend_api_mode": "chat_completions", "os_backend": "harbor"}
    tools_data, _, _, _ = build_tools_data(cfg)
    assert tools_data["fs_read_file"]["tool_fabric"] is ReadFile
    assert tools_data["fs_write_file"]["tool_fabric"] is WriteToFile
    assert tools_data["fs_apply_file_edits"]["tool_fabric"] is ApplyFileEdits
    assert tools_data["exec_command"]["tool_fabric"] is ExecuteCommand
    assert tools_data["exec_python"]["tool_fabric"] is RunPythonCode
    assert "download_file" not in tools_data
