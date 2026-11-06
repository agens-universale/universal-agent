"""Таблица отображения встроенных тулов: категория, подпись, главный аргумент.

Лёгкий модуль без импортов пакета `tools` и settings: им пользуются
фронтенды и сводка аргументов. Политика скрытия остаётся у фронтенда.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Mapping
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
    SUBAGENT_SPECIAL_AGENT_NAME,
)

_SUBAGENT_PREFIX = "subagent_"
_MCP_PREFIX = "mcp_"

# Предел подписи MCP (сервер, название тула): в строке трейса на 80
# колонках после глифа, категории, сервера и названия остаётся место цели
# вызова. Обычные названия тулов — короче 30 символов.
MCP_LABEL_MAX = 40
# ESC-последовательности: CSI (цвета, курсор), OSC (заголовок окна) до BEL
# или ST, прочие двухсимвольные.
_ESC_SEQUENCE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-_])"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


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
    FINISH = "Finish"
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
    SESSION_THINK_TOOL_NAME: _d(_C.THINK, "thoughts"),
    SKILLS_LIST_NAME: _d(_C.SKILL, "pack"),
    SKILLS_LOAD_NAME: _d(_C.SKILL, "skill_id"),
    EXEC_CALCULATOR_NAME: _d(_C.CALC, "expression"),
    EXEC_SLEEP_NAME: _d(_C.WAIT, "seconds"),
    SUBAGENT_SPECIAL_AGENT_NAME: _d(_C.AGENT, "role"),
    FINISH_TOOL_NAME: _d(_C.HIDDEN, None),
    ASK_HUMAN_TOOL_NAME: _d(_C.HIDDEN, None),
    INTERMEDIATE_REPLY_TOOL_NAME: _d(_C.HIDDEN, None),
    PROPOSE_PLAN_TOOL_NAME: _d(_C.HIDDEN, None),
    SESSION_TODO_TOOL_NAME: _d(_C.HIDDEN, None),
    SESSION_COMPACT_TOOL_NAME: _d(_C.HIDDEN, None),
}

#: Вложенный контекст (вызовы субагента): тулы, видимые только там. Сдача
#: ребёнка — последнее событие блока `Agent`; наверху её показывает конец
#: хода (решение владельца 2026-10-23).
NESTED_TOOL_DISPLAY: Mapping[str, ToolDisplay] = {
    FINISH_TOOL_NAME: _d(_C.FINISH, "solution_type"),
}

#: Короткая роль встроенного субагента — объект заголовка `Agent` во
#: фронтендах; особый — аргумент `role`, кастомный и неизвестный — суффикс
#: имени (`subagent_role`).
SUBAGENT_ROLES: Mapping[str, str] = {
    "subagent_worker": "Worker",
    "subagent_planner": "Planner",
    "subagent_code_planner": "Code planner",
    "subagent_code_generator": "Coder",
    "subagent_code_reviewer": "Reviewer",
    "subagent_critic": "Critic",
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


def _mcp_label(text: str) -> str:
    """Подпись из данных MCP-сервера — одной строкой и без управляющих
    символов: перевод строки ломает строку трейса, ESC доезжает до
    терминала, длинное название вытесняет цель вызова."""
    text = _CONTROL.sub(" ", _ESC_SEQUENCE.sub("", text))
    flat = " ".join(text.split())
    if len(flat) <= MCP_LABEL_MAX:
        return flat
    return flat[: MCP_LABEL_MAX - 1].rstrip() + "…"


def _mcp_parts(
    wire_name: str,
    mcp_tools: Mapping[str, McpToolInfo] | None,
    mcp_servers: Iterable[str],
) -> tuple[str, tuple[str, ...]]:
    """Сервер и кандидаты в название тула по убыванию предпочтения."""
    info = (mcp_tools or {}).get(wire_name)
    if info is not None:
        return info["server"], (info.get("title") or "", info["tool"])
    rest = wire_name[len(_MCP_PREFIX) :]
    for server in sorted(mcp_servers, key=len, reverse=True):
        if rest.startswith(server + "_"):
            return server, (rest[len(server) + 1 :],)
    return "mcp", (rest,)


def _mcp_display(
    wire_name: str,
    mcp_tools: Mapping[str, McpToolInfo] | None,
    mcp_servers: Iterable[str],
) -> ToolDisplay:
    server, names = _mcp_parts(wire_name, mcp_tools, mcp_servers)
    detail = next((label for label in map(_mcp_label, names) if label), "")
    return ToolDisplay(_C.MCP, _mcp_label(server), None, detail)


#: Поля `session_think` в порядке показа; все необязательные.
THINK_FIELDS = ("thoughts", "critics", "doubts", "reflection")


def think_parts(
    args: Mapping[str, object], injected: Collection[str] = ()
) -> list[str]:
    """Непустые части вызова Think: сначала `THINK_FIELDS` по порядку,
    затем любые другие ключи как пришли; значение не-строка — JSON-текстом.
    Всё, что модель передала, — мысль (живой случай 2026-11-01 — `thought`
    мимо схемы). Служебные поля схемы (``injected``) дают текст, только
    когда своего нет. Один разбор на тул (перед схемой), сводку строки и
    раскрытие ленты."""
    own = {k: v for k, v in args.items() if k not in injected}
    service = {k: v for k, v in args.items() if k in injected}
    return _think_texts(own) or _think_texts(service)


def _think_texts(args: Mapping[str, object]) -> list[str]:
    keys = [*THINK_FIELDS, *(key for key in args if key not in THINK_FIELDS)]
    texts = (_think_value_text(args.get(key)) for key in keys)
    return [text for text in texts if text]


def _think_value_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if value in (None, [], {}):
        return ""
    return json.dumps(value, ensure_ascii=False)


def tool_display(
    wire_name: str,
    *,
    nested: bool = False,
    mcp_tools: Mapping[str, McpToolInfo] | None = None,
    mcp_servers: Iterable[str] = (),
) -> ToolDisplay:
    """Отображение тула; ``nested`` — вызов субагента: сначала
    `NESTED_TOOL_DISPLAY`."""
    name = LEGACY_TOOL_NAMES.get(wire_name, wire_name)
    shown = (NESTED_TOOL_DISPLAY.get(name) if nested else None) or (
        TOOL_DISPLAY.get(name)
    )
    if shown is not None:
        return shown
    if name.startswith(_SUBAGENT_PREFIX):
        return ToolDisplay(_C.AGENT, _C.AGENT.value, "task")
    if name.startswith(_MCP_PREFIX):
        return _mcp_display(name, mcp_tools, mcp_servers)
    return ToolDisplay(_C.CALLED, _C.CALLED.value, None, wire_name)


def subagent_role(wire_name: str, args: Mapping[str, object]) -> str:
    """Короткая роль субагента по имени тула и разобранным аргументам
    вызова: у `subagent_special_agent` — его аргумент `role`; иначе из
    `SUBAGENT_ROLES`, иначе суффикс после `subagent_` словами
    (`security_audit` → `Security audit`)."""
    role = args.get("role")
    role = role.strip() if isinstance(role, str) else ""
    if wire_name == SUBAGENT_SPECIAL_AGENT_NAME and role:
        return role
    known = SUBAGENT_ROLES.get(wire_name)
    if known is not None:
        return known
    suffix = wire_name.removeprefix(_SUBAGENT_PREFIX).replace("_", " ")
    return suffix.strip().capitalize() or wire_name


def _snapshot_tools(server: str, entry: object) -> dict[str, McpToolInfo]:
    tools = entry.get("tools") if isinstance(entry, Mapping) else None
    found: dict[str, McpToolInfo] = {}
    for item in tools if isinstance(tools, list) else ():
        if not isinstance(item, Mapping):
            continue
        wire, tool = item.get("name"), item.get("tool")
        if not (wire and tool and isinstance(wire, str)):
            continue
        title = item.get("title")
        found[wire] = {
            "server": server,
            "tool": str(tool),
            "title": title if isinstance(title, str) and title else None,
        }
    return found


def mcp_tools_from_snapshot(
    snapshot: Mapping[str, object] | None,
) -> dict[str, McpToolInfo]:
    """Снимок `build_mcp_tools_snapshot` -> `wire-имя -> McpToolInfo`."""
    result: dict[str, McpToolInfo] = {}
    for server, entry in (snapshot or {}).items():
        result.update(_snapshot_tools(server, entry))
    return result


def mcp_servers_of(snapshot: Mapping[str, object] | None) -> tuple[str, ...]:
    """Имена серверов снимка (фолбэк `tool_display(mcp_servers=...)`)."""
    return tuple(snapshot or ())
