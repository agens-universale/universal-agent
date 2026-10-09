"""Интеграция turn-end compaction с ``run_agent`` (accept/reject, порог, payload)."""

from __future__ import annotations

import json
import logging

import pytest
from gc_core import processing_responses as pr
from gc_core import run_summary_coordinator as rsc
from gc_core.intent_guard import guard as intent_guard_guard
from gc_core.intent_guard.validation_result import ValidationResult
from gc_core.message_policy.user_message_policy import (
    is_dialog_summary_user_message_text,
)
from gc_core.run_summary_coordinator import RunSummaryCoordinator
from gc_core.states import State
from gc_core.summarize.compaction import apply_summary_state_patch
from gc_testkit import run_coro
from gc_testkit.helpers.summary_sample import valid_summary_xml_for_tests
from gc_testkit.intent_fixtures import intent_user_ask_texts, seed_state_intent
from gc_testkit.settings import fake_ctx
from helpers.turn_finish_apply import stub_turn_finish_apply


def _async_noop(fn):
    """Корутинная заглушка: ядро зовёт диспетчер и хуки только async-именами.

    Синхронную подмену цикл получил бы как готовый результат вместо awaitable
    и упал бы на ``await`` — или, хуже, тихо не исполнил бы её вовсе.
    """

    async def _wrapped(*args, **kwargs):
        return fn(*args, **kwargs)

    return _wrapped


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
    summary_overrides = overrides.pop("summary_params", {})
    summary_params = {
        "chat_completions_summary_trigger_chars": 50000,
        "chat_completions_summary_turn_end_trigger_chars": 10,
        "incremental_batch_chars": 100,
        "max_accumulated_summary_chars": 20000,
        "last_dialog_steps_count": 5,
        "last_dialog_step_message_max_chars": 5000,
    }
    summary_params.update(summary_overrides)
    cfg = {
        "backend.api_mode": "chat_completions",
        "backend.completions_url": "http://backend.local/v1/chat/completions",
        "logging.console_trace": False,
        "agent.no_tool_call_max_count": 5,
        "agent.strict_turn_finish_validation": True,
        "summary": summary_params,
    }
    cfg.update(overrides)
    return fake_ctx(
        cfg,
        logger=logging.getLogger("test_turn_end_compaction"),
        auth_connector=None,
    )


def _submit_solution_call():
    return {
        "type": "function_call",
        "name": "turn_finish",
        "arguments": json.dumps(
            {
                "solution_type": "full_solution",
                "task_report": "Сделано по запросу пользователя",
                "handoff": "{}",
            },
            ensure_ascii=False,
        ),
        "call_id": "call_finish",
        "id": "fc_finish",
    }


def _expected_finish_arguments() -> str:
    return _submit_solution_call()["arguments"]


async def _accept_validation(**_kwargs):
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


async def _reject_validation(**_kwargs):
    return ValidationResult(
        ok=False,
        action="fix_report_and_continue",
        trace_ok=False,
        report_ok=False,
        trace_gaps=["gap"],
        report_mismatches=["mismatch"],
        missing_requirements=["req"],
        conflicts=["conflict"],
        coverage_score=0.0,
        feedback_for_agent="improve the answer",
    )


def _coordinator(
    state: State, *, turn_end_trigger_chars: int = 1
) -> RunSummaryCoordinator:
    ctx = _make_ctx()
    return RunSummaryCoordinator(
        ctx=ctx,
        state=state,
        thread_session=None,
        summary_trigger_chars=50000,
        turn_end_trigger_chars=turn_end_trigger_chars,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
    )


def _seed_long_history(state: State, *, user_chars: int = 80) -> None:
    state.messages = [
        {"role": "system", "content": "sys", "id": "m_sys"},
        {"role": "user", "content": "x" * user_chars, "id": "m_u"},
        {"role": "assistant", "content": "y" * 40, "id": "m_a"},
    ]


@pytest.fixture(autouse=True)
def _accept_finish_gate(monkeypatch):
    monkeypatch.setattr(pr, "aapply_tool", stub_turn_finish_apply)
    monkeypatch.setattr(
        intent_guard_guard,
        "avalidate_final_report_vs_user_intent",
        _accept_validation,
    )


def test_turn_end_below_threshold_does_not_compact(monkeypatch):
    state = _make_state()
    _seed_long_history(state, user_chars=5)
    seed_state_intent(state, current=["вопрос"])
    recorded: list[str] = []

    def record(**kwargs):
        recorded.append(str(kwargs.get("trigger_reason")))

    monkeypatch.setattr(rsc, "arun_summary_compaction", record)
    coord = _coordinator(state, turn_end_trigger_chars=10_000)

    result = run_coro(
        pr.arun_agent(
            [_submit_solution_call()],
            state,
            _make_ctx(),
            turn_end_compaction_hook=coord.aturn_end_compact_if_needed,
        )
    )

    assert result == _expected_finish_arguments()
    assert recorded == []
    assert (
        len(
            [
                m
                for m in state.messages
                if is_dialog_summary_user_message_text(
                    str(m.get("content") or "")
                )
            ]
        )
        == 0
    )


