"""Одна сводка аргументов вызова тула: главный аргумент по таблице отображения.

Формат — спека P1a §2.5: путь сохраняет имя файла, `code` — число строк,
поиск — `"pattern" in path`; главного нет — до трёх пар `key=preview`.
"""

from __future__ import annotations

import pytest

from gc_core.execution.progress import tool_args_summary


def test_read_file_shows_path_only():
    out = tool_args_summary("fs_read_file", {"path": "a.py", "offset": 3})
    assert out == "a.py"


def test_long_path_keeps_file_name():
    out = tool_args_summary(
        "fs_read_file", {"path": "/a/very/long/dir/name/file.py"}, limit=10
    )
    assert out == "/…/file.py"


def test_find_in_files_shows_pattern_in_path():
    out = tool_args_summary(
        "fs_find_in_files", {"pattern": "TIMEOUT", "path": "packages/"}
    )
    assert out == '"TIMEOUT" in packages/'


def test_find_in_files_without_path_shows_pattern():
    assert tool_args_summary("fs_find_in_files", {"pattern": "TIMEOUT"}) == (
        '"TIMEOUT"'
    )


def test_list_files_shows_glob():
    assert tool_args_summary("fs_list_files", {"path": "packages/tui"}) == (
        "packages/tui/*"
    )


def test_file_info_marks_info():
    assert tool_args_summary("fs_file_info", {"path": "x.py"}) == "x.py · info"


def test_python_shows_line_count():
    assert tool_args_summary("exec_python", {"code": "a=1\nb=2\n"}) == (
        "python · 2 lines"
    )


def test_python_single_line():
    assert tool_args_summary("exec_python", {"code": "a=1"}) == (
        "python · 1 line"
    )


def test_command_collapses_whitespace():
    out = tool_args_summary("exec_command", {"command": "uv  run\npytest"})
    assert out == "uv run pytest"


def test_skill_reference_shows_skill_and_slug():
    out = tool_args_summary(
        "skills_read_reference", {"skill_id": "brainstorming", "slug": "guide"}
    )
    assert out == "brainstorming · guide"


def test_special_agent_shows_role():
    out = tool_args_summary(
        "subagent_special_agent", {"role": "критик", "task": "t"}
    )
    assert out == "критик"


def test_subagent_shows_task():
    out = tool_args_summary(
        "subagent_worker", {"task": "проверка", "reason": "r"}
    )
    assert out == "проверка"


def test_mcp_shows_first_scalar():
    out = tool_args_summary(
        "mcp_office_read_docx", {"file": "отчёт.docx", "pages": 3}
    )
    assert out == "отчёт.docx"


def test_legacy_name_uses_current_rules():
    assert tool_args_summary("read_file", {"path": "a.py"}) == "a.py"


def test_primary_not_string_falls_back_to_pairs():
    assert tool_args_summary("fs_read_file", {"path": None}) == "path=None"


def test_unknown_tool_falls_back_to_pairs():
    assert tool_args_summary("legacy_tool", {"a": "x", "b": 2}) == "a='x', b=2"


def test_no_tool_name_falls_back_to_pairs():
    assert tool_args_summary(None, {"path": "a.py"}) == "path='a.py'"


def test_json_string_arguments():
    assert tool_args_summary("fs_read_file", '{"path": "a.py"}') == "a.py"


def test_broken_json_shows_collapsed_text():
    assert tool_args_summary("fs_read_file", '{"path": ') == '{"path":'


def test_empty_arguments_give_empty_summary():
    assert tool_args_summary("fs_read_file", {}) == ""


@pytest.mark.parametrize(
    ("wire_name", "arguments"),
    [
        ("fs_read_file", {"path": "a.py", "offset": 3}),
        ("fs_read_file", {"path": "/a/very/long/dir/name/file.py"}),
        ("fs_find_in_files", {"pattern": "TIMEOUT", "path": "packages/"}),
        ("fs_list_files", {"path": "packages/tui"}),
        ("fs_file_info", {"path": "x.py"}),
        ("exec_python", {"code": "a=1\nb=2\n"}),
        ("exec_command", {"command": "uv  run\npytest"}),
        (
            "skills_read_reference",
            {"skill_id": "brainstorming", "slug": "guide"},
        ),
        ("subagent_special_agent", {"role": "критик", "task": "t"}),
        ("subagent_worker", {"task": "проверка", "reason": "r"}),
        ("mcp_office_read_docx", {"file": "отчёт.docx", "pages": 3}),
        ("fs_read_file", {"path": None}),
        ("legacy_tool", {"a": "x", "b": 2}),
        ("fs_read_file", '{"path": "a.py"}'),
        ("fs_read_file", '{"path": '),
        ("session_todo", {"todos": [{"content": "шаг", "status": "pending"}]}),
    ],
)
def test_summary_fits_limit(wire_name, arguments):
    assert len(tool_args_summary(wire_name, arguments, limit=10)) <= 10
