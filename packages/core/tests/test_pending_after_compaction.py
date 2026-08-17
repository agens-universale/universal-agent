"""run_agent: сообщение, пришедшее во время turn-end компакции, подхватывается новым ходом."""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from gc_core.intent_guard import guard as intent_guard_guard
from gc_core import processing_responses as pr
from gc_core.intent_guard.validation_result import ValidationResult
from gc_core.states import State


def _make_state(*, max_steps: int = 5) -> State:
    state = State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=max_steps,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    state.backend_api_mode = "chat_completions"
    return state


def _make_ctx(**overrides):
    cfg = {
        "backend_api_mode": "chat_completions",
        "completions_url": "http://backend.local/v1/chat/completions",
        "console_trace": False,
        "no_tool_call_max_count": 5,
        "strict_turn_finish_validation": True,
    }
    cfg.update(overrides)
    return SimpleNamespace(
        config_dict=cfg,
        logger=logging.getLogger("test_pending_after_compaction"),
        auth_connector=None,
    )


def _submit_solution_call():
    return {
        "type": "function_call",
        "name": "turn_finish",
        "arguments": json.dumps(
            {"solution_type": "full_solution",
             "task_report": "Сделано по запросу пользователя",
             "handoff": "{}"},
            ensure_ascii=False,
        ),
        "call_id": "call_finish",
        "id": "fc_finish",
    }


def _expected_finish_return() -> str:
    return _submit_solution_call()["arguments"]


def _accept_validation(**_kwargs):
    return ValidationResult(
        ok=True, action="accept", trace_ok=True, report_ok=True,
        trace_gaps=[], report_mismatches=[], missing_requirements=[],
        conflicts=[], coverage_score=1.0, feedback_for_agent="",
    )


def _pending_once(compacted):
    """get_pending: возвращает новое сообщение ровно один раз — после компакции."""
    def _get_pending():
        if compacted["done"] and not compacted["consumed"]:
            compacted["consumed"] = True
            return [{"role": "user", "content": "новое во время сжатия", "id": "m_new"}]
        return []
    return _get_pending


def test_pending_after_compaction_is_picked_up(monkeypatch):
    """Сообщение, пришедшее во время компакции, подхватывается → backend вызван для нового хода."""
    monkeypatch.setattr(
        intent_guard_guard, "validate_final_report_vs_user_intent", _accept_validation
    )
    compacted = {"done": False, "consumed": False}

    def _hook():
        compacted["done"] = True

    backend_calls: list = []

    def _fake_user_ask(messages, _state, _ctx):
        backend_calls.append([m.get("content") for m in messages])
        return [_submit_solution_call()], "resp_2"

    monkeypatch.setattr(pr, "process_user_ask", _fake_user_ask)

    result = pr.run_agent(
        [_submit_solution_call()],
        _make_state(),
        _make_ctx(),
        turn_end_compaction_hook=_hook,
        get_pending_messages=_pending_once(compacted),
    )

    assert result == _expected_finish_return()
    assert compacted["done"] is True
    # backend (process_user_ask) вызван РОВНО один раз — для продолженного хода с новым сообщением
    assert backend_calls == [["новое во время сжатия"]]


def test_no_pending_finishes_normally(monkeypatch):
    """Нет pending → финиш как раньше: компакция один раз, backend не зовётся (регресс инварианта)."""
    monkeypatch.setattr(
        intent_guard_guard, "validate_final_report_vs_user_intent", _accept_validation
    )
    hook_calls: list = []

    def _hook():
        hook_calls.append(1)

    ua_calls: list = []

    def _fake_user_ask(_messages, _state, _ctx):
        ua_calls.append(1)
        return [_submit_solution_call()], "resp"

    monkeypatch.setattr(pr, "process_user_ask", _fake_user_ask)

    result = pr.run_agent(
        [_submit_solution_call()],
        _make_state(),
        _make_ctx(),
        turn_end_compaction_hook=_hook,
        get_pending_messages=lambda: [],
    )

    assert result == _expected_finish_return()
    assert hook_calls == [1]
    assert ua_calls == []


def test_stop_after_compaction_does_not_continue(monkeypatch):
    """stop_requested во время компакции → после continue верх цикла возвращает None, нового хода нет."""
    monkeypatch.setattr(
        intent_guard_guard, "validate_final_report_vs_user_intent", _accept_validation
    )
    compacted = {"done": False, "consumed": False}
    stopped = {"flag": False}

    def _hook():
        compacted["done"] = True
        stopped["flag"] = True  # стоп выставлен во время компакции

    ua_calls: list = []

    def _fake_user_ask(_messages, _state, _ctx):
        ua_calls.append(1)
        return [_submit_solution_call()], "resp"

    monkeypatch.setattr(pr, "process_user_ask", _fake_user_ask)

    result = pr.run_agent(
        [_submit_solution_call()],
        _make_state(),
        _make_ctx(),
        turn_end_compaction_hook=_hook,
        get_pending_messages=_pending_once(compacted),
        check_stop=lambda: stopped["flag"],
    )

    assert result is None
    assert ua_calls == []


def test_continued_turn_passes_through_finish_gate(monkeypatch):
    """Продолженный после компакции ход проходит finish-gate штатно: gate отрабатывает
    для ОБОИХ ходов и run завершается (новый ход не ломает/не пропускает gate)."""
    compacted = {"done": False, "consumed": False}

    def _hook():
        compacted["done"] = True

    def _fake_user_ask(_messages, _state, _ctx):
        return [_submit_solution_call()], "resp_2"

    monkeypatch.setattr(pr, "process_user_ask", _fake_user_ask)

    gate_calls: list = []

    def _capture(**kwargs):
        gate_calls.append(kwargs)
        return _accept_validation()

    monkeypatch.setattr(
        intent_guard_guard, "validate_final_report_vs_user_intent", _capture
    )

    result = pr.run_agent(
        [_submit_solution_call()],
        _make_state(),
        _make_ctx(strict_turn_finish_validation=True),
        turn_end_compaction_hook=_hook,
        get_pending_messages=_pending_once(compacted),
    )

    assert result == _expected_finish_return()
    # finish-gate отработал дважды: первый ход + продолженный (после компакции)
    assert len(gate_calls) == 2