def test_turn_end_above_threshold_replaces_trace(monkeypatch):
    state = _make_state()
    _seed_long_history(state, user_chars=80)
    seed_state_intent(state, current=["вопрос"])
    recorded: list[str] = []

    def compact(**kwargs):
        recorded.append(str(kwargs.get("trigger_reason")))
        st = kwargs["state"]
        sys_m = next(
            (
                m
                for m in st.messages
                if str(m.get("role", "")).lower() == "system"
            ),
            None,
        )
        st.messages = (
            [
                sys_m,
                {
                    "role": "user",
                    "content": '<technical type="dialog_summary">compact</technical>',
                    "id": "m_sum",
                },
            ]
            if sys_m
            else [
                {
                    "role": "user",
                    "content": '<technical type="dialog_summary">compact</technical>',
                    "id": "m_sum",
                }
            ]
        )

    monkeypatch.setattr(rsc, "arun_summary_compaction", compact)
    coord = _coordinator(state, turn_end_trigger_chars=1)

    result = run_coro(
        pr.arun_agent(
            [_submit_solution_call()],
            state,
            _make_ctx(),
            turn_end_compaction_hook=coord.aturn_end_compact_if_needed,
        )
    )

    assert result == _expected_finish_arguments()
    assert recorded == ["turn_end"]
    assert len(state.messages) == 2
    assert is_dialog_summary_user_message_text(
        str(state.messages[-1].get("content") or "")
    )


def test_turn_end_reject_skips_compaction(monkeypatch):
    monkeypatch.setattr(
        intent_guard_guard,
        "avalidate_final_report_vs_user_intent",
        _reject_validation,
    )
    state = _make_state(max_steps=0)
    _seed_long_history(state, user_chars=80)
    seed_state_intent(state, log=["closed"], current=["open"])
    recorded: list[str] = []

    monkeypatch.setattr(
        rsc,
        "arun_summary_compaction",
        lambda **kwargs: recorded.append(str(kwargs.get("trigger_reason"))),
    )

    async def _stub_user_ask(_messages, _state, _ctx):
        return (
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "retry"}],
                    "id": "m_after",
                }
            ],
            "rid",
        )

    monkeypatch.setattr(pr, "aprocess_user_ask", _stub_user_ask)
    coord = _coordinator(state, turn_end_trigger_chars=1)

    run_coro(
        pr.arun_agent(
            [_submit_solution_call()],
            state,
            _make_ctx(),
            turn_end_compaction_hook=coord.aturn_end_compact_if_needed,
        )
    )

    assert recorded == []
    assert intent_user_ask_texts(state.current_user_intent) == ["open"]


def test_turn_end_runs_on_fail_open_when_volume_high(monkeypatch):
    async def _raise_validation(*_args, **_kwargs):
        raise RuntimeError("validation backend failure")

    monkeypatch.setattr(
        intent_guard_guard,
        "avalidate_final_report_vs_user_intent",
        _raise_validation,
    )
    state = _make_state()
    _seed_long_history(state, user_chars=80)
    seed_state_intent(state, current=["вопрос"])
    recorded: list[str] = []

    monkeypatch.setattr(
        rsc,
        "arun_summary_compaction",
        lambda **kwargs: recorded.append(str(kwargs.get("trigger_reason"))),
    )
    coord = _coordinator(state, turn_end_trigger_chars=1)

    result = run_coro(
        pr.arun_agent(
            [_submit_solution_call()],
            state,
            _make_ctx(**{"agent.strict_turn_finish_validation": True}),
            turn_end_compaction_hook=coord.aturn_end_compact_if_needed,
        )
    )

    assert result == _expected_finish_arguments()
    assert recorded == ["turn_end"]
    assert state.current_user_intent == []
    assert intent_user_ask_texts(state.user_intent_log) == ["вопрос"]


def test_turn_end_flush_moves_intent_before_compact(monkeypatch):
    state = _make_state()
    _seed_long_history(state, user_chars=80)
    seed_state_intent(state, current=["вопрос", "уточнение"])

    def compact(**kwargs):
        st = kwargs["state"]
        assert st.current_user_intent == []
        assert intent_user_ask_texts(st.user_intent_log) == [
            "вопрос",
            "уточнение",
        ]
        sys_m = next(
            (
                m
                for m in st.messages
                if str(m.get("role", "")).lower() == "system"
            ),
            None,
        )
        st.messages = (
            [sys_m, {"role": "user", "content": "summary", "id": "s"}]
            if sys_m
            else []
        )

    monkeypatch.setattr(rsc, "arun_summary_compaction", compact)
    coord = _coordinator(state, turn_end_trigger_chars=1)

    run_coro(
        pr.arun_agent(
            [_submit_solution_call()],
            state,
            _make_ctx(),
            turn_end_compaction_hook=coord.aturn_end_compact_if_needed,
        )
    )


