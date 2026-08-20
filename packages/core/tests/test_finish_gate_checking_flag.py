"""Флаг «судья turn_finish работает сейчас» (meta.runtime.finish_gate_checking) —
жизненный цикл в run_agent (turn_finish-ветка). Поднят только при gate.enabled
(судья реально пойдёт); снят строго в finally — даже при исключении."""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from gc_core.intent_guard import guard as intent_guard_guard
from gc_core.intent_guard.validation_result import ValidationResult
from gc_core.states import State


def _make_state() -> State:
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
    state.backend_api_mode = "chat_completions"
    return state


def _make_ctx(**overrides):
    cfg = {
        "backend_api_mode": "chat_completions",
        "strict_turn_finish_validation": True,
        "finish_gate_max_iterations": 10,
    }
    cfg.update(overrides)
    return SimpleNamespace(
        config_dict=cfg,
        logger=logging.getLogger("test_finish_gate_checking_flag"),
        auth_connector=None,
    )


def _submit_solution_call() -> dict:
    args = {
        "solution_type": "direct_answer",
        "task_report": "done",
        "handoff": json.dumps({}, ensure_ascii=False),
    }
    return {
        "type": "function_call",
        "name": "turn_finish",
        "arguments": json.dumps(args, ensure_ascii=False),
        "call_id": "call_finish",
        "id": "fc_finish",
    }


def _accept_validation() -> ValidationResult:
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


def test_flag_raised_during_judge_call_and_cleared_after_accept(monkeypatch):
    """Флаг True ровно на время вызова судьи; после accept — снят (finally)."""
    calls: list[bool] = []

    async def _accept(**_kwargs):
        # В момент вызова судьи флаг уже должен быть поднят (True) — это сигнал,
        # который включает TUI-индикатор «сверяюсь…».
        assert calls == [True]
        return _accept_validation()

    monkeypatch.setattr(
        intent_guard_guard, "avalidate_final_report_vs_user_intent", _accept
    )

    from gc_core.processing_responses import run_agent

    result = run_agent(
        [_submit_solution_call()],
        _make_state(),
        _make_ctx(),
        mark_finish_gate_checking=lambda active: calls.append(active),
    )

    assert result is not None
    assert calls == [True, False]


def test_flag_not_raised_on_smart_skip(monkeypatch):
    """smart + 0 tool calls → gate.enabled=False, судья не зовётся → индикатор
    «сверяюсь…» не вспыхивает (нет True/False в runtime-meta)."""
    calls: list[bool] = []
    judge_calls: list[int] = []

    async def _should_not_run(**_kwargs):
        judge_calls.append(1)
        return _accept_validation()

    monkeypatch.setattr(
        intent_guard_guard, "avalidate_final_report_vs_user_intent", _should_not_run
    )

    from gc_core.processing_responses import run_agent

    state = _make_state()
    state.turn_tool_calls = 0
    result = run_agent(
        [_submit_solution_call()],
        state,
        _make_ctx(finish_gate_smart=True),
        mark_finish_gate_checking=lambda active: calls.append(active),
    )

    assert result is not None
    assert judge_calls == []
    assert calls == []


def test_flag_cleared_in_finally_even_if_guard_raises(monkeypatch):
    """try/finally вокруг evaluate_turn_finish: исключение судьи не оставляет флаг
    залипшим True (иначе индикатор «сверяюсь…» завис бы навсегда)."""
    calls: list[bool] = []

    class _BoomGuard:
        enabled = True  # иначе run_agent не поднимет флаг (только при enabled)

        def __init__(self, **_kwargs) -> None:
            pass

        async def aevaluate_turn_finish(self, **_kwargs):
            raise RuntimeError("judge boom")

    from gc_core import processing_responses as pr

    monkeypatch.setattr(pr, "FinishGateGuard", _BoomGuard)

    with pytest.raises(RuntimeError):
        pr.run_agent(
            [_submit_solution_call()],
            _make_state(),
            _make_ctx(),
            mark_finish_gate_checking=lambda active: calls.append(active),
        )

    assert calls == [True, False]


def test_flag_absent_when_callback_not_wired():
    """CLI/server без TUI: callback не передан → run_agent работает как раньше,
    без AttributeError/TypeError (mark_finish_gate_checking=None по умолчанию)."""
    monkeypatch_calls: list[bool] = []
    from gc_core.intent_guard import guard as guard_mod

    orig = guard_mod.avalidate_final_report_vs_user_intent
    guard_mod.avalidate_final_report_vs_user_intent = lambda **_kwargs: _accept_validation()
    try:
        from gc_core.processing_responses import run_agent

        result = run_agent([_submit_solution_call()], _make_state(), _make_ctx())
    finally:
        guard_mod.avalidate_final_report_vs_user_intent = orig

    assert result is not None
    assert monkeypatch_calls == []
