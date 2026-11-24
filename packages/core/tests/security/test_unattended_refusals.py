"""Отказ защиты в запуске без человека: «продолжай задачу без этого действия».

Спросить некого (Harbor, сервер, A2A) — значит, инструкция «спроси
пользователя, как быть» модели не годится: послушная модель бросала задачу
на первом же упоминании `AGENTS.md` в команде. Запрет повтора и обхода
остаётся; меняется только то, что делать после отказа. С человеком тексты
прежние.
"""

import json

import pytest
from gc_core.message_policy.permission_denials import UNATTENDED_GUIDANCE
from gc_core.security.command_guard import enforce_command_guard
from gc_core.security.command_moderation import NO_WORKAROUND_NOTICE
from gc_core.security.path_policy import enforce_write_guard
from gc_core.security.read_guard import enforce_read_guard
from gc_testkit.settings import fake_ctx


class _Logger:
    def __init__(self):
        self.events = []

    def info(self, *_a, **_k):
        pass

    debug = warning = error = info


@enforce_write_guard(path_arg="path")
def _write(path: str, **_kwargs) -> str:
    raise AssertionError(f"Guard allowed the write to {path}")


@enforce_read_guard(path_arg="path")
def _read(path: str, **_kwargs) -> str:
    raise AssertionError(f"Guard allowed the read of {path}")


@enforce_command_guard(payload_arg="command", tool_name="exec_command")
def _command(command: str, **_kwargs) -> str:
    raise AssertionError(f"Guard allowed the command {command}")


@pytest.fixture
def project(tmp_path, monkeypatch):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    (tmp_path / ".gc").mkdir()
    return tmp_path


def _ctx(root, *, human):
    """``human=False`` — спросить некого; ``True`` — человек отвечает «нет»."""
    return fake_ctx(
        config={
            "launch_root": str(root),
            "security.write_guard": {"enabled": False},
            "security.read_guard": {"enabled": False},
            "security.command_moderation": {"mode": "allow"},
        },
        logger=_Logger(),
        is_tui=human,
        tui_confirm=(lambda **_k: "deny_once") if human else None,
    )


def _error(raw: str) -> dict:
    out = json.loads(raw)
    assert out["ok"] is False, out
    return out["error"]


def test_unattended_guidance_says_continue_and_keeps_the_no_workaround_rule():
    assert "Continue the task" in UNATTENDED_GUIDANCE
    assert "Do NOT retry" in UNATTENDED_GUIDANCE
    assert "another way" in UNATTENDED_GUIDANCE
    assert "ask how to proceed" not in UNATTENDED_GUIDANCE


@pytest.mark.parametrize(
    "command",
    [
        "head input.csv; cat AGENTS.md",  # ask: файл инструкций
        "ls -la .gc",  # deny: служебный каталог
    ],
)
def test_command_refused_without_a_human_tells_to_continue(project, command):
    err = _error(
        _command(
            command=command, cwd=str(project), ctx=_ctx(project, human=False)
        )
    )
    assert err["type"] == "protected_path_denied"
    assert err["guidance"] == UNATTENDED_GUIDANCE


@pytest.mark.parametrize(
    "call",
    [
        lambda root, ctx: _write(path=str(root / ".gc" / "x"), ctx=ctx),
        lambda root, ctx: _write(path=str(root / "AGENTS.md"), ctx=ctx),
        lambda root, ctx: _read(path=str(root / ".gc" / "x"), ctx=ctx),
    ],
    ids=["write-deny", "write-ask", "read-deny"],
)
def test_file_tool_refused_without_a_human_tells_to_continue(project, call):
    err = _error(call(project, _ctx(project, human=False)))
    assert err["type"] == "protected_path_denied"
    assert err["guidance"] == UNATTENDED_GUIDANCE


def test_with_a_human_the_command_deny_keeps_its_guidance(project):
    err = _error(
        _command(
            command="ls -la .gc",
            cwd=str(project),
            ctx=_ctx(project, human=True),
        )
    )
    assert err["guidance"] == NO_WORKAROUND_NOTICE


def test_with_a_human_a_declined_question_is_not_called_unattended(project):
    err = _error(
        _write(path=str(project / "AGENTS.md"), ctx=_ctx(project, human=True))
    )
    assert err["guidance"] != UNATTENDED_GUIDANCE
