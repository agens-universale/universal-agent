from typing import Any, Dict

from tools.fs.append_to_file import AppendToFile
from tools.fs.apply_file_edits import ApplyFileEdits
from tools.fs.file_info import FileInfo
from tools.fs.list_files import ListFiles
from tools.fs.read_file import ReadFile
from tools.fs.save_base64_file import SaveBase64File
from tools.fs.search_and_replace import SearchAndReplace
from tools.fs.search_files import SearchFiles
from tools.fs.write_to_file_tool import WriteToFile
from tools.exec.download_file import DownloadFile
from tools.exec.calculator import Calculator
from tools.exec.execute_command import ExecuteCommand
from tools.exec.run_python_code import RunPythonCode
from tools.exec.sleep_tool import Sleep
from tools.project.project_memory import ProjectMemory
from tools.session.read_user_intent_log import ReadUserIntentLog
from tools.session.session_compact import SessionCompact
from tools.session.think import Think
from tools.session.working_memory import WorkingMemory
from tools.session.stored_outputs.read_stored_tool_output import ReadStoredToolOutput
from tools.turn.submit_solution import SubmitSolution
from tools.turn.intermediate_reply import IntermediateReply
from tools.turn.ask_human import AskHuman

try:
    from tools.exec.fetch_url import FetchUrl
except ModuleNotFoundError:
    from tools.exec.simple_fetch_url import FetchUrl


def _tool_entry(tool_cls: type) -> Dict[str, Any]:
    return {
        "tool_fabric": tool_cls,
        "spec": tool_cls.tool_description(),
        "description": tool_cls.prompt_description(),
    }


def _fs_registry() -> Dict[str, Dict[str, Any]]:
    return {
        "fs_write_file": _tool_entry(WriteToFile),
        "fs_append_file": _tool_entry(AppendToFile),
        "fs_apply_file_edits": _tool_entry(ApplyFileEdits),
        "fs_read_file": _tool_entry(ReadFile),
        "fs_file_info": _tool_entry(FileInfo),
        "fs_list_files": _tool_entry(ListFiles),
        "fs_search_files": _tool_entry(SearchFiles),
        "fs_search_and_replace": _tool_entry(SearchAndReplace),
        "fs_save_base64_file": _tool_entry(SaveBase64File),
    }


def _exec_registry() -> Dict[str, Dict[str, Any]]:
    return {
        "exec_command": _tool_entry(ExecuteCommand),
        "exec_calculator": _tool_entry(Calculator),
        "exec_sleep": _tool_entry(Sleep),
        "exec_python": _tool_entry(RunPythonCode),
        "exec_fetch_url": _tool_entry(FetchUrl),
        "download_file": _tool_entry(DownloadFile),
    }


def _turn_registry() -> Dict[str, Dict[str, Any]]:
    return {
        "turn_ask_human": _tool_entry(AskHuman),
        "turn_intermediate_reply": _tool_entry(IntermediateReply),
        "turn_finish": _tool_entry(SubmitSolution),
    }


def _session_registry(config_dict: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    registry: Dict[str, Dict[str, Any]] = {
        "session_think": _tool_entry(Think),
        "session_read_stored_tool_output": _tool_entry(ReadStoredToolOutput),
        "session_memory": _tool_entry(WorkingMemory),
    }

    backend_mode = str(config_dict.get("backend_api_mode", "responses")).strip().lower()
    if backend_mode == "chat_completions":
        registry["session_read_user_intent_log"] = _tool_entry(ReadUserIntentLog)
        registry["session_compact"] = _tool_entry(SessionCompact)

    return registry


def _project_registry() -> Dict[str, Dict[str, Any]]:
    return {
        "project_memory": _tool_entry(ProjectMemory),
    }


def _apply_stop_tools(
    tools_data: Dict[str, Dict[str, Any]],
    config_dict: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    stop_tools_cfg = config_dict.get("stop_tools", [])
    if not isinstance(stop_tools_cfg, list):
        return tools_data

    stop_tools = {
        tool_name.strip()
        for tool_name in stop_tools_cfg
        if isinstance(tool_name, str) and tool_name.strip()
    }
    if not stop_tools:
        return tools_data

    return {
        tool_name: tool_data
        for tool_name, tool_data in tools_data.items()
        if tool_name not in stop_tools
    }


def get_builtin_tools_data(config_dict: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    Собрать словарь builtin tools в формате tools_data.

    Инструменты из stop_tools исключаются из результата.
    """
    tools_data: Dict[str, Dict[str, Any]] = {}
    tools_data.update(_fs_registry())
    tools_data.update(_exec_registry())
    tools_data.update(_turn_registry())
    tools_data.update(_session_registry(config_dict))
    tools_data.update(_project_registry())
    return _apply_stop_tools(tools_data, config_dict)


__all__ = [
    "get_builtin_tools_data",
    "_fs_registry",
    "_exec_registry",
    "_turn_registry",
    "_session_registry",
    "_project_registry",
]
