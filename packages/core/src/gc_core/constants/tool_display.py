"""Таблица отображения встроенных тулов: категория, подпись, главный аргумент.

Лёгкий модуль без импортов пакета `tools` и settings: им пользуются
фронтенды и сводка аргументов. Политика скрытия остаётся у фронтенда.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TypedDict

from gc_core.constants.tool_names import (
    ASK_HUMAN_TOOL_NAME,
    DOWNLOAD_FILE_NAME,
    EXEC_CALCULATOR_NAME,
    EXEC_COMMAND_NAME,
    EXEC_FETCH_URL_NAME,
    EXEC_PYTHON_NAME,
    EXEC_SLEEP_NAME,
    FINISH_TOOL_NAME,
    FS_APPEND_FILE_NAME,
    FS_APPLY_FILE_EDITS_NAME,
    FS_FILE_INFO_NAME,
    FS_FIND_FILES_BY_NAME_NAME,
    FS_FIND_IN_FILES_NAME,
    FS_LIST_FILES_NAME,
    FS_READ_FILE_NAME,
    FS_SAVE_BASE64_FILE_NAME,
    FS_SEARCH_AND_REPLACE_NAME,
    FS_VIEW_IMAGE_NAME,
    FS_WRITE_FILE_NAME,
    INTERMEDIATE_REPLY_TOOL_NAME,
    PROJECT_MEMORY_TOOL_NAME,
    PROPOSE_PLAN_TOOL_NAME,
    READ_STORED_TOOL_OUTPUT_NAME,
    READ_USER_INTENT_LOG_NAME,
    SESSION_COMPACT_TOOL_NAME,
    SESSION_MEMORY_TOOL_NAME,
    SESSION_THINK_TOOL_NAME,
    SESSION_TODO_TOOL_NAME,
    SKILLS_LIST_NAME,
    SKILLS_LOAD_NAME,
    SKILLS_READ_ASSET_NAME,
    SKILLS_READ_REFERENCE_NAME,
    SKILLS_RUN_SCRIPT_NAME,
)

_SUBAGENT_PREFIX = "subagent_"
_MCP_PREFIX = "mcp_"


class DisplayCategory(StrEnum):
    READ = "Read"
    WRITE = "Write"
    EDIT = "Edit"
    SEARCH = "Search"
    FIND = "Find"
    RUN = "Run"
    FETCH = "Fetch"
    VIEW = "View"
    MEMORY = "Memory"
    THINK = "Think"
    SKILL = "Skill"
    AGENT = "Agent"
    CALC = "Calc"
    WAIT = "Wait"
    MCP = "MCP"
    HIDDEN = "Hidden"
    CALLED = "Called"


@dataclass(frozen=True)
class ToolDisplay:
    category: DisplayCategory
    label: str
    primary_arg: str | None
    detail: str | None = None


class McpToolInfo(TypedDict):
    server: str
    tool: str
    title: str | None


def _d(category: DisplayCategory, arg: str | None) -> ToolDisplay:
    return ToolDisplay(category, category.value, arg)


_C = DisplayCategory

TOOL_DISPLAY: Mapping[str, ToolDisplay] = {
    FS_READ_FILE_NAME: _d(_C.READ, "path"),
    SKILLS_READ_REFERENCE_NAME: _d(_C.READ, "skill_id"),
    SKILLS_READ_ASSET_NAME: _d(_C.READ, "skill_id"),
    READ_STORED_TOOL_OUTPUT_NAME: _d(_C.READ, "output_ref"),
    READ_USER_INTENT_LOG_NAME: _d(_C.READ, "action"),
    FS_WRITE_FILE_NAME: _d(_C.WRITE, "path"),
    FS_APPEND_FILE_NAME: _d(_C.WRITE, "path"),
    FS_SAVE_BASE64_FILE_NAME: _d(_C.WRITE, "path"),
    FS_APPLY_FILE_EDITS_NAME: _d(_C.EDIT, "path"),
    FS_SEARCH_AND_REPLACE_NAME: _d(_C.EDIT, "path"),
    FS_FIND_IN_FILES_NAME: _d(_C.SEARCH, "pattern"),
    FS_FIND_FILES_BY_NAME_NAME: _d(_C.FIND, "query"),
    FS_LIST_FILES_NAME: _d(_C.FIND, "path"),
    FS_FILE_INFO_NAME: _d(_C.FIND, "path"),
    EXEC_COMMAND_NAME: _d(_C.RUN, "command"),
    EXEC_PYTHON_NAME: _d(_C.RUN, "code"),
    SKILLS_RUN_SCRIPT_NAME: _d(_C.RUN, "script_name"),
    EXEC_FETCH_URL_NAME: _d(_C.FETCH, "url"),
    DOWNLOAD_FILE_NAME: _d(_C.FETCH, "url"),
    FS_VIEW_IMAGE_NAME: _d(_C.VIEW, "path"),
    SESSION_MEMORY_TOOL_NAME: _d(_C.MEMORY, "action"),
    PROJECT_MEMORY_TOOL_NAME: _d(_C.MEMORY, "action"),
    SESSION_THINK_TOOL_NAME: _d(_C.THINK, None),
    SKILLS_LIST_NAME: _d(_C.SKILL, "pack"),
    SKILLS_LOAD_NAME: _d(_C.SKILL, "skill_id"),
    EXEC_CALCULATOR_NAME: _d(_C.CALC, "expression"),
    EXEC_SLEEP_NAME: _d(_C.WAIT, "seconds"),
    FINISH_TOOL_NAME: _d(_C.HIDDEN, None),
    ASK_HUMAN_TOOL_NAME: _d(_C.HIDDEN, None),
    INTERMEDIATE_REPLY_TOOL_NAME: _d(_C.HIDDEN, None),
    PROPOSE_PLAN_TOOL_NAME: _d(_C.HIDDEN, None),
    SESSION_TODO_TOOL_NAME: _d(_C.HIDDEN, None),
    SESSION_COMPACT_TOOL_NAME: _d(_C.HIDDEN, None),
}

# Старое имя тула (до переименования, для чтения старых тредов) -> текущее.
LEGACY_TOOL_NAMES: Mapping[str, str] = {
    "ask_human": ASK_HUMAN_TOOL_NAME,
    "submit_solution": FINISH_TOOL_NAME,
    "intermediate_reply": INTERMEDIATE_REPLY_TOOL_NAME,
    "read_file": FS_READ_FILE_NAME,
    "write_to_file": FS_WRITE_FILE_NAME,
    "append_to_file": FS_APPEND_FILE_NAME,
    "execute_command": EXEC_COMMAND_NAME,
    "run_python_code": EXEC_PYTHON_NAME,
    "fetch_url": EXEC_FETCH_URL_NAME,
    "list_files": FS_LIST_FILES_NAME,
    "find_files_by_name": FS_FIND_FILES_BY_NAME_NAME,
    "search_files": FS_FIND_IN_FILES_NAME,
    "fs_search_files": FS_FIND_IN_FILES_NAME,
    "search_and_replace": FS_SEARCH_AND_REPLACE_NAME,
}


def _mcp_display(
    wire_name: str,
    mcp_tools: Mapping[str, McpToolInfo] | None,
    mcp_servers: Iterable[str],
) -> ToolDisplay:
    info = (mcp_tools or {}).get(wire_name)
    if info is not None:
        detail = info.get("title") or info["tool"]
        return ToolDisplay(_C.MCP, info["server"], None, detail)
    rest = wire_name[len(_MCP_PREFIX) :]
    for server in sorted(mcp_servers, key=len, reverse=True):
        if rest.startswith(server + "_"):
            return ToolDisplay(_C.MCP, server, None, rest[len(server) + 1 :])
    return ToolDisplay(_C.MCP, "mcp", None, rest)


def tool_display(
    wire_name: str,
    *,
    mcp_tools: Mapping[str, McpToolInfo] | None = None,
    mcp_servers: Iterable[str] = (),
) -> ToolDisplay:
    name = LEGACY_TOOL_NAMES.get(wire_name, wire_name)
    shown = TOOL_DISPLAY.get(name)
    if shown is not None:
        return shown
    if name.startswith(_SUBAGENT_PREFIX):
        return ToolDisplay(_C.AGENT, _C.AGENT.value, "task")
    if name.startswith(_MCP_PREFIX):
        return _mcp_display(name, mcp_tools, mcp_servers)
    return ToolDisplay(_C.CALLED, _C.CALLED.value, None, wire_name)
