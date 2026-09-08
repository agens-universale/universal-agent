import importlib
import logging
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from gc_core.intent_guard import guard as intent_guard_guard
import pytest
import httpx

from gc_core.intent_guard.validation_result import ValidationResult
from gc_testkit.intent_fixtures import (
    intent_user_ask_texts,
    intent_user_ask_texts_from_jsonl,
    seed_state_intent,
)
from gc_testkit.paths import module_dir


async def _anoop(*_a, **_k):
    """Пауза политики стала кооперативной (asyncio.sleep, Задача 11)."""
    return None


def teardown_module(_module) -> None:
    # Нельзя выкидывать processing_responses: тесты вида
    # `from processing_responses import f` держат f с устаревшим __globals__;
    # monkeypatch затем меняет другой экземпляр модуля.
    for name in list(sys.modules):
        if name == "gc_core.tools" or name.startswith("gc_core.tools."):
            sys.modules.pop(name, None)
    real_tools = importlib.import_module("gc_core.tools")
    proc = sys.modules.get("gc_core.processing_responses")
    if proc is not None:
        proc.aapply_tool = real_tools.aapply_tool
        proc.WrongFunctionArgumentsException = (
            real_tools.WrongFunctionArgumentsException
        )
    cycle_stop_mod = sys.modules.get("gc_core.message_policy.cycle_stop")
    if cycle_stop_mod is not None:
        cycle_stop_mod.aapply_tool = real_tools.aapply_tool


processing_module = None
request_policy = None
run_agent = None
State = None


def setup_module(module) -> None:
    global processing_module, request_policy, run_agent, State
    _stub_tools_modules()
    from gc_core import processing_responses as pm
    from gc_core import request_backend_policy as rq
    from gc_core.processing_responses import run_agent as ra
    from gc_core.states import State as St

    processing_module = pm
    request_policy = rq
    run_agent = ra
    State = St
    pm.aapply_tool = sys.modules["gc_core.tools"].aapply_tool


def _stub_tools_modules() -> None:
    for name in list(sys.modules):
        if name == "gc_core.tools" or name.startswith("gc_core.tools."):
            sys.modules.pop(name, None)

    tools_pkg = types.ModuleType("gc_core.tools")

    class _WrongFunctionArgumentsException(Exception):
        def to_llm_feedback(self):
            return str(self)

    async def _apply_tool(*_args, **_kwargs):
        tool_name = _kwargs.get("tool_name")
        if tool_name == "turn_finish":
            arguments = _kwargs.get("arguments")
            try:
                payload = json.loads(arguments) if isinstance(arguments, str) else {}
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
        raise AssertionError("apply_tool should not be called in this test")

    def _build_tools_data(_config_dict):
        return {}, [], []

    tools_pkg.aapply_tool = _apply_tool
    tools_pkg.build_tools_data = _build_tools_data
    tools_pkg.WrongFunctionArgumentsException = _WrongFunctionArgumentsException
    # Цикл импортирует ещё и распознаватель «неизвестный тул» — заглушка обязана
    # его отдавать, иначе импорт processing_responses падает на пустом месте.
    tools_pkg.is_unknown_tool_output = lambda *_a, **_k: False
    # Имена приехали с develop: processing_responses импортирует их из пакета,
    # и заглушка обязана их отдавать, иначе импорт модуля падает целиком.
    tools_pkg.is_internal_tool_error_output = lambda *_a, **_k: False
    tools_pkg.tool_arguments_valid = lambda *_a, **_k: True
    tools_dir = module_dir("tools")
    tools_pkg.__path__ = [str(tools_dir)]
    sys.modules["gc_core.tools"] = tools_pkg

    from gc_core.message_policy import cycle_stop as _cycle_stop_mod

    _cycle_stop_mod.aapply_tool = tools_pkg.aapply_tool


def _make_state(*, max_steps: int = 5):
    return State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=max_steps,
        system_prompt_id="v1",
        use_server_subagents=False,
    )


