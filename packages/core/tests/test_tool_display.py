import subprocess
import sys

import pytest
from gc_core.constants.tool_display import (
    DisplayCategory,
    ToolDisplay,
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


def _snapshot(title):
    return {
        "mcp_office_read_docx": {
            "server": "office",
            "tool": "read_docx",
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
