"""E2E finish gate — регрессия §1.2 SPEC (две оси, fix_report без повтора ask_human)."""

import importlib
from gc_testkit import as_coro
import json
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import importlib

from gc_core.intent_guard import guard as intent_guard_guard
import pytest

from gc_testkit.intent_fixtures import (
    intent_user_ask_texts,
    intent_user_ask_texts_from_jsonl,
    seed_state_intent,
)
from gc_core.intent_log import append_tool_aggregated
from gc_core.intent_guard.feedback import build_finish_gate_feedback
from gc_core.intent_guard.guard import FinishGateGuard
from gc_core.intent_guard.validation_result import ValidationResult
from gc_testkit.paths import module_dir
from gc_testkit import run_coro

_trace_digest_tests = importlib.import_module("test_intent_guard_trace_digest")
_ask_human_scenario_messages = _trace_digest_tests._ask_human_scenario_messages


processing_module = None
arun_agent = None
State = None


def _stub_tools_modules() -> None:
    for name in list(sys.modules):
        if name == "gc_core.tools" or name.startswith("gc_core.tools."):
            sys.modules.pop(name, None)

    tools_pkg = types.ModuleType("gc_core.tools")

    class _WrongFunctionArgumentsException(Exception):
        def to_llm_feedback(self):
            return str(self)

    # Ветка await'ит `aapply_tool`: синхронная заглушка здесь давала бы
    # `TypeError: object str can't be used in 'await' expression`.
    async def _apply_tool(*_args, **_kwargs):
        tool_name = _kwargs.get("tool_name")
        arguments = _kwargs.get("arguments")
        try:
            payload = json.loads(arguments) if isinstance(arguments, str) else {}
        except Exception:
            payload = {}
        if tool_name == "turn_finish":
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
        return json.dumps(
            {"tool": tool_name, "ok": True, "status": "executed"},
            ensure_ascii=False,
        )

    def _build_tools_data(_config_dict):
        return {}, [], []

    tools_pkg.aapply_tool = _apply_tool
    # Имена приехали с develop: processing_responses импортирует их из
    # пакета, и заглушка обязана их отдавать — иначе падает импорт модуля.
    tools_pkg.is_internal_tool_error_output = lambda *_a, **_k: False
    tools_pkg.is_unknown_tool_output = lambda *_a, **_k: False
    tools_pkg.tool_arguments_valid = lambda *_a, **_k: True
    tools_pkg.abuild_tools_data = _build_tools_data
    tools_pkg.WrongFunctionArgumentsException = _WrongFunctionArgumentsException
    tools_dir = module_dir("tools")
    tools_pkg.__path__ = [str(tools_dir)]
    sys.modules["gc_core.tools"] = tools_pkg


@pytest.fixture(scope="module", autouse=True)
def _load_processing_responses():
    global processing_module, arun_agent, State
    _stub_tools_modules()
    from gc_core import processing_responses as pm
    from gc_core.processing_responses import arun_agent as ra
    from gc_core.states import State as St

    processing_module = pm
    arun_agent = ra
    State = St
    # Заглушка ставится ПРЯМЫМ присваиванием (не monkeypatch), поэтому снимать
    # её обязаны мы сами. Раньше teardown выбрасывал только модули из
    # sys.modules, а `processing_responses.aapply_tool` оставался заглушкой на
    # весь остаток прогона — и `test_parity_smoke` падал пятью тестами, проходя
    # при этом в одиночку.
    pm.aapply_tool = sys.modules["gc_core.tools"].aapply_tool
    yield
    for name in list(sys.modules):
        if name == "gc_core.tools" or name.startswith("gc_core.tools."):
            sys.modules.pop(name, None)
    # Сохранить «прежнее» значение до yield нельзя: к этому моменту оно уже
    # заглушка — pm импортирован после подмены. Настоящую функцию берём
    # переимпортом уже очищенного пакета.
    import gc_core.tools as real_tools

    pm.aapply_tool = real_tools.aapply_tool


