"""Одна сводка аргументов вызова тула: главный аргумент по таблице отображения.

Формат — спека P1a §2.5: путь сохраняет имя файла, `code` — число строк,
поиск — `"pattern" in path`; главного нет — у тула ядра пусто, у MCP и
неизвестного — до трёх пар `key=preview`.
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


def test_list_files_trailing_separator_adds_only_star():
    assert tool_args_summary("fs_list_files", {"path": "packages/"}) == (
        "packages/*"
    )


def test_list_files_root_is_slash_star():
    assert tool_args_summary("fs_list_files", {"path": "/"}) == "/*"


def test_skill_read_without_slug_shows_only_skill_id():
    out = tool_args_summary(
        "skills_read_reference", {"skill_id": "brainstorming"}
    )
    assert out == "brainstorming"


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


def test_known_tool_without_primary_has_empty_summary():
    """Пары `key=value` — только для тулов вне таблицы (MCP, неизвестные):
    у тула ядра без главного аргумента сводка пуста."""
    assert tool_args_summary("fs_read_file", {"path": None}) == ""
    assert tool_args_summary("turn_ask_human", {"question": "Да?"}) == ""


def test_think_shows_start_of_thought():
    assert tool_args_summary("session_think", {"thoughts": "x"}) == "x"


def test_think_without_thoughts_shows_the_first_filled_field():
    """Поля Think необязательные: модель может заполнить одно `reflection`
    — строка не должна оставаться голой «Think»."""
    assert tool_args_summary("session_think", {"doubts": "x"}) == "x"
    assert (
        tool_args_summary("session_think", {"critics": " ", "reflection": "y"})
        == "y"
    )
    assert tool_args_summary("session_think", {}) == ""


def test_think_summary_takes_the_misnamed_thought_field():
    """`thought` вместо `thoughts` (живой случай 2026-11-01) — то же поле."""
    args = {"thought": "главное", "reflection": "вывод"}
    assert tool_args_summary("session_think", args) == "главное"


def test_mcp_without_scalar_falls_back_to_pairs():
    summary = tool_args_summary("mcp_office_read", {"pages": [1, 2]})
    assert summary == "pages=<list 2>"


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


# ---- пути от корня проекта (решение владельца, спека P3 §10) ----

ROOT = "/home/dmitry/work/project"


@pytest.fixture
def home(monkeypatch):
    monkeypatch.setenv("HOME", "/home/dmitry")


@pytest.mark.usefixtures("home")
def test_path_inside_the_project_is_relative_to_its_root():
    out = tool_args_summary(
        "fs_read_file", {"path": f"{ROOT}/README.md"}, root=ROOT
    )
    assert out == "README.md"


@pytest.mark.usefixtures("home")
def test_list_files_inside_the_project_is_relative():
    out = tool_args_summary(
        "fs_list_files", {"path": f"{ROOT}/packages"}, root=ROOT
    )
    assert out == "packages/*"


@pytest.mark.usefixtures("home")
def test_find_in_files_path_is_relative():
    out = tool_args_summary(
        "fs_find_in_files",
        {"pattern": "TIMEOUT", "path": f"{ROOT}/packages/"},
        root=ROOT,
    )
    assert out == '"TIMEOUT" in packages'


@pytest.mark.usefixtures("home")
def test_path_outside_the_project_under_home_uses_tilde():
    out = tool_args_summary(
        "fs_read_file", {"path": "/home/dmitry/notes/a.md"}, root=ROOT
    )
    assert out == "~/notes/a.md"


@pytest.mark.usefixtures("home")
def test_path_outside_home_is_absolute():
    out = tool_args_summary(
        "fs_read_file", {"path": "/tmp/build.log"}, root=ROOT
    )
    assert out == "/tmp/build.log"


@pytest.mark.usefixtures("home")
def test_long_rooted_path_is_cut_in_the_middle():
    out = tool_args_summary(
        "fs_read_file",
        {"path": f"{ROOT}/packages/tui/src/gc_tui/palette.py"},
        root=ROOT,
        limit=24,
    )
    assert out == "packages/…/palette.py"


@pytest.mark.usefixtures("home")
def test_run_command_is_not_rewritten():
    out = tool_args_summary(
        "exec_command", {"command": f"cat {ROOT}/README.md"}, root=ROOT
    )
    assert out == f"cat {ROOT}/README.md"


@pytest.mark.usefixtures("home")
def test_mcp_path_argument_is_not_rewritten():
    """Путь MCP-тула — в координатах сервера: корень проекта не при чём."""
    out = tool_args_summary(
        "mcp_office_read_docx", {"path": f"{ROOT}/a.docx"}, root=ROOT
    )
    assert out == f"{ROOT}/a.docx"


def test_without_root_the_path_is_shown_as_is():
    assert tool_args_summary("fs_read_file", {"path": f"{ROOT}/a.py"}) == (
        f"{ROOT}/a.py"
    )


# ---- команда без приставки `cd <корень проекта>` (решение владельца) ----


def _run(command: str) -> str:
    return tool_args_summary("exec_command", {"command": command}, root=ROOT)


@pytest.mark.usefixtures("home")
@pytest.mark.parametrize(
    "prefix",
    [
        f"cd {ROOT} && ",
        f"cd {ROOT}/ && ",
        f"cd {ROOT}; ",
        f"cd {ROOT};",
        f'cd "{ROOT}" && ',
        f"cd '{ROOT}' && ",
        f"  cd  {ROOT}  &&  ",
        f"cd {ROOT}\n",
        f"cd {ROOT}\r\n",
        f"cd {ROOT}  \n  ",
    ],
)
def test_cd_into_the_project_root_is_dropped(prefix):
    assert _run(f"{prefix}ls -la") == "ls -la"


@pytest.mark.usefixtures("home")
def test_cd_into_the_root_in_tilde_form_is_dropped():
    assert _run("cd ~/work/project && make test") == "make test"


@pytest.mark.usefixtures("home")
@pytest.mark.parametrize(
    "command",
    [
        f"cd {ROOT}/packages && ls",
        "cd /tmp && ls",
        f"cd {ROOT}",
        f"cd {ROOT} && ",
        f"echo cd {ROOT} && ls",
        f"cd {ROOT} || ls",
    ],
)
def test_other_commands_are_shown_as_written(command):
    assert _run(command) == " ".join(command.split())


@pytest.mark.usefixtures("home")
def test_cd_prefix_is_kept_without_root():
    out = tool_args_summary("exec_command", {"command": f"cd {ROOT} && ls"})
    assert out == f"cd {ROOT} && ls"


@pytest.mark.usefixtures("home")
def test_real_arguments_are_not_changed():
    args = {"command": f"cd {ROOT} && ls"}
    tool_args_summary("exec_command", args, root=ROOT)
    assert args == {"command": f"cd {ROOT} && ls"}


# ---- снятие `cd <корень>` для сырой команды (TUI рисует её построчно) ----

_HEREDOC = "python - <<'EOF'\nprint(1)\nEOF"


def test_strip_root_cd_keeps_line_breaks():
    from gc_core.execution.progress import strip_root_cd

    assert strip_root_cd(f"cd /proj && {_HEREDOC}", "/proj") == _HEREDOC


def test_strip_root_cd_keeps_other_directory():
    from gc_core.execution.progress import strip_root_cd

    command = f"cd /other && {_HEREDOC}"
    assert strip_root_cd(command, "/proj") == command


def test_strip_root_cd_keeps_bare_cd():
    from gc_core.execution.progress import strip_root_cd

    assert strip_root_cd("cd /proj && ", "/proj") == "cd /proj && "


def test_strip_root_cd_without_prefix_is_unchanged():
    from gc_core.execution.progress import strip_root_cd

    assert strip_root_cd(_HEREDOC, "/proj") == _HEREDOC


@pytest.mark.parametrize(
    ("command", "summary"),
    [
        (f"cd /proj && {_HEREDOC}", "python - <<'EOF' print(1) EOF"),
        ("cd /other && ls -la", "cd /other && ls -la"),
        ("cd /proj\nmake test\n  && echo ok", "make test && echo ok"),
        ('  cd "/proj" ; git status  ', "git status"),
        ("cd /proj && ", "cd /proj &&"),
    ],
)
def test_summary_of_multiline_command_is_unchanged(command, summary):
    """Сводка — по-прежнему одна строка: значения зафиксированы по базе."""
    out = tool_args_summary("exec_command", {"command": command}, root="/proj")
    assert out == summary


@pytest.mark.parametrize("action", ["write", "append", "read"])
@pytest.mark.parametrize(
    "register", ["goal", "constraints", "state", "knowledge"]
)
def test_session_memory_shows_action_and_register(action, register):
    out = tool_args_summary(
        "session_memory",
        {"action": action, "register": register, "content": "секрет"},
    )
    assert out == f"{action} {register}"


def test_session_memory_list_has_no_target():
    out = tool_args_summary(
        "session_memory", {"action": "list", "register": "goal"}
    )
    assert out == "list"


@pytest.mark.parametrize("action", ["write", "append", "read"])
def test_project_memory_shows_action_and_project(action):
    out = tool_args_summary(
        "project_memory", {"action": action, "content": "секрет"}
    )
    assert out == f"{action} project"


@pytest.mark.parametrize("name", ["session_memory", "project_memory"])
def test_memory_without_action_has_no_summary(name):
    """Сводка памяти — только от действия: без `action` она пуста, пар
    `key=value` нет (из них фронтенд не выводит глагол)."""
    assert tool_args_summary(name, {"register": "goal"}) == ""
