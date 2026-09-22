"""Task 5: инжект protected-путей в промпт судьи + C1 Часть 2.

C1 Часть 2: при недоступности судьи и mode != allow →
- если confirm доступен → ask через порт (user confirmed → allow)
- если confirm недоступен → fail-closed deny (как раньше)

Protected paths injection: build_judge_payload добавляет поле never_touch_paths
с объединённым списком instruction + operational защищённых путей.
"""
import json

import pytest

from gc_core.security import command_moderation as cm
from gc_core.security.command_moderation import ModerationContext, build_judge_payload, decide_command_execution, moderate_command_execution


# ---------------------------------------------------------------------------
# C1 Часть 2: judge-unavailable → confirm-порт
# ---------------------------------------------------------------------------


def test_judge_unavailable_deny_mode_asks_when_confirm_available(monkeypatch):
    """deny + судья недоступен + confirm доступен → сначала «нужно подтверждение»,
    по «да» — allow.

    Протокол двухшаговый, как у ask: сама функция человека не спрашивает, ожидание
    — дело драйвера гарда (OffLoop). Раньше она звала confirm-порт сама, а тело
    гарда зовёт её инлайн, и в асинхронном пути ожидание шло на event loop.
    """
    monkeypatch.setattr(cm, "confirm_available", lambda ctx: True)
    ctx = object()
    asked = decide_command_execution("deny", None, False, None, ctx=ctx)
    assert asked.allowed is False
    assert asked.requires_confirmation is True
    assert asked.reason_code == cm.JUDGE_UNAVAILABLE_CONFIRM_REQUIRED
    dec = decide_command_execution("deny", None, False, True, ctx=ctx)
    assert dec.allowed is True
    assert dec.reason_code == "judge_unavailable_user_confirmed"


def test_judge_unavailable_deny_mode_blocks_when_no_confirm(monkeypatch):
    """deny + судья недоступен + confirm недоступен → fail-closed."""
    monkeypatch.setattr(cm, "confirm_available", lambda ctx: False)
    ctx = object()
    dec = decide_command_execution("deny", None, False, None, ctx=ctx)
    assert dec.allowed is False
    assert dec.reason_code == "judge_unavailable"


def test_judge_unavailable_deny_mode_blocks_when_confirm_rejected(monkeypatch):
    """deny + судья недоступен + пользователь отказал → fail-closed."""
    monkeypatch.setattr(cm, "confirm_available", lambda ctx: True)
    dec = decide_command_execution("deny", None, False, False, ctx=object())
    assert dec.allowed is False
    assert dec.reason_code == "judge_unavailable"


def test_decision_never_asks_the_human_itself(monkeypatch):
    """Решение чистое: ожидание человека уходит в поток через драйвер гарда."""
    def _boom(**_k):
        raise AssertionError("decide_command_execution сама спросила человека — на loop'е")

    monkeypatch.setattr(cm, "request_confirm", _boom)
    monkeypatch.setattr(cm, "confirm_available", lambda ctx: True)
    for mode in ("ask", "deny"):
        decide_command_execution(mode, None, False, None, ctx=object())


def test_judge_unavailable_allow_mode_skips_confirm(monkeypatch):
    """allow + судья недоступен → немедленно allow (confirm-порт не трогаем)."""
    called = []
    monkeypatch.setattr(cm, "confirm_available", lambda ctx: called.append(1) or True)
    ctx = object()
    dec = decide_command_execution("allow", None, False, None, ctx=ctx)
    assert dec.allowed is True
    assert not called  # confirm_available не должен вызываться для mode=allow


def test_judge_unavailable_no_ctx_still_blocks(monkeypatch):
    """Без ctx confirm недоступен → fail-closed (обратная совместимость)."""
    # confirm_available получит ctx=None и вернёт False (no isatty в тестовой среде)
    dec = decide_command_execution("deny", None, False, None)
    assert dec.allowed is False


# ---------------------------------------------------------------------------
# Protected paths injection в build_judge_payload
# ---------------------------------------------------------------------------