def _make_state(*, max_steps: int = 5, parent_state_id=None):
    state = State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=max_steps,
        system_prompt_id="v1",
        use_server_subagents=False,
        parent_state_id=parent_state_id,
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
        logger=logging.getLogger("test_finish_gate_two_axis"),
        auth_connector=None,
    )


def _submit_solution_call(
    *,
    solution_type: str = "full_solution",
    handoff: dict | None = None,
):
    args: dict = {
        "solution_type": solution_type,
        "task_report": "done",
        "handoff": json.dumps(handoff if handoff is not None else {}, ensure_ascii=False),
    }
    return {
        "type": "function_call",
        "name": "turn_finish",
        "arguments": json.dumps(args, ensure_ascii=False),
        "call_id": "call_finish",
        "id": "fc_finish",
    }


def _scenario_12_validation(**_kwargs):
    """trace_ok: ask_human уже был; report_ok: full_solution не подходит."""
    return ValidationResult(
        ok=False,
        action="fix_report",
        trace_ok=True,
        report_ok=False,
        trace_gaps=[],
        report_mismatches=[
            "solution_type full_solution не соответствует: вопрос уже задан через ask_human"
        ],
        missing_requirements=[],
        conflicts=[],
        coverage_score=0.5,
        feedback_for_agent="Снова вызови ask_human и задай вопрос пользователю.",
    )


def test_fix_report_feedback_does_not_repeat_ask_human():
    validation = _scenario_12_validation()
    feedback = build_finish_gate_feedback(validation)
    assert "fix_report" in feedback
    assert "without repeating" in feedback
    lowered = feedback.lower()
    assert "задай вопрос" not in lowered
    assert "снова вызови ask_human" not in lowered
    assert validation.feedback_for_agent not in feedback


def test_guard_reject_uses_structured_feedback_not_raw_llm(monkeypatch):
    monkeypatch.setattr(
        intent_guard_guard,
        "avalidate_final_report_vs_user_intent",
            as_coro(_scenario_12_validation),
    )
    state = _make_state()
    seed_state_intent(
        state,
        current="спроси меня что нибудь",
        log="спроси меня что нибудь",
    )
    guard = FinishGateGuard(ctx=_make_ctx(), state=state)
    decision = run_coro(guard.aevaluate_turn_finish(
        finish_handoff_arguments={"solution_type": "full_solution"},
    ))
    assert decision.outcome == "reject"
    lowered = decision.feedback_text.lower()
    assert "задай вопрос" not in lowered
    assert "снова вызови ask_human" not in lowered
    assert "Снова вызови ask_human" not in decision.feedback_text


def test_run_agent_rejects_submit_with_fix_report_feedback(monkeypatch):
    monkeypatch.setattr(
        intent_guard_guard,
        "avalidate_final_report_vs_user_intent",
            as_coro(_scenario_12_validation),
    )

    def _stub_user_ask(_messages, _state, _ctx):
        return (
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "retry"}],
                    "id": "m_after_gate",
                }
            ],
            "rid_stub",
        )

    monkeypatch.setattr(
        processing_module, "aprocess_user_ask", as_coro(_stub_user_ask)
    )

    state = _make_state(max_steps=0)
    seed_state_intent(
        state,
        current="спроси меня что нибудь",
        log="спроси меня что нибудь",
    )

    run_coro(arun_agent(
        [_submit_solution_call()],
        state,
        _make_ctx(),
    ))

    tool_output = next(
        m
        for m in state.messages
        if m.get("type") == "function_call_output" and m.get("call_id") == "call_finish"
    )
    payload = json.loads(tool_output["output"])
    assert payload["ok"] is False
    assert payload["status"] == "rejected_by_finish_gate"
    assert "fix_report" in payload["message"]
    assert payload["finish_gate"]["action"] == "fix_report"
    assert "задай вопрос" not in payload["message"].lower()
    assert "снова вызови ask_human" not in payload["message"].lower()


