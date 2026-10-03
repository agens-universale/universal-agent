import copy
from typing import Any, Dict

from gc_core.loop_detector.constants import LOOP_WARN_KEY, copy_loop_warn
from gc_core.settings import settings_of
from gc_core.tools.exec.calculator import Calculator
from gc_core.tools.exec.download_file import DownloadFile
from gc_core.tools.exec.execute_command import ExecuteCommand
from gc_core.tools.exec.run_python_code import RunPythonCode
from gc_core.tools.exec.sleep_tool import Sleep
from gc_core.tools.fs.append_to_file import AppendToFile
from gc_core.tools.fs.apply_file_edits import ApplyFileEdits
from gc_core.tools.fs.file_info import FileInfo
from gc_core.tools.fs.find_files_by_name import FindFilesByName
from gc_core.tools.fs.find_in_files import FindInFiles
from gc_core.tools.fs.list_files import ListFiles
from gc_core.tools.fs.read_file import ReadFile
from gc_core.tools.fs.save_base64_file import SaveBase64File
from gc_core.tools.fs.search_and_replace import SearchAndReplace
from gc_core.tools.fs.write_to_file_tool import WriteToFile
from gc_core.tools.project.project_memory import ProjectMemory
from gc_core.tools.session.read_user_intent_log import ReadUserIntentLog
from gc_core.tools.session.session_compact import SessionCompact
from gc_core.tools.session.stored_outputs.read_stored_tool_output import (
    ReadStoredToolOutput,
)
from gc_core.tools.session.think import Think
from gc_core.tools.session.todo import SessionTodo
from gc_core.tools.session.working_memory import WorkingMemory
from gc_core.tools.turn.ask_human import AskHuman
from gc_core.tools.turn.intermediate_reply import IntermediateReply
from gc_core.tools.turn.submit_solution import SubmitSolution

try:
    from gc_core.tools.exec.fetch_url import FetchUrl
except ModuleNotFoundError:
    from gc_core.tools.exec.simple_fetch_url import FetchUrl


def _tool_entry(tool_cls: type, settings: Any = None) -> Dict[str, Any]:
    # Deep-copy: inject_tool_fields / strict schema mutate the spec in place.
    # Several tools return a shared class-level parameters dict from
    # tool_description(); without a copy the second build_tools_data() in the
    # same process sees the field already present, skips inject, leaves
    # injected_fields empty, and strip_injected_tool_args never removes it
    # (TypeError: unexpected keyword argument 'reason').
    spec = tool_cls.tool_description(settings)
    description = tool_cls.prompt_description(settings)
    entry: Dict[str, Any] = {
        "tool_fabric": tool_cls,
        "spec": copy.deepcopy(spec),
        "description": description,
    }
    warn = copy_loop_warn(tool_cls)
    if warn is not None:
        entry[LOOP_WARN_KEY] = warn
    return entry


def _fs_registry(
    settings: Any = None,
) -> Dict[str, Dict[str, Any]]:
    return {
        "fs_write_file": _tool_entry(WriteToFile, settings),
        "fs_append_file": _tool_entry(AppendToFile, settings),
        "fs_apply_file_edits": _tool_entry(ApplyFileEdits, settings),
        "fs_read_file": _tool_entry(ReadFile, settings),
        "fs_file_info": _tool_entry(FileInfo, settings),
        "fs_list_files": _tool_entry(ListFiles, settings),
        "fs_find_files_by_name": _tool_entry(FindFilesByName, settings),
        "fs_find_in_files": _tool_entry(FindInFiles, settings),
        "fs_search_and_replace": _tool_entry(SearchAndReplace, settings),
        "fs_save_base64_file": _tool_entry(SaveBase64File, settings),
    }


def _exec_registry(
    settings: Any = None,
) -> Dict[str, Dict[str, Any]]:
    return {
        "exec_command": _tool_entry(ExecuteCommand, settings),
        "exec_calculator": _tool_entry(Calculator, settings),
        "exec_sleep": _tool_entry(Sleep, settings),
        "exec_python": _tool_entry(RunPythonCode, settings),
        "exec_fetch_url": _tool_entry(FetchUrl, settings),
        "download_file": _tool_entry(DownloadFile, settings),
    }


def _turn_registry(
    settings: Any = None,
) -> Dict[str, Dict[str, Any]]:
    return {
        "turn_ask_human": _tool_entry(AskHuman, settings),
        "turn_intermediate_reply": _tool_entry(IntermediateReply, settings),
        "turn_finish": _tool_entry(SubmitSolution, settings),
    }


def _session_registry(
    settings: Any,
) -> Dict[str, Dict[str, Any]]:
    registry: Dict[str, Dict[str, Any]] = {
        "session_think": _tool_entry(Think, settings),
        "session_read_stored_tool_output": _tool_entry(
            ReadStoredToolOutput, settings
        ),
        "session_memory": _tool_entry(WorkingMemory, settings),
        "session_todo": _tool_entry(SessionTodo, settings),
    }

    backend_mode = settings_of(settings).backend.api_mode.strip().lower()
    if backend_mode == "chat_completions":
        registry["session_read_user_intent_log"] = _tool_entry(
            ReadUserIntentLog, settings
        )
        registry["session_compact"] = _tool_entry(SessionCompact, settings)

    return registry


def _project_registry(
    settings: Any = None,
) -> Dict[str, Dict[str, Any]]:
    return {
        "project_memory": _tool_entry(ProjectMemory, settings),
    }


def _apply_stop_tools(
    tools_data: Dict[str, Dict[str, Any]],
    settings: Any,
) -> Dict[str, Dict[str, Any]]:
    stop_tools_cfg = settings_of(settings).agent.stop_tools
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


def _collect_builtin_registry(
    settings: Any,
) -> Dict[str, Dict[str, Any]]:
    """Все встроенные tools_data ДО применения stop_tools."""
    tools_data: Dict[str, Dict[str, Any]] = {}
    tools_data.update(_fs_registry(settings))
    tools_data.update(_exec_registry(settings))
    tools_data.update(_turn_registry(settings))
    tools_data.update(_session_registry(settings))
    tools_data.update(_project_registry(settings))
    return tools_data


def all_builtin_tool_names(settings: Any) -> set[str]:
    """Имена всех встроенных тулов (до stop_tools).

    Для валидации stop_tools этого мало: скиллы и MCP живут в слитом реестре.
    Полный каталог — ``registered_tool_names`` после ``build_tools_data``.
    """
    return set(_collect_builtin_registry(settings).keys())


def get_builtin_tools_data(
    settings: Any,
) -> Dict[str, Dict[str, Any]]:
    """
    Собрать словарь builtin tools в формате tools_data.

    Инструменты из stop_tools исключаются из результата.

    Скиллы, MCP и сабагенты этот срез не видит — их режет ``build_tools_data``
    повторным проходом по слитому набору. Прямые вызывающие по-прежнему
    получают builtin уже без ``stop_tools``.
    """
    return _apply_stop_tools(_collect_builtin_registry(settings), settings)


__all__ = [
    "_exec_registry",
    "_fs_registry",
    "_project_registry",
    "_session_registry",
    "_turn_registry",
    "all_builtin_tool_names",
    "get_builtin_tools_data",
]
