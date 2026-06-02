from typing import Any, Dict

from tools.fs.append_to_file import AppendToFile
from tools.fs.apply_file_edits import ApplyFileEdits
from tools.fs.list_files import ListFiles
from tools.fs.read_file import ReadFile
from tools.fs.save_base64_file import SaveBase64File
from tools.fs.search_and_replace import SearchAndReplace
from tools.fs.search_files import SearchFiles
from tools.fs.write_to_file_tool import WriteToFile
from tools.exec.calculator import Calculator
from tools.exec.execute_command import ExecuteCommand
from tools.exec.run_python_code import RunPythonCode
from tools.exec.sleep_tool import Sleep
from tools.session.intermediate_reply import IntermediateReply
from tools.session.ask_human import AskHuman
from tools.session.long_memory import LongMemory
from tools.session.read_user_intent_log import ReadUserIntentLog
from tools.session.submit_solution import SubmitSolution
from tools.session.think import Think
from tools.session.working_memory import WorkingMemory
from tools.session.stored_outputs.read_stored_tool_output import ReadStoredToolOutput

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
        "write_to_file": _tool_entry(WriteToFile),
        "append_to_file": _tool_entry(AppendToFile),
        "apply_file_edits": _tool_entry(ApplyFileEdits),
        "read_file": _tool_entry(ReadFile),
        "list_files": _tool_entry(ListFiles),
        "search_files": _tool_entry(SearchFiles),
        "search_and_replace": _tool_entry(SearchAndReplace),
        "save_base64_file": _tool_entry(SaveBase64File),
    }


def _exec_registry() -> Dict[str, Dict[str, Any]]:
    return {
        "execute_command": _tool_entry(ExecuteCommand),
        "calculator": _tool_entry(Calculator),
        "sleep": _tool_entry(Sleep),
        "run_python_code": _tool_entry(RunPythonCode),
        "fetch_url": _tool_entry(FetchUrl),
    }


def _session_registry(config_dict: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    registry: Dict[str, Dict[str, Any]] = {
        "long_memory": _tool_entry(LongMemory),
        "think": _tool_entry(Think),
        "read_stored_tool_output": _tool_entry(ReadStoredToolOutput),
        "submit_solution": _tool_entry(SubmitSolution),
        "ask_human": _tool_entry(AskHuman),
        "intermediate_reply": _tool_entry(IntermediateReply),
        "working_memory": _tool_entry(WorkingMemory),
    }

    backend_mode = str(config_dict.get("backend_api_mode", "responses")).strip().lower()
    if backend_mode == "chat_completions":
        registry["read_user_intent_log"] = _tool_entry(ReadUserIntentLog)

    return registry


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
    tools_data.update(_session_registry(config_dict))
    return _apply_stop_tools(tools_data, config_dict)


__all__ = ["get_builtin_tools_data"]