def _make_ctx(**overrides):
    cfg = {
        "backend_api_mode": "chat_completions",
        "completions_url": "http://backend.local/v1/chat/completions",
        "chat_completions_max_tokens": 100,
        "chat_completions_min_response_tokens": 30,
        "use_ssl": True,
        "use_retry": False,
        "console_trace": False,
        "no_tool_call_max_count": 5,
        "strict_turn_finish_validation": False,
    }
    cfg.update(overrides)
    return SimpleNamespace(
        config_dict=cfg,
        logger=logging.getLogger("test_context_baseline"),
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


def _expected_finish_return() -> str:
    """run_agent возвращает finish_arguments (JSON arguments submit_solution)."""
    return _submit_solution_call()["arguments"]


def _generic_tool_call(*, call_id: str = "call_other", name: str = "memory_files"):
    return {
        "type": "function_call",
        "name": name,
        "arguments": '{"action":"list"}',
        "call_id": call_id,
        "id": "fc_other",
    }


def _chat_response(content: str = "ok"):
    return {
        "id": "chatcmpl_test",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {},
    }


def _http_error(status_code: int = 500) -> httpx.HTTPStatusError:
    """Ошибка HTTP в форме httpx: requests ушёл из зависимостей ядра."""
    request = httpx.Request("POST", "http://backend.invalid/v1")
    response = httpx.Response(status_code, text="backend failed", request=request)
    return httpx.HTTPStatusError("backend failed", request=request, response=response)


def test_run_agent_calls_pre_backend_hook_before_user_message_backend(monkeypatch):
    order: list[str] = []

    async def _fake_process_user_ask(_messages, _state, _ctx):
        order.append("backend")
        return [_submit_solution_call()], "resp_1"

    def _pre_backend_hook(_messages, _mode):
        order.append(f"pre:{_mode}")

    monkeypatch.setattr(processing_module, "aprocess_user_ask", _fake_process_user_ask)

    result = run_agent(
        [{"role": "user", "content": "hello", "id": "m1"}],
        _make_state(),
        _make_ctx(),
        pre_backend_compaction_hook=_pre_backend_hook,
    )

    assert result == _expected_finish_return()
    assert order == ["pre:user_ask", "backend"]


def test_run_agent_calls_pre_backend_hook_before_tool_output_backend(monkeypatch):
    order: list[str] = []

    async def _fake_process_tool_outputs(_messages, _state, _ctx):
        order.append("backend")
        return [_submit_solution_call()], "resp_1"

    def _pre_backend_hook(_messages, _mode):
        order.append(f"pre:{_mode}")

    monkeypatch.setattr(
        processing_module, "aprocess_tool_outputs", _fake_process_tool_outputs
    )

    result = run_agent(
        [
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "tool result",
                "id": "fco_1",
            }
        ],
        _make_state(),
        _make_ctx(),
        pre_backend_compaction_hook=_pre_backend_hook,
    )

    assert result == _expected_finish_return()
    assert order == ["pre:function_call_output", "backend"]


def test_run_agent_responses_mode_does_not_call_pre_backend_hook(monkeypatch):
    """Режим responses не должен вызывать pre-backend compaction (см. spec Step 0)."""
    hook_calls: list[str] = []

    def _pre_backend_hook(_messages, mode):
        hook_calls.append(str(mode))

    async def _fake_process_user_ask(_messages, _state, _ctx):
        return [_submit_solution_call()], "resp_1"

    monkeypatch.setattr(processing_module, "aprocess_user_ask", _fake_process_user_ask)

    result = run_agent(
        [{"role": "user", "content": "hello", "id": "m1"}],
        _make_state(),
        _make_ctx(backend_api_mode="responses"),
        pre_backend_compaction_hook=_pre_backend_hook,
    )

    assert result == _expected_finish_return()
    assert hook_calls == []


def test_chat_completions_http_error_retries_with_reduced_max_tokens(monkeypatch):
    max_tokens_seen: list[int] = []
    use_retry_seen: list[bool] = []

    async def _fake_request_backend(data, *_args, **kwargs):
        max_tokens_seen.append(data["max_tokens"])
        use_retry_seen.append(bool(kwargs.get("use_retry")))
        if len(max_tokens_seen) < 3:
            raise _http_error(500)
        return _chat_response()

    monkeypatch.setattr(
        request_policy,
        "arequest_backend_chat_completions",
        _fake_request_backend,
    )
    monkeypatch.setattr(request_policy.asyncio, "sleep", _anoop)

    response, payload = request_policy.request_chat_completions_with_policy(
        payload_factory=lambda max_tokens: {"max_tokens": max_tokens},
        initial_max_tokens=100,
        min_response_tokens=30,
        headers={},
        url="http://backend.local/v1/chat/completions",
        verify=True,
        use_retry=True,
        logger=logging.getLogger("test_context_baseline"),
    )

    assert response["id"] == "chatcmpl_test"
    assert payload["max_tokens"] == 30
    assert max_tokens_seen == [100, 50, 30]
    assert use_retry_seen == [True, False, False]