def test_turn_end_compaction_failure_still_returns_submit_payload(monkeypatch):
    state = _make_state()
    _seed_long_history(state, user_chars=80)
    seed_state_intent(state, current=["вопрос"])
    before_msgs = list(state.messages)

    def boom(**_kwargs):
        raise RuntimeError("summary backend down")

    monkeypatch.setattr(rsc, "arun_summary_compaction", boom)
    coord = _coordinator(state, turn_end_trigger_chars=1)

    result = run_coro(
        pr.arun_agent(
            [_submit_solution_call()],
            state,
            _make_ctx(),
            turn_end_compaction_hook=coord.aturn_end_compact_if_needed,
        )
    )

    assert result == _expected_finish_arguments()
    assert state.current_user_intent == []
    assert intent_user_ask_texts(state.user_intent_log) == ["вопрос"]
    assert len(state.messages) >= len(before_msgs)
    assert not any(
        is_dialog_summary_user_message_text(str(m.get("content") or ""))
        for m in state.messages
    )


def test_turn_end_working_memory_in_summary_message(monkeypatch):
    state = _make_state()
    _seed_long_history(state, user_chars=80)
    seed_state_intent(state, current=["вопрос"])
    if not hasattr(state, "shared") or state.shared is None:
        from gc_core.states import SharedStateData

        state.shared = SharedStateData()

    def compact(**kwargs):
        st = kwargs["state"]
        apply_summary_state_patch(
            state=st,
            raw_summary=valid_summary_xml_for_tests(seed="wm-test"),
            last_dialog_steps="step tail",
            memory_append_deltas={"knowledge": "note from patch"},
            summarized_messages_count=2,
        )

    monkeypatch.setattr(rsc, "arun_summary_compaction", compact)
    coord = _coordinator(state, turn_end_trigger_chars=1)

    run_coro(
        pr.arun_agent(
            [_submit_solution_call()],
            state,
            _make_ctx(),
            turn_end_compaction_hook=coord.aturn_end_compact_if_needed,
        )
    )

    assert state.shared.working_memory.get("knowledge") == "note from patch"
    summary_msgs = [
        m
        for m in state.messages
        if is_dialog_summary_user_message_text(str(m.get("content") or ""))
    ]
    assert len(summary_msgs) == 1
    body = str(summary_msgs[0].get("content") or "")
    assert "note from patch" in body


def _agent_executor_config() -> dict:
    return {
        "agent.verbose": False,
        "agent.model": "CodeChat",
        "agent.max_steps": 5,
        "agent.system_prompt_id": "v1",
        "agent.use_server_subagents": False,
        "paths.agent_md_path": "",
        "agent.limit_project_metadata": True,
        "backend.api_mode": "chat_completions",
        "logging.console_trace": False,
        "agent.no_tool_call_max_count": 5,
        "agent.strict_turn_finish_validation": True,
        "summary": {
            "chat_completions_summary_trigger_chars": 0,
            "chat_completions_summary_turn_end_trigger_chars": 1,
            "incremental_batch_chars": 100,
            "max_accumulated_summary_chars": 20000,
            "last_dialog_steps_count": 5,
            "last_dialog_step_message_max_chars": 5000,
        },
    }


def test_agent_executor_fail_open_turn_end_compaction(monkeypatch):
    """Полный wiring: AgentExecutor → run_agent → fail_open → turn-end hook."""
    from gc_core import agent_executor as ae

    class _SkillsRegistryStub:
        async def list_skills(self):
            return []

    async def _raise_validation(*_args, **_kwargs):
        raise RuntimeError("validation backend failure")

    async def _fake_process_user_ask(_messages, _state, _ctx):
        return ([_submit_solution_call()], "resp_stub")

    recorded: list[str] = []
    monkeypatch.setattr(
        intent_guard_guard,
        "avalidate_final_report_vs_user_intent",
        _raise_validation,
    )
    monkeypatch.setattr(pr, "aprocess_user_ask", _fake_process_user_ask)
    # Строковые пути: после reload в test_summary_triggers импорт ``rsc`` в этом файле
    # может указывать на старый модуль, а AgentExecutor — на актуальный.
    monkeypatch.setattr(
        "gc_core.run_summary_coordinator.exceeded_summary_triggers",
        lambda **_k: True,
    )
    monkeypatch.setattr(
        "gc_core.run_summary_coordinator.arun_summary_compaction",
        lambda **kwargs: recorded.append(str(kwargs.get("trigger_reason"))),
    )

    ctx = fake_ctx(
        _agent_executor_config(),
        logger=logging.getLogger("test_agent_executor_turn_end"),
        tools_spec=[],
        tools_description=[],
        skills_registry=_SkillsRegistryStub(),
    )
    executor = ae.AgentExecutor(ctx)
    result = run_coro(executor.arun(["вопрос"], system="cli", verbose=False))

    assert result == _expected_finish_arguments()
    assert recorded == ["turn_end"]
