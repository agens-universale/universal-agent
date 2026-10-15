"""Проверка действий недоступна: прогон агента завершается с понятной причиной."""

from __future__ import annotations

import json
from types import SimpleNamespace

from gc_core import processing_responses as pr
from gc_core.execution.state import SharedStateData
from gc_core.processing_responses import arun_agent
from gc_core.security.permission_store import PermissionStore
from gc_core.states import State
from gc_testkit import run_coro
from gc_testkit.settings import fake_ctx

REASON = "The automatic review of actions is unavailable. Run stopped."


def _state():
    state = State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=5,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    state.shared = SharedStateData(permission_mode="edit")
    return state


def _ctx(state):
    return fake_ctx(
        {
            "backend.api_mode": "chat_completions",
            "agent.strict_turn_finish_validation": False,
            "agent.no_tool_call_max_count": 5,
        },
        logger=None,
        auth_connector=None,
        permission_store=PermissionStore(
            state.shared.confirm_rules, shared=state.shared
        ),
    )


def _fc(name, call_id, arguments="{}"):
    return {
        "id": f"id_{call_id}",
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


def _patch_apply(monkeypatch, state, applied, *, stop_on):
    async def fake_apply(**kw):
        name = kw["tool_name"]
        applied.append(name)
        if name == stop_on:
            # так поступает шаг проверки: отказ вызову и причина в состоянии
            state.judge_stop_reason = REASON
            return json.dumps(
                {
                    "tool": name,
                    "ok": False,
                    "error": {"type": "action_check_failed"},
                }
            )
        return json.dumps({"tool": name, "ok": True}, ensure_ascii=False)

    async def _noop(**kw):
        return None

    monkeypatch.setattr(pr, "aapply_tool", fake_apply)
    monkeypatch.setattr(
        pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res
    )
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", _noop)


def test_stop_reason_ends_the_run_and_skips_the_rest_of_the_batch(monkeypatch):
    state = _state()
    applied: list[str] = []
    _patch_apply(monkeypatch, state, applied, stop_on="exec_command")
    persisted: list[dict] = []
    batch = [
        _fc("fs_read_file", "c1", '{"path":"a.py"}'),
        _fc("exec_command", "c2", '{"command":"git push"}'),
        _fc("fs_write_file", "c3", '{"path":"a.py","content":"x"}'),
    ]
    result = run_coro(
        arun_agent(
            batch,
            state,
            _ctx(state),
            persist_messages_callback=lambda msgs: persisted.extend(msgs),
        )
    )
    assert result == REASON
    assert state.terminal_outcome == "judge_unavailable"
    assert applied == ["fs_read_file", "exec_command"]
    outputs = {
        m["call_id"]: m["output"]
        for m in state.messages
        if m.get("type") == "function_call_output"
    }
    # пары вызов-ответ целы и сохранены: следующий прогон не увидит непарный вызов
    assert set(outputs) == {"c1", "c2", "c3"}
    assert json.loads(outputs["c3"])["skipped"] is True
    # сохранены именно ВЫВОДЫ пачки, а не только сами вызовы
    assert {
        m.get("call_id")
        for m in persisted
        if m.get("type") == "function_call_output"
    } == {"c1", "c2", "c3"}
    # причина остановки не переживает прогон
    assert state.judge_stop_reason is None


def test_without_a_stop_reason_the_run_goes_on(monkeypatch):
    state = _state()
    applied: list[str] = []
    _patch_apply(monkeypatch, state, applied, stop_on="никогда")
    run_coro(
        arun_agent(
            [
                _fc("fs_read_file", "c1", '{"path":"a.py"}'),
                _fc("turn_finish", "c2", "{}"),
            ],
            state,
            _ctx(state),
            persist_messages_callback=lambda msgs: None,
        )
    )
    assert state.terminal_outcome != "judge_unavailable"
    assert "turn_finish" in applied


def _paused_executor(*, is_tui):
    import threading

    from gc_core.agent_executor import AgentExecutor
    from gc_core.execution.state import SharedStateData
    from gc_testkit.settings import fake_ctx

    shared = SharedStateData(judge_paused=True, judge_denials_in_a_row=3)
    ex = AgentExecutor.__new__(AgentExecutor)
    ex._lock = threading.Lock()
    ex._ctx = fake_ctx(is_tui=is_tui)
    return ex, SimpleNamespace(shared=shared)


def test_where_no_one_can_be_asked_a_new_user_message_lifts_the_judge_pause():
    """Сервер, `gc-cli` без терминала: человек прочитал «нужно решение
    человека» и написал дальше — это и есть его решение."""
    from gc_core.message_policy.user_message_policy import (
        wrap_technical_user_content,
    )

    ex, state = _paused_executor(is_tui=False)
    ex._lift_judge_pause(
        state, [{"role": "user", "content": wrap_technical_user_content("x")}]
    )
    assert (
        state.shared.judge_paused is True
    )  # служебное сообщение — не человек
    ex._lift_judge_pause(state, [{"role": "user", "content": "продолжай"}])
    assert state.shared.judge_paused is False
    assert state.shared.judge_denials_in_a_row == 0


def test_where_the_human_can_be_asked_only_a_yes_lifts_the_pause():
    ex, state = _paused_executor(is_tui=True)
    ex._lift_judge_pause(state, [{"role": "user", "content": "продолжай"}])
    assert state.shared.judge_paused is True
