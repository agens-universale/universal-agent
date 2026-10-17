"""Защищённые классы с вопросом пола: скиллы (и в C2 — настройки
инструментов). Запись спрашивается там, где режим спрашивает человека;
plan запрещает без вопроса; чтение не спрашивается никогда."""

import io
import json
import sys
from pathlib import Path

import pytest
from gc_testkit.settings import fake_ctx

from gc_core.execution.state import SharedStateData
from gc_core.security.guard_probe import (
    GuardProbeRequest,
    evaluate_guard_probe,
)
from gc_core.security import protected_paths
from gc_core.security.path_policy import enforce_write_guard
from gc_core.security.permission_store import PermissionStore
from gc_core.security.protected_paths import protected_decision
from gc_core.security.read_guard import enforce_read_guard


@pytest.fixture(autouse=True)
def _no_terminal(monkeypatch):
    """Сценарии «без человека»: stdin — не терминал. Иначе при `pytest -s`
    автопроверка канала нашла бы консоль, и вопрос ушёл бы в неё."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))


@pytest.fixture
def home(tmp_path, monkeypatch):
    """`gc_home` во временном каталоге; проект — `<tmp>/root`."""
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    (tmp_path / "gc").mkdir()
    (tmp_path / "root").mkdir()
    return tmp_path


def _ctx(mode, root, *, answers=None, config=None):
    """Контекст с настоящим стором. `answers` — очередь ответов человека;
    None — человека нет."""
    shared = SharedStateData(permission_mode=mode)
    asked = []

    def tui_confirm(*, title, question, fields, **kwargs):
        asked.append(title)
        return answers.pop(0) if answers else "deny_once"

    ctx = fake_ctx(
        config={"launch_root": str(root), **(config or {})},
        permission_store=PermissionStore(shared.confirm_rules, shared=shared),
        logger=None,
        is_tui=answers is not None,
        tui_confirm=tui_confirm if answers is not None else None,
    )
    ctx.asked = asked
    return ctx


@enforce_write_guard(path_arg="path")
def _write(path: str, ctx=None) -> str:
    return "written"


@enforce_read_guard(path_arg="path")
def _read(path: str, ctx=None) -> str:
    return "content"


def _skills(home):
    """Проектный и пользовательский (глобальный) скилл."""
    return (
        home / "root" / ".gc" / "skills" / "foo" / "SKILL.md",
        home / "gc" / "skills" / "foo" / "SKILL.md",
    )


# --- скиллы (§4.1) --------------------------------------------------------------


def test_skill_write_is_a_protected_question(home):
    for path in _skills(home):
        assert protected_decision(str(path), cwd=str(home / "root")) == "ask"


def test_edit_asks_before_writing_a_skill(home):
    for path in _skills(home):
        ctx = _ctx("edit", home / "root", answers=["deny_once"])
        out = json.loads(_write(path=str(path), ctx=ctx))
        assert out["error"]["type"] == "protected_path_denied", path
        assert ctx.asked == ["Защита записи"], path


def test_manual_asks_once_and_writes_a_project_skill_after_yes(home):
    path = _skills(home)[0]
    ctx = _ctx("manual", home / "root", answers=["allow_once"])
    assert _write(path=str(path), ctx=ctx) == "written"
    assert ctx.asked == ["Защита записи"]


def test_manual_asks_about_a_global_skill_first(home):
    path = _skills(home)[1]
    ctx = _ctx("manual", home / "root", answers=["allow_once", "allow_once"])
    assert _write(path=str(path), ctx=ctx) == "written"
    assert ctx.asked[0] == "Защита записи"


def test_session_answer_covers_repeated_writes_of_the_same_skill_file(home):
    path = str(_skills(home)[0])
    ctx = _ctx("edit", home / "root", answers=["allow_remember"])
    assert _write(path=path, ctx=ctx) == "written"
    assert _write(path=path, ctx=ctx) == "written"
    assert ctx.asked == ["Защита записи"]


def test_plan_denies_a_skill_write_without_asking(home):
    for path in _skills(home):
        ctx = _ctx("plan", home / "root", answers=["allow_remember"])
        out = json.loads(_write(path=str(path), ctx=ctx))
        assert out["error"]["type"] == "write_denied_by_mode", path
        assert ctx.asked == [], path


def test_reading_a_skill_is_never_asked(home):
    path = _skills(home)[0]
    path.parent.mkdir(parents=True)
    path.write_text("x", encoding="utf-8")
    for mode in ("edit", "manual", "plan"):
        ctx = _ctx(mode, home / "root", answers=[])
        assert _read(path=str(path), ctx=ctx) == "content", mode
        assert ctx.asked == [], mode


def test_worker_facts_ask_about_a_skill_write(home):
    """Под Temporal решение пола приходит фактом воркера — тем же правилом."""
    root = str(home / "root")
    request = GuardProbeRequest(
        "protected_write", "fs_write_file", str(_skills(home)[0]), root
    )
    settings = fake_ctx(config={"launch_root": root})
    facts = evaluate_guard_probe(request, settings=settings)
    assert facts.protected_decision == "ask"


# --- настройки инструментов (§4.2) ----------------------------------------------


@pytest.mark.parametrize("name", sorted(protected_paths.TOOL_CONFIG_FILES))
def test_every_tool_settings_file_is_protected_anywhere(home, name):
    root = home / "root"
    for path in (root / name, root / "sub" / name, home / "elsewhere" / name):
        assert protected_decision(str(path), cwd=str(root)) == "ask", path


@pytest.mark.parametrize("parts", protected_paths.TOOL_CONFIG_DIRS)
def test_every_tool_settings_directory_is_protected_anywhere(home, parts):
    root = home / "root"
    for base in (root, root / "sub", home / "elsewhere"):
        path = base.joinpath(*parts, "file")
        assert protected_decision(str(path), cwd=str(root)) == "ask", path


def test_the_list_is_exactly_the_reference_list():
    """Состав — список эталона плюс `.codex` (решение владельца): своих
    имён сверх этого не добавляем."""
    assert len(protected_paths.TOOL_CONFIG_DIRS) == 10
    assert len(protected_paths.TOOL_CONFIG_FILES) == 37
    assert (".codex",) in protected_paths.TOOL_CONFIG_DIRS
    assert (".config", "git") in protected_paths.TOOL_CONFIG_DIRS


def test_inside_the_project_only_parts_below_the_root_count(home):
    """Корень проекта сам лежит в каталоге из списка (рабочая копия в
    `.claude/worktrees`): файлы проекта — обычные, его настройки — нет."""
    root = home / ".claude" / "worktrees" / "w"
    root.mkdir(parents=True)
    cwd = str(root)
    assert protected_decision(str(root / "src" / "a.py"), cwd=cwd) == "allow"
    assert protected_decision(str(root / ".vscode" / "x"), cwd=cwd) == "ask"
    assert protected_decision(str(root / ".envrc"), cwd=cwd) == "ask"


def test_outside_the_project_every_part_counts(home):
    cwd = str(home / "root")
    bashrc = Path.home() / ".bashrc"
    cargo = Path.home() / ".cargo" / "config.toml"
    assert protected_decision(str(bashrc), cwd=cwd) == "ask"
    assert protected_decision(str(cargo), cwd=cwd) == "ask"


def test_similar_names_are_not_tool_settings(home):
    root = home / "root"
    for path in (
        root / ".envrc.example",
        root / "my.vscode" / "x",
        root / ".config" / "other" / "x",
        root / "git" / ".config",
    ):
        assert protected_decision(str(path), cwd=str(root)) == "allow", path


def test_tool_settings_case_follows_the_file_system(home, monkeypatch):
    root = str(home / "root")
    monkeypatch.setattr(protected_paths, "_CASE_INSENSITIVE_FS", True)
    assert (
        protected_decision(f"{root}/.VSCode/settings.json", cwd=root) == "ask"
    )
    assert protected_decision(f"{root}/.EnvRC", cwd=root) == "ask"
    monkeypatch.setattr(protected_paths, "_CASE_INSENSITIVE_FS", False)
    assert protected_decision(f"{root}/.EnvRC", cwd=root) == "allow"


def _tool_settings(home):
    """Каталог и файл — в проекте и вне его."""
    root = home / "root"
    return (
        root / ".vscode" / "settings.json",
        root / ".envrc",
        home / "user" / ".bashrc",
        home / "user" / ".config" / "git" / "config",
    )


def test_edit_asks_before_writing_tool_settings(home):
    for path in _tool_settings(home):
        ctx = _ctx("edit", home / "root", answers=["deny_once"])
        out = json.loads(_write(path=str(path), ctx=ctx))
        assert out["error"]["type"] == "protected_path_denied", path
        assert ctx.asked == ["Защита записи"], path


def test_explicit_allow_does_not_lift_the_floor(home):
    path = home / "root" / ".envrc"
    ctx = _ctx(
        "edit",
        home / "root",
        answers=["deny_once"],
        config={"security.write_guard.mode": "allow"},
    )
    out = json.loads(_write(path=str(path), ctx=ctx))
    assert out["error"]["type"] == "protected_path_denied"


def test_manual_asks_once_and_writes_tool_settings_after_yes(home):
    for path in _tool_settings(home)[:2]:
        ctx = _ctx("manual", home / "root", answers=["allow_once"])
        assert _write(path=str(path), ctx=ctx) == "written", path
        assert ctx.asked == ["Защита записи"], path


def test_plan_denies_tool_settings_writes_without_asking(home):
    for path in _tool_settings(home):
        ctx = _ctx("plan", home / "root", answers=["allow_remember"])
        out = json.loads(_write(path=str(path), ctx=ctx))
        assert out["error"]["type"] == "write_denied_by_mode", path
        assert ctx.asked == [], path


def test_reading_tool_settings_by_command_or_tool_is_not_asked(home):
    """Иначе вопрос на каждое `cat .envrc`: в проверке команд класс — allow."""
    from gc_core.security.command_guard import (
        enforce_command_guard,
        scan_command_for_protected,
    )

    root = home / "root"
    (root / ".envrc").write_text("export A=1", encoding="utf-8")
    assert scan_command_for_protected("cat .envrc", cwd=str(root)) == "allow"

    @enforce_command_guard(payload_arg="command", tool_name="exec_command")
    def run(command: str, ctx=None) -> str:
        return "executed"

    ctx = _ctx("edit", root, answers=[])
    assert run(command="cat .envrc", ctx=ctx) == "executed"
    assert _read(path=str(root / ".envrc"), ctx=ctx) == "content"
    assert ctx.asked == []


def test_worker_facts_carry_the_tool_settings_class(home):
    root = str(home / "root")
    settings = fake_ctx(config={"launch_root": root})
    write = evaluate_guard_probe(
        GuardProbeRequest(
            "protected_write", "fs_write_file", f"{root}/.envrc", root
        ),
        settings=settings,
    )
    command = evaluate_guard_probe(
        GuardProbeRequest("command", "exec_command", "cat .envrc", root),
        settings=settings,
    )
    assert write.protected_decision == "ask"
    assert command.protected_decision == "allow"