def test_run_agent_propagates_backend_connection_error(monkeypatch):
    """После исчерпания transport-ретраев не подменяем ошибку no-tool-call bounce."""

    async def _failing_process_user_ask(_messages, _state, _ctx):
        raise httpx.ConnectError("network is down")

    monkeypatch.setattr(
        processing_module, "aprocess_user_ask", _failing_process_user_ask
    )

    with pytest.raises(httpx.ConnectError, match="network is down"):
        run_agent(
            [{"role": "user", "content": "hello", "id": "m1"}],
            _make_state(),
            _make_ctx(),
        )


def test_run_agent_propagates_backend_http_error(monkeypatch):
    async def _failing_process_user_ask(_messages, _state, _ctx):
        raise _http_error(503)

    monkeypatch.setattr(
        processing_module, "aprocess_user_ask", _failing_process_user_ask
    )

    with pytest.raises(httpx.HTTPStatusError):
        run_agent(
            [{"role": "user", "content": "hello", "id": "m1"}],
            _make_state(),
            _make_ctx(),
        )


def test_chat_completions_connection_error_does_not_lower_max_tokens(monkeypatch):
    max_tokens_seen: list[int] = []

    async def _fake_request_backend(data, *_args, **_kwargs):
        max_tokens_seen.append(data["max_tokens"])
        raise httpx.ConnectError("network is down")

    monkeypatch.setattr(
        request_policy,
        "arequest_backend_chat_completions",
        _fake_request_backend,
    )
    monkeypatch.setattr(request_policy.asyncio, "sleep", _anoop)

    with pytest.raises(httpx.ConnectError):
        request_policy.request_chat_completions_with_policy(
            payload_factory=lambda max_tokens: {"max_tokens": max_tokens},
            initial_max_tokens=100,
            min_response_tokens=30,
            headers={},
            url="http://backend.local/v1/chat/completions",
            verify=True,
            use_retry=False,
            logger=logging.getLogger("test_context_baseline"),
        )

    assert max_tokens_seen == [100]


def test_turn_finish_commits_current_user_intent_on_success():
    state = _make_state()
    seed_state_intent(state, current=["актуальный вопрос", "уточнение"])

    result = run_agent(
        [_submit_solution_call()],
        state,
        _make_ctx(strict_turn_finish_validation=False),
    )

    assert result == _expected_finish_return()
    assert state.current_user_intent == []
    assert intent_user_ask_texts(state.user_intent_log) == [
        "актуальный вопрос",
        "уточнение",
    ]
    synthetic_outputs = [
        m
        for m in state.messages
        if m.get("type") == "function_call_output" and m.get("call_id") == "call_finish"
    ]
    assert len(synthetic_outputs) == 1
    output_payload = json.loads(synthetic_outputs[0]["output"])
    assert output_payload["message"] == "Task outcome submitted."
    assert output_payload["task_report"] == "Сделано по запросу пользователя"
    assert "Dialog turn finished" not in synthetic_outputs[0]["output"]


def test_turn_finish_validation_failure_fails_open(monkeypatch):
    state = _make_state()
    seed_state_intent(state, current=["актуальный вопрос"])

    async def _raise_validation(*_args, **_kwargs):
        raise RuntimeError("validation backend failure")

    monkeypatch.setattr(
        intent_guard_guard,
        "avalidate_final_report_vs_user_intent",
        _raise_validation,
    )

    result = run_agent(
        [_submit_solution_call()],
        state,
        _make_ctx(strict_turn_finish_validation=True),
    )

    assert result == _expected_finish_return()
    assert state.current_user_intent == []
    assert intent_user_ask_texts(state.user_intent_log) == ["актуальный вопрос"]
    synthetic_outputs = [
        m
        for m in state.messages
        if m.get("type") == "function_call_output" and m.get("call_id") == "call_finish"
    ]
    assert len(synthetic_outputs) == 1


def test_cycle_stop_generates_outputs_for_all_pending_tool_calls():
    state = _make_state()
    result = run_agent(
        [_submit_solution_call(), _generic_tool_call()],
        state,
        _make_ctx(strict_turn_finish_validation=False),
    )

    assert result == _expected_finish_return()
    outputs = [m for m in state.messages if m.get("type") == "function_call_output"]
    by_call = {m.get("call_id"): m for m in outputs}
    assert "call_finish" in by_call
    assert "call_other" in by_call
    finish_payload = json.loads(by_call["call_finish"]["output"])
    assert finish_payload["task_report"] == "Сделано по запросу пользователя"
    assert "Dialog turn finished" not in by_call["call_finish"]["output"]
    assert "skipped_on_cycle_stop" in by_call["call_other"]["output"]


