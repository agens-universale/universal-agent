from gc_core.tools import abuild_tools_data
from gc_core.tools.exec.execute_command import ExecuteCommand
from gc_core.tools.exec.run_python_code import RunPythonCode
from gc_core.tools.fs.apply_file_edits import ApplyFileEdits
from gc_core.tools.fs.read_file import ReadFile
from gc_core.tools.fs.write_to_file_tool import WriteToFile
from gc_testkit.settings import make_settings


async def test_harbor_pack_off_by_default():
    cfg = {"backend.api_mode": "chat_completions"}
    tools_data, _, _, _ = await abuild_tools_data(make_settings(cfg))
    assert tools_data["fs_read_file"]["tool_fabric"] is ReadFile
    assert "download_file" in tools_data


async def test_harbor_flag_keeps_base_tools_and_drops_download_file():
    cfg = {"backend.api_mode": "chat_completions", "os_backend": "harbor"}
    tools_data, _, _, _ = await abuild_tools_data(make_settings(cfg))
    assert tools_data["fs_read_file"]["tool_fabric"] is ReadFile
    assert tools_data["fs_write_file"]["tool_fabric"] is WriteToFile
    assert tools_data["fs_apply_file_edits"]["tool_fabric"] is ApplyFileEdits
    assert tools_data["exec_command"]["tool_fabric"] is ExecuteCommand
    assert tools_data["exec_python"]["tool_fabric"] is RunPythonCode
    assert "download_file" not in tools_data


async def test_remote_kinds_drop_download_file_too():
    # Кейс «прикреплённый контейнер»: файлы и код — только там; download_file
    # пишет на диск хоста и для docker/ssh/exec так же лишний, как для harbor.
    for section in (
        {"kind": "docker", "docker": {"name": "box"}},
        {"kind": "ssh", "ssh": {"host": "h"}},
        {"kind": "exec", "exec": {"argv_prefix": ["env"]}},
    ):
        cfg = {
            "backend.api_mode": "chat_completions",
            **{f"os_backend.{k}": v for k, v in section.items()},
        }
        tools_data, _, _, _ = await abuild_tools_data(make_settings(cfg))
        assert "download_file" not in tools_data, section["kind"]
        assert "fs_read_file" in tools_data


async def test_sandbox_kind_keeps_download_file():
    # sandbox — файлы на хосте, download_file работает под write_guard как у local.
    cfg = {"backend.api_mode": "chat_completions", "os_backend": "sandbox"}
    tools_data, _, _, _ = await abuild_tools_data(make_settings(cfg))
    assert "download_file" in tools_data