def test_run_agent_accept_clears_current_user_intent_for_subagent(monkeypatch):
    def _accept(**_kwargs):
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
        intent_guard_guard,
        "avalidate_final_report_vs_user_intent",
            as_coro(_accept),
    )

    state = _make_state(parent_state_id="orch-parent")
    seed_state_intent(
        state,
        current="subagent task",
        log="subagent task",
    )

    result = run_coro(arun_agent(
        [_submit_solution_call(solution_type="direct_answer")],
        state,
        _make_ctx(),
    ))

    assert result is not None
    assert state.current_user_intent == []


def _continue_work_validation(**_kwargs):
    return ValidationResult(
        ok=False,
        action="continue_work",
        trace_ok=False,
        report_ok=True,
        trace_gaps=["нет fs_write_file для report.md"],
        report_mismatches=[],
        missing_requirements=["нет fs_write_file для report.md"],
        conflicts=[],
        coverage_score=0.0,
        feedback_for_agent="создай report.md",
    )


def _handoff_phantom_validation(**_kwargs):
    return ValidationResult(
        ok=False,
        action="fix_report",
        trace_ok=True,
        report_ok=False,
        trace_gaps=[],
        report_mismatches=["handoff.artifacts: phantom_plan.md отсутствует в trace"],
        missing_requirements=[],
        conflicts=[],
        coverage_score=0.5,
        feedback_for_agent="добавь phantom_plan.md в handoff",
    )


def test_handoff_artifacts_fix_report_lists_paths():
    feedback = build_finish_gate_feedback(_handoff_phantom_validation())
    assert "fix_report" in feedback
    assert "phantom_plan.md" in feedback


def test_run_agent_continue_work_feedback_lists_trace_gaps(monkeypatch):
    monkeypatch.setattr(
        intent_guard_guard,
        "avalidate_final_report_vs_user_intent",
            as_coro(_continue_work_validation),
    )

    def _stub_user_ask(_messages, _state, _ctx):
        return (
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "will write file"}],
                    "id": "m_retry",
                }
            ],
            "rid_stub",
        )

    monkeypatch.setattr(
        processing_module, "aprocess_user_ask", as_coro(_stub_user_ask)
    )

    state = _make_state(max_steps=0)
    seed_state_intent(
        state,
        current="создай report.md",
        log="создай report.md",
    )

    run_coro(arun_agent(
        [_submit_solution_call()],
        state,
        _make_ctx(),
    ))

    tool_output = next(
        m
        for m in state.messages
        if m.get("type") == "function_call_output" and m.get("call_id") == "call_finish"
    )
    payload = json.loads(tool_output["output"])
    assert payload["ok"] is False
    assert payload["status"] == "rejected_by_finish_gate"
    text = payload["message"]
    assert "continue_work" in text.lower()
    assert "fs_write_file" in text
    assert "report.md" in text


def test_main_accept_after_ask_human_trace(monkeypatch):
    def _accept(**kwargs):
        assert kwargs.get("finish_handoff_arguments")
        args = kwargs["finish_handoff_arguments"]
        if isinstance(args, str):
            args = json.loads(args)
        assert args.get("solution_type") == "direct_answer"
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
        intent_guard_guard,
        "avalidate_final_report_vs_user_intent",
            as_coro(_accept),
    )

    state = _make_state(max_steps=0)
    state.messages = _ask_human_scenario_messages(include_pending_submit=False)
    seed_state_intent(
        state,
        current="спроси меня что нибудь",
        log="предыдущий запрос в треде",
    )

    result = run_coro(arun_agent(
        [_submit_solution_call(solution_type="direct_answer")],
        state,
        _make_ctx(),
    ))

    assert result is not None
    assert state.current_user_intent == []
    assert intent_user_ask_texts(state.user_intent_log) == [
        "предыдущий запрос в треде",
        "спроси меня что нибудь",
    ]