def test_turn_finish_validation_receives_context_and_current_scope(monkeypatch):
    state = _make_state()
    seed_state_intent(
        state,
        log=["старый вопрос"],
        current=["актуальный вопрос", "уточнение"],
    )
    captured: dict[str, object] = {}

    async def _fake_validation(**kwargs):
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
        _fake_validation,
    )

    result = run_agent(
        [_submit_solution_call()],
        state,
        _make_ctx(strict_turn_finish_validation=True),
    )

    assert result == _expected_finish_return()
    assert intent_user_ask_texts_from_jsonl(
        captured["current_user_requests"]
    ) == ["актуальный вопрос", "уточнение"]
    assert intent_user_ask_texts_from_jsonl(
        captured["context_user_messages"]
    ) == ["старый вопрос"]


def test_finish_gate_builds_trace_digest_from_state_messages(monkeypatch):
    from unittest.mock import patch

    from gc_core.intent_guard.trace_digest import FinishGateTraceDigest

    state = _make_state()
    state.messages = [
        {"role": "user", "content": "сделай X"},
        {
            "type": "function_call",
            "call_id": "t1",
            "name": "fs_read_file",
            "arguments": {"path": "x.py"},
        },
    ]
    seed_state_intent(state, log=["сделай X"], current=["сделай X"])
    digest = FinishGateTraceDigest(
        trace_digest_text='<message n="1">\n[user] сделай X\n</message>',
        digest_chars=30,
    )
    validator_json = (
        '{"action":"accept","trace_ok":true,"report_ok":true,'
        '"is_question":false,"trace_gaps":[],"report_mismatches":[]}'
    )

    with patch(
        "gc_core.intent_guard.final_report_consistency.build_finish_gate_trace_digest",
        return_value=digest,
    ) as mock_build:
        with patch(
            "gc_core.summarize.llm.arequest_chat_completion_text",
            return_value=validator_json,
        ):
            run_agent(
                [_submit_solution_call()],
                state,
                _make_ctx(strict_turn_finish_validation=True),
            )

    mock_build.assert_called_once()
    call_state = mock_build.call_args.kwargs.get("state") or mock_build.call_args[0][0]
    assert call_state is state


def test_finish_gate_rejection_still_appends_cycle_stop_tool_outputs(monkeypatch):
    """При отклонении quality/finish gate pending tool_calls должны закрываться
    синтетическими function_call_output (как при успехе), иначе chat/completions
    получает невалидную цепочку assistant.tool_calls → user."""
    recorded: list[list[str | None]] = []

    async def _reject_validation(**_kwargs):
        return ValidationResult(
            ok=False,
            action="fix_report_and_continue",
            trace_ok=False,
            report_ok=False,
            trace_gaps=["m"],
            report_mismatches=["c"],
            missing_requirements=["m"],
            conflicts=["c"],
            coverage_score=0.0,
            feedback_for_agent="improve the answer",
        )

    real_append = processing_module.aappend_cycle_stop_tool_outputs_for_batch

    def _spy_append_cycle_stop(**kwargs):
        tool_calls = kwargs.get("tool_calls") or []
        recorded.append([t.get("call_id") for t in tool_calls])
        return real_append(**kwargs)

    monkeypatch.setattr(
        intent_guard_guard,
        "avalidate_final_report_vs_user_intent",
        _reject_validation,
    )
    monkeypatch.setattr(
        processing_module,
        "aappend_cycle_stop_tool_outputs_for_batch",
        _spy_append_cycle_stop,
    )

    async def _stub_user_ask(_messages, _state, _ctx):
        return (
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                    "id": "m_after_gate",
                }
            ],
            "rid_stub",
        )

    monkeypatch.setattr(processing_module, "aprocess_user_ask", _stub_user_ask)

    state = _make_state(max_steps=0)
    seed_state_intent(state, log=["user question"], current=["user question"])

    run_agent(
        [_submit_solution_call()],
        state,
        _make_ctx(strict_turn_finish_validation=True),
    )

    assert recorded == [["call_finish"]]
    assert any(
        m.get("type") == "function_call_output" and m.get("call_id") == "call_finish"
        for m in state.messages
    )
