import subprocess
import sys

import pytest
from gc_core.constants.tool_display import (
    MCP_LABEL_MAX,
    TOOL_DISPLAY,
    DisplayCategory,
    ToolDisplay,
    subagent_role,
    tool_display,
)


def test_read_file():
    assert tool_display("fs_read_file") == ToolDisplay(
        DisplayCategory.READ, "Read", "path"
    )


@pytest.mark.parametrize(
    "name",
    [
        "turn_finish",
        "turn_ask_human",
        "turn_intermediate_reply",
        "turn_propose_plan",
        "session_todo",
        "session_compact",
    ],
)
def test_hidden(name):
    assert tool_display(name).category == DisplayCategory.HIDDEN


@pytest.mark.parametrize("name", ["subagent_worker", "subagent_my_reviewer"])
def test_subagent_builtin_and_custom(name):
    shown = tool_display(name)
    assert shown.category == DisplayCategory.AGENT
    assert shown.label == "Agent"
    assert shown.primary_arg == "task"


def test_special_agent_shows_role():
    shown = tool_display("subagent_special_agent")
    assert shown.category == DisplayCategory.AGENT
    assert shown.label == "Agent"
    assert shown.primary_arg == "role"


def test_legacy_names():
    assert tool_display("read_file") == tool_display("fs_read_file")
    assert tool_display("fs_search_files").category == DisplayCategory.SEARCH
    assert tool_display("submit_solution").category == DisplayCategory.HIDDEN


def test_unknown_name_called():
    assert tool_display("gone_tool") == ToolDisplay(
        DisplayCategory.CALLED, "Called", None, "gone_tool"
    )


def _snapshot(title, *, server="office", tool="read_docx"):
    return {
        "mcp_office_read_docx": {
            "server": server,
            "tool": tool,
            "title": title,
        }
    }


def test_mcp_from_snapshot():
    shown = tool_display(
        "mcp_office_read_docx", mcp_tools=_snapshot("Read Word")
    )
    assert shown.category == DisplayCategory.MCP
    assert shown.label == "office"
    assert shown.detail == "Read Word"


def test_mcp_without_title():
    shown = tool_display("mcp_office_read_docx", mcp_tools=_snapshot(None))
    assert shown.detail == "read_docx"


def test_mcp_longest_server_key():
    shown = tool_display(
        "mcp_browser_playwright_click",
        mcp_servers=["browser", "browser_playwright"],
    )
    assert shown.label == "browser_playwright"
    assert shown.detail == "click"


def test_mcp_unknown_server():
    shown = tool_display("mcp_x_y")
    assert shown.label == "mcp"
    assert shown.detail == "x_y"


def test_module_does_not_import_tools():
    code = (
        "import sys, gc_core.constants.tool_display;"
        " assert 'gc_core.tools' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


@pytest.mark.parametrize(
    ("name", "role"),
    [
        ("subagent_worker", "Worker"),
        ("subagent_planner", "Planner"),
        ("subagent_code_planner", "Code planner"),
        ("subagent_code_generator", "Coder"),
        ("subagent_code_reviewer", "Reviewer"),
        ("subagent_critic", "Critic"),
        ("subagent_security_audit", "Security audit"),
    ],
)
def test_subagent_role(name, role):
    assert subagent_role(name, {"role": "Аудитор"}) == role


@pytest.mark.parametrize(
    ("args", "role"),
    [
        ({"role": " Аудитор ретраев "}, "Аудитор ретраев"),
        ({"role": "  "}, "Special agent"),
        ({"role": 7}, "Special agent"),
        ({}, "Special agent"),
    ],
)
def test_special_agent_role_from_argument(args, role):
    """Роль `subagent_special_agent` — его аргумент `role`, без него —
    суффикс имени, как у кастомного."""
    assert subagent_role("subagent_special_agent", args) == role


def test_finish_visible_only_nested():
    """Сдача субагента — строка его блока; наверху `turn_finish` скрыт."""
    assert tool_display("turn_finish").category == DisplayCategory.HIDDEN
    nested = tool_display("turn_finish", nested=True)
    assert nested == ToolDisplay(
        DisplayCategory.FINISH, "Finish", "solution_type"
    )


@pytest.mark.parametrize(
    "name", ["turn_ask_human", "turn_intermediate_reply", "session_compact"]
)
def test_other_hidden_stay_hidden_nested(name):
    shown = tool_display(name, nested=True)
    assert shown.category == DisplayCategory.HIDDEN


def test_nested_regular_tool_unchanged():
    assert tool_display("fs_read_file", nested=True) == tool_display(
        "fs_read_file"
    )


def test_finish_summary_solution_type_only_nested():
    """Главный аргумент сдачи — только во вложенной таблице: наверху
    главного нет, а пары — только у тулов вне таблицы, сводка пуста."""
    from gc_core.execution.progress import tool_args_summary

    args = {"solution_type": "full_solution", "task": "сверить таблицу"}
    assert TOOL_DISPLAY["turn_finish"].primary_arg is None
    nested = tool_args_summary("turn_finish", args, nested=True)
    assert nested == "full_solution"
    assert tool_args_summary("turn_finish", args) == ""


def _mcp_shown(title, *, server="office", tool="read_docx"):
    return tool_display(
        "mcp_office_read_docx",
        mcp_tools=_snapshot(title, server=server, tool=tool),
    )


def test_mcp_title_newlines_become_one_space():
    assert _mcp_shown("Read\n  Word\r\n\tdoc ").detail == "Read Word doc"


def test_mcp_title_escape_sequences_removed():
    assert _mcp_shown("\x1b[31mRed\x1b[0m title").detail == "Red title"
    assert _mcp_shown("\x1b]0;pwned\x07Read").detail == "Read"


def test_mcp_title_bel_and_c1_removed():
    assert _mcp_shown("Read\x07Word\x9b").detail == "Read Word"


def test_mcp_long_title_clipped_with_ellipsis():
    detail = _mcp_shown("x" * 200).detail
    assert detail == "x" * (MCP_LABEL_MAX - 1) + "…"


def test_mcp_title_empty_after_cleaning_falls_back_to_tool():
    assert _mcp_shown(" \n\x1b[0m\x07 ").detail == "read_docx"


def test_mcp_tool_name_cleaned_too():
    assert _mcp_shown(None, tool="read\x1b[2Jdocx\n").detail == "readdocx"


def test_mcp_server_part_cleaned():
    shown = _mcp_shown("Read", server="off\nice\x1b[1m" + "s" * 100)
    assert shown.label == ("off ice" + "s" * 100)[: MCP_LABEL_MAX - 1] + "…"


def test_mcp_wire_name_parts_cleaned():
    shown = tool_display("mcp_office_read\ndocx", mcp_servers=["office"])
    assert shown.detail == "read docx"


def test_mcp_normal_title_unchanged():
    assert _mcp_shown("Прочитать документ Word").detail == (
        "Прочитать документ Word"
    )
