"""Backend smart (спека §3): судья при >=1 вызове инструмента за ход.
Счётчик — State.turn_tool_calls; проекция — apply_finish_gate_mode;
чтение — FinishGateGuard.enabled. CLI/server ключ finish_gate_smart не ставят."""

from types import SimpleNamespace

import pytest
from gc_testkit import run_coro


def _async_noop(fn):
    """Заглушка корутинного хука: ядро зовёт async-двойник напрямую."""

    async def _wrapped(*args, **kwargs):
        return fn(*args, **kwargs)

    return _wrapped


def test_schema_default_is_smart():
    from gc_core.config_modules.tui import TuiConfig

    cfg = TuiConfig()
    assert cfg.finish_gate_mode == "smart"
    assert cfg.text_answer_mode == "judge"


def test_schema_accepts_smart():
    from gc_core.config_modules.tui import TuiConfig

    assert TuiConfig(finish_gate_mode="smart").finish_gate_mode == "smart"
    assert TuiConfig(finish_gate_mode="on").finish_gate_mode == "on"
    assert TuiConfig(finish_gate_mode="off").finish_gate_mode == "off"


def test_schema_rejects_unknown_mode():
    from gc_core.config_modules.tui import TuiConfig

    with pytest.raises(Exception):
        TuiConfig(finish_gate_mode="maybe")


def test_state_counter_starts_zero_and_resets_on_flush():
    from gc_core.states import State

    st = State.__new__(
        State
    )  # без тяжёлого __init__ (get_project_data и т.п.)
    st.turn_tool_calls = 0
    st.current_user_intent = []
    st.user_intent_log = []
    st.user_turn_seq = 0
    st.turn_tool_calls = 5
    st.flush_current_user_intent()
    assert st.turn_tool_calls == 0  # сброс на границе хода


def test_apply_finish_gate_mode_projects_smart():
    from gc_tui.toggles import apply_finish_gate_mode

    cfg = {
        "tui_finish_gate_mode": "smart",
        "tui_finish_gate_mode_explicit": True,
    }
    apply_finish_gate_mode(cfg)
    assert (
        cfg["strict_turn_finish_validation"] is True
    )  # судья потенциально включён
    assert cfg["finish_gate_smart"] is True


def test_apply_finish_gate_mode_on_off_unchanged():
    from gc_tui.toggles import apply_finish_gate_mode

    on = {"tui_finish_gate_mode": "on", "tui_finish_gate_mode_explicit": True}
    apply_finish_gate_mode(on)
    assert (
        on["strict_turn_finish_validation"] is True
        and on["finish_gate_smart"] is False
    )
    off = {
        "tui_finish_gate_mode": "off",
        "tui_finish_gate_mode_explicit": True,
    }
    apply_finish_gate_mode(off)
    assert (
        off["strict_turn_finish_validation"] is False
        and off["finish_gate_smart"] is False
    )


def _guard(cfg, turn_tool_calls):
    from gc_core.intent_guard.guard import FinishGateGuard

    ctx = SimpleNamespace(config_dict=cfg)
    state = SimpleNamespace(
        parent_state_id=None, turn_tool_calls=turn_tool_calls
    )
    return FinishGateGuard(ctx=ctx, state=state)


def test_smart_skips_judge_without_tool_calls():
    cfg = {
        "backend_api_mode": "chat_completions",
        "strict_turn_finish_validation": True,
        "finish_gate_smart": True,
    }
    assert _guard(cfg, 0).enabled is False
    assert _guard(cfg, 1).enabled is True


def test_cli_server_unaffected_no_smart_key():
    """CLI/server: apply_finish_gate_mode не зовётся → ключа finish_gate_smart нет →
    guard работает как прежде (strict AND chat_completions), счётчик не читается."""
    cfg = {
        "backend_api_mode": "chat_completions",
        "strict_turn_finish_validation": True,
    }
    assert (
        _guard(cfg, 0).enabled is True
    )  # 0 вызовов не гасит судью без smart-ключа
    off = {
        "backend_api_mode": "chat_completions",
        "strict_turn_finish_validation": False,
    }
    assert _guard(off, 5).enabled is False


def test_mid_turn_user_ask_does_not_reset_tool_counter():
    """Реальная mid-turn реплика (как после drain pending → persist) не трогает
    turn_tool_calls — сброс только через flush на границе хода."""
    from gc_core.intent_log.user_turn import append_user_asks_from_messages
    from gc_core.states import State

    st = State.__new__(State)
    st.turn_tool_calls = 4
    st.current_user_intent = []
    st.user_turn_seq = 0

    st.current_user_intent = append_user_asks_from_messages(
        st.current_user_intent,
        [{"role": "user", "content": "уточнение mid-turn", "id": "m_mid"}],
        user_turn_seq=st.user_turn_seq,
    )
    assert st.turn_tool_calls == 4
    assert st.current_user_intent  # ask записан в открытый ход