def test_run_agent_handoff_phantom_artifact_fix_report(monkeypatch):
    monkeypatch.setattr(
        intent_guard_guard,
        "avalidate_final_report_vs_user_intent",
            as_coro(_handoff_phantom_validation),
    )

    def _stub_user_ask(_messages, _state, _ctx):
        return (
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "fixing handoff"}],
                    "id": "m_retry",
                }
            ],
            "rid_stub",
        )

    monkeypatch.setattr(
        processing_module, "aprocess_user_ask", as_coro(_stub_user_ask)
    )

    state = _make_state(max_steps=0)
    seed_state_intent(
        state,
        current="спроси меня что нибудь",
        log="спроси меня что нибудь",
    )

    run_coro(arun_agent(
        [
            _submit_solution_call(
                handoff={"artifacts": ["phantom_plan.md"]},
            )
        ],
        state,
        _make_ctx(),
    ))

    tool_output = next(
        m
        for m in state.messages
        if m.get("type") == "function_call_output" and m.get("call_id") == "call_finish"
    )
    payload = json.loads(tool_output["output"])
    assert payload["ok"] is False
    assert "fix_report" in payload["message"]
    assert "phantom_plan.md" in payload["message"]


def test_guard_passes_structured_intent_lines_to_validator(monkeypatch):
    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)
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
        intent_guard_guard,
        "avalidate_final_report_vs_user_intent",
            as_coro(_capture),
    )
    state = _make_state()
    seed_state_intent(state, current="open task", log="closed task")
    state.current_user_intent = append_tool_aggregated(
        list(state.current_user_intent),
        "turn_ask_human",
        "topic chosen",
        user_turn_seq=state.user_turn_seq,
        ts="2026-05-30T11:35:08Z",
    )

    guard = FinishGateGuard(ctx=_make_ctx(), state=state)
    run_coro(guard.aevaluate_turn_finish(finish_handoff_arguments={"solution_type": "full_solution"}))

    assert intent_user_ask_texts_from_jsonl(captured["current_user_requests"]) == [
        "open task",
    ]
    assert len(captured["current_user_requests"]) == 2
    assert intent_user_ask_texts_from_jsonl(captured["context_user_messages"]) == [
        "closed task",
    ]


def test_run_agent_reject_continues_turn_with_technical_user_message(monkeypatch):
    """Reject finish gate must not return finish_arguments; inject technical user feedback."""
    validate_calls = {"n": 0}

    def _validate(**_kwargs):
        validate_calls["n"] += 1
        if validate_calls["n"] == 1:
            return _scenario_12_validation()
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
        intent_guard_guard,
        "avalidate_final_report_vs_user_intent",
            as_coro(_validate),
    )
    monkeypatch.setattr(
        processing_module,
        "amaybe_update_intent_scope_from_tool",
        as_coro(lambda **_kwargs: None),
    )

    user_ask_batches: list[list] = []

    def _stub_user_ask(in_messages, _state, _ctx):
        user_ask_batches.append(list(in_messages))
        return (
            [_submit_solution_call(solution_type="direct_answer")],
            "rid_retry",
        )

    monkeypatch.setattr(
        processing_module, "aprocess_user_ask", as_coro(_stub_user_ask)
    )

    state = _make_state(max_steps=10)
    seed_state_intent(
        state,
        current="спроси меня что нибудь",
        log="предыдущий ход",
    )

    result = run_coro(arun_agent([_submit_solution_call()], state, _make_ctx()))

    assert validate_calls["n"] == 2
    assert result is not None
    assert intent_user_ask_texts(state.user_intent_log) == [
        "предыдущий ход",
        "спроси меня что нибудь",
    ]
    assert len(user_ask_batches) == 1
    assert len(user_ask_batches[0]) == 1
    tech_content = str(user_ask_batches[0][0].get("content") or "")
    assert "intent" in tech_content.lower()
    assert "continue working" in tech_content.lower()
    assert "<technical" in tech_content.lower()
    assert any(
        "quality gate" in str(m.get("content") or "").lower()
        or "intent" in str(m.get("content") or "").lower()
        for m in state.messages
        if m.get("role") == "user"
    )