def test_build_judge_payload_includes_never_touch_paths(monkeypatch, tmp_path):
    """build_judge_payload добавляет never_touch_paths из protected_paths."""
    from pathlib import Path
    from gc_core.security.protected_paths import ProtectedSet

    instr = tmp_path / "instructions.md"
    oper = tmp_path / ".gc"
    fake_ps = ProtectedSet(
        instructions=frozenset({instr}),
        operational=frozenset({oper}),
        skills_exceptions=frozenset(),
    )
    monkeypatch.setattr(cm, "_get_protected_paths", lambda cwd: fake_ps)

    context = ModerationContext(
        tool_name="exec_command",
        launch_root=str(tmp_path),
        cwd=str(tmp_path),
    )
    payload = build_judge_payload("exec_command", "rm -rf /", context)

    assert "never_touch_paths" in payload
    paths_in_payload = payload["never_touch_paths"]
    assert isinstance(paths_in_payload, list)
    assert len(paths_in_payload) == 2
    assert str(instr) in paths_in_payload
    assert str(oper) in paths_in_payload


def test_build_judge_payload_never_touch_paths_empty_on_error(monkeypatch, tmp_path):
    """При ошибке protected_paths → never_touch_paths пустой список (не падаем)."""
    def bad_ps(cwd):
        raise RuntimeError("cannot load")

    monkeypatch.setattr(cm, "_get_protected_paths", bad_ps)

    context = ModerationContext(
        tool_name="exec_command",
        launch_root=str(tmp_path),
        cwd=str(tmp_path),
    )
    payload = build_judge_payload("exec_command", "echo ok", context)
    assert payload.get("never_touch_paths") == []


# ---------------------------------------------------------------------------
# I-2: wrapper threads ctx → C1 Часть 2 доступна live через декоратор
# ---------------------------------------------------------------------------


def test_wrapper_threads_ctx_to_confirm_port(monkeypatch, tmp_path):
    """Обёртка moderate_command_execution передаёт ctx в decide_command_execution.

    Сценарий: судья недоступен, mode=deny, confirm-порт доступен и пользователь
    подтверждает → команда должна быть выполнена (allowed).
    Ctx передаётся в wrapper через kwarg ctx; resolve_tool_context пробрасывает
    его в ToolSecurityContext.ctx, откуда wrapper берёт его для decide_command_execution.
    """
    from types import SimpleNamespace

    received_ctx: list = []

    def fake_confirm_available(ctx):
        received_ctx.append(ctx)
        return True

    monkeypatch.setattr(cm, "confirm_available", fake_confirm_available)
    monkeypatch.setattr(cm, "request_confirm", lambda **k: True)
    # Судья всегда недоступен — кидаем исключение.
    #
    # Подменять надо ШОВ, через который ходит ТЕЛО гварда. Здесь стояла
    # подмена `run_command_judge_with_retry` — она перестала что-либо делать,
    # когда тело перешло на `judge_with_retry_flow` (судья теперь ждётся на
    # loop вызывающего). Тест остался зелёным по совпадению: судья в этом
    # конфиге и так недоступен, то есть fail-closed он больше НЕ проверял.
    async def _judge_is_down(_payload):
        raise cm.JudgeUnavailable("test")

    monkeypatch.setattr(cm, "_default_judge_client", lambda *a, **k: _judge_is_down)

    fake_ctx = SimpleNamespace(
        config_dict={
            "launch_root": str(tmp_path),
            "command_moderation": {"enabled": True, "mode": "deny"},
        },
        logger=None,
    )

    @moderate_command_execution(payload_arg="command", tool_name="exec_command")
    def run(command: str, ctx=None) -> str:
        return json.dumps({"ok": True})

    result = json.loads(run("echo hello", ctx=fake_ctx))

    assert result["ok"] is True, "confirm-порт должен разрешить выполнение"
    assert received_ctx and received_ctx[0] is fake_ctx, "ctx должен быть проброшен в confirm_available"