def test_technical_user_message_does_not_reset_tool_counter():
    """Technical user (reject судьи / bounce / bad args) не сбрасывает счётчик
    и не попадает в current_user_intent."""
    from gc_core.intent_log.user_turn import append_user_asks_from_messages
    from gc_core.processing_responses import (
        _make_technical_feedback_user_message,
    )
    from gc_core.states import State

    st = State.__new__(State)
    st.turn_tool_calls = 3
    st.current_user_intent = []
    st.user_turn_seq = 0

    tech = _make_technical_feedback_user_message("finish gate reject feedback")
    st.current_user_intent = append_user_asks_from_messages(
        st.current_user_intent,
        [tech],
        user_turn_seq=st.user_turn_seq,
    )
    assert st.turn_tool_calls == 3
    assert st.current_user_intent == []


def test_run_agent_mid_turn_pending_preserves_counter_smart_gate_runs(
    monkeypatch,
):
    """Юзер дописал во время хода (pending drain) — счётчик не обнуляется;
    finish_gate_smart всё ещё видит tool calls и зовёт судью."""
    import json
    import logging

    from gc_core import processing_responses as pr
    from gc_core.intent_guard import guard as intent_guard_guard
    from gc_core.intent_guard.validation_result import ValidationResult
    from gc_core.states import State

    async def stub_turn_finish_apply(**kwargs):
        tool_name = kwargs.get("tool_name")
        arguments = kwargs.get("arguments")
        if tool_name != "turn_finish":
            return json.dumps(
                {"tool": tool_name, "ok": True}, ensure_ascii=False
            )
        try:
            payload = (
                json.loads(arguments) if isinstance(arguments, str) else {}
            )
        except Exception:
            payload = {}
        result = {
            "tool": "turn_finish",
            "ok": True,
            "status": "submitted",
            "message": "Task outcome submitted.",
            "error": None,
        }
        if isinstance(payload, dict):
            for key in ("solution_type", "comment", "task_report", "handoff"):
                if key in payload:
                    result[key] = payload[key]
        return json.dumps(result, ensure_ascii=False)

    gate_calls: list = []

    async def _capture(**_kwargs):
        gate_calls.append(1)
        return ValidationResult(
            ok=True,
            action="accept",
            trace_ok=True,
            report_ok=True,
            trace_gaps=[],
            report_mismatches=[],
            missing_requirements=[],
            conflicts=[],
            coverage_score=1.0,
            feedback_for_agent="",
        )

    monkeypatch.setattr(
        intent_guard_guard, "avalidate_final_report_vs_user_intent", _capture
    )

    phase = {"n": 0}

    async def _fake_user_ask(_messages, _state, _ctx):
        phase["n"] += 1
        if phase["n"] == 1:
            return [
                {
                    "type": "function_call",
                    "name": "read_file",
                    "arguments": "{}",
                    "call_id": "c_read",
                    "id": "fc_read",
                }
            ], "r1"
        return [
            {
                "type": "function_call",
                "name": "turn_finish",
                "arguments": json.dumps(
                    {
                        "solution_type": "full_solution",
                        "task_report": "done",
                        "handoff": "{}",
                    },
                    ensure_ascii=False,
                ),
                "call_id": "call_finish",
                "id": "fc_finish",
            }
        ], "r2"

    monkeypatch.setattr(pr, "aprocess_user_ask", _fake_user_ask)
    # Заглушка КОРУТИННАЯ: ядро ждёт `aapply_tool` нативно, синхронная даёт
    # "object str can't be used in 'await' expression".
    monkeypatch.setattr(pr, "aapply_tool", stub_turn_finish_apply)
    monkeypatch.setattr(
        pr, "maybe_store_and_wrap_tool_output", lambda res, **_kw: res
    )
    monkeypatch.setattr(
        pr,
        "amaybe_update_intent_scope_from_tool",
        _async_noop(lambda **_kw: None),
    )

    state = State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    state.backend_api_mode = "chat_completions"

    pending_sent = {"done": False}
    counter_when_pending: list[int] = []

    def get_pending():
        # После tool-call счётчик уже >0 — имитируем mid-turn реплику юзера.
        if not pending_sent["done"] and int(state.turn_tool_calls or 0) >= 1:
            pending_sent["done"] = True
            counter_when_pending.append(state.turn_tool_calls)
            return [
                {
                    "role": "user",
                    "content": "уточнение mid-turn",
                    "id": "m_mid",
                }
            ]
        return []

    ctx = SimpleNamespace(
        config_dict={
            "backend_api_mode": "chat_completions",
            "strict_turn_finish_validation": True,
            "finish_gate_smart": True,
            "no_tool_call_max_count": 5,
        },
        logger=logging.getLogger("test_finish_gate_smart_mid_turn"),
        auth_connector=None,
    )

    result = run_coro(
        pr.arun_agent(
            [{"role": "user", "content": "сделай X", "id": "m0"}],
            state,
            ctx,
            get_pending_messages=get_pending,
        )
    )

    assert pending_sent["done"] is True
    assert counter_when_pending == [1]  # mid-turn не сбросил счётчик
    assert len(gate_calls) == 1  # smart-гейт всё ещё вызвал судью
    assert result is not None
