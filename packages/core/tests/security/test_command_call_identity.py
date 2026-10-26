"""Вопрос о команде и запомненный ответ: что входит в действие, кроме текста.

Каталог вне проекта и переменные окружения меняют то, что исполнится, — ответ
«в сессии» на них не распространяется. Каталог внутри проекта и фоновый запуск
— та же команда; их человек только видит в вопросе.
"""

import json

from gc_core.security.command_moderation import (
    command_call_identity,
    moderate_command_execution,
)
from gc_core.security.permission_store import PermissionStore
from gc_testkit.settings import fake_ctx, make_settings

ROOT = "/work/proj"


def test_plain_command_keeps_the_old_key_and_adds_nothing():
    assert command_call_identity("npm test", {}, ROOT) == (
        "command:npm test",
        [],
    )


def test_directory_inside_the_project_is_shown_but_is_the_same_action():
    key, fields = command_call_identity(
        "npm test", {"cwd": "packages/api"}, ROOT
    )
    assert key == "command:npm test"
    assert fields == [("Каталог", "packages/api")]
    assert (
        command_call_identity("npm test", {"cwd": ROOT}, ROOT)[0]
        == "command:npm test"
    )


def test_directory_outside_the_project_is_a_separate_action():
    key, fields = command_call_identity(
        "git commit -m x", {"cwd": "../other"}, ROOT
    )
    assert key == "command:git commit -m x [outside project: /work/other]"
    assert fields == [("Каталог", "../other")]


def test_a_sibling_with_the_same_prefix_is_outside_the_project():
    key, _ = command_call_identity("ls", {"cwd": "/work/proj-old"}, ROOT)
    assert key == "command:ls [outside project: /work/proj-old]"


def test_environment_is_part_of_the_action_without_its_values():
    env = [{"name": "LD_PRELOAD", "value": "/tmp/secret-token.so"}]
    key, fields = command_call_identity("make", {"env_vars": env}, ROOT)
    assert key.startswith("command:make [env: LD_PRELOAD #")
    assert "secret-token" not in key
    assert fields == [("Окружение", "LD_PRELOAD")]
    other = [{"name": "LD_PRELOAD", "value": "/usr/lib/other.so"}]
    assert command_call_identity("make", {"env_vars": other}, ROOT)[0] != key


def test_background_run_is_shown_but_is_the_same_action():
    key, fields = command_call_identity(
        "npm run dev", {"run_in_background": True}, ROOT
    )
    assert key == "command:npm run dev"
    assert fields == [
        ("Запуск", "в фоне — продолжит работать после ответа агента")
    ]


def _manual_tool(
    tmp_path, monkeypatch, answers, permissions=None, moderation=None
):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    asked = []

    def tui_confirm(*, title, question, fields):
        asked.append(fields)
        return answers.pop(0)

    ctx = fake_ctx(
        is_tui=True,
        tui_confirm=tui_confirm,
        permission_store=PermissionStore(),
    )
    loaded = make_settings(
        {
            "launch_root": str(tmp_path),
            "security.permissions": permissions or {"default_mode": "manual"},
            **(
                {"security.command_moderation": moderation}
                if moderation
                else {}
            ),
        }
    )
    ctx.settings = loaded.settings
    ctx.runtime = loaded.runtime

    class _Tool:
        _ctx = ctx

        @moderate_command_execution(
            payload_arg="command",
            tool_name="execute_command",
            judge_client_extractor=lambda self, *a, **kw: None,
        )
        def run(
            self, command, cwd=None, env_vars=None, run_in_background=False
        ):
            return json.dumps({"ok": True})

        def _command_moderation_blocked_result(self, err, *a, **kw):
            return json.dumps({"ok": False, "error": err})

    return _Tool(), asked


def test_manual_question_shows_what_changes_the_action(tmp_path, monkeypatch):
    tool, asked = _manual_tool(tmp_path, monkeypatch, ["allow_once"])
    tool.run(
        command="npm run dev",
        cwd="web",
        env_vars=[{"name": "PORT", "value": "3000"}],
        run_in_background=True,
    )
    assert asked[0][1:] == [
        ("Каталог", "web"),
        ("Окружение", "PORT"),
        ("Запуск", "в фоне — продолжит работать после ответа агента"),
    ]


def test_session_answer_covers_background_and_subdirectories_but_not_other_projects(
    tmp_path, monkeypatch
):
    tool, asked = _manual_tool(
        tmp_path, monkeypatch, ["allow_remember", "allow_once"]
    )
    assert json.loads(tool.run(command="npm test"))["ok"] is True
    assert (
        json.loads(tool.run(command="npm test", run_in_background=True))["ok"]
        is True
    )
    assert (
        json.loads(tool.run(command="npm test", cwd="packages/api"))["ok"]
        is True
    )
    assert len(asked) == 1
    assert (
        json.loads(tool.run(command="npm test", cwd=str(tmp_path.parent)))[
            "ok"
        ]
        is True
    )
    assert len(asked) == 2


def test_edit_judge_question_shows_the_same_lines_and_remembers_by_the_same_key(
    tmp_path, monkeypatch
):
    """Путь судьи команд в edit (недоступный судья — вопрос человеку): те же
    строки и тот же ключ, что у manual."""
    tool, asked = _manual_tool(
        tmp_path,
        monkeypatch,
        ["allow_remember", "allow_once"],
        permissions={"default_mode": "edit"},
        moderation={"enabled": True, "mode": "ask"},
    )
    tool.run(command="make", env_vars=[{"name": "CC", "value": "clang"}])
    assert ("Окружение", "CC") in asked[0]
    tool.run(command="make", env_vars=[{"name": "CC", "value": "clang"}])
    assert len(asked) == 1
    tool.run(command="make", env_vars=[{"name": "CC", "value": "gcc"}])
    assert len(asked) == 2
