import importlib
import logging
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import intent_guard.guard as intent_guard_guard
import pytest
import requests

from intent_guard.validation_result import ValidationResult


def teardown_module(_module) -> None:
    # Нельзя выкидывать processing_responses: тесты вида
    # `from processing_responses import f` держат f с устаревшим __globals__;
    # monkeypatch затем меняет другой экземпляр модуля.
    for name in list(sys.modules):
        if name == "tools" or name.startswith("tools."):
            sys.modules.pop(name, None)
    real_tools = importlib.import_module("tools")
    proc = sys.modules.get("processing_responses")
    if proc is not None:
        proc.apply_tool = real_tools.apply_tool
        proc.WrongFunctionArgumentsException = (
            real_tools.WrongFunctionArgumentsException
        )
    cycle_stop_mod = sys.modules.get("message_policy.cycle_stop")
    if cycle_stop_mod is not None:
        cycle_stop_mod.apply_tool = real_tools.apply_tool


processing_module = None
request_policy = None
run_agent = None
State = None


def setup_module(module) -> None:
    global processing_module, request_policy, run_agent, State
    _stub_tools_modules()
    import processing_responses as pm
    import request_backend_policy as rq
    from processing_responses import run_agent as ra
    from states import State as St

    processing_module = pm
    request_policy = rq
    run_agent = ra
    State = St
    pm.apply_tool = sys.modules["tools"].apply_tool


def _stub_tools_modules() -> None:
    for name in list(sys.modules):
        if name == "tools" or name.startswith("tools."):
            sys.modules.pop(name, None)

    tools_pkg = types.ModuleType("tools")

    class _WrongFunctionArgumentsException(Exception):
        def to_llm_feedback(self):
            return str(self)

    def _apply_tool(*_args, **_kwargs):
        tool_name = _kwargs.get("tool_name")
        if tool_name == "submit_solution":
            arguments = _kwargs.get("arguments")
            try:
                payload = json.loads(arguments) if isinstance(arguments, str) else {}
            except Exception:
                payload = {}
            result = {
                "tool": "submit_solution",
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

    tools_pkg.apply_tool = _apply_tool
    tools_pkg.build_tools_data = _build_tools_data
    tools_pkg.WrongFunctionArgumentsException = _WrongFunctionArgumentsException
    root = Path(__file__).resolve().parents[1]
    tools_dir = root / "src" / "tools"
    tools_pkg.__path__ = [str(tools_dir)]
    sys.modules["tools"] = tools_pkg

    import message_policy.cycle_stop as _cycle_stop_mod

    _cycle_stop_mod.apply_tool = tools_pkg.apply_tool


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
        "strict_submit_solution_validation": False,
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
        "name": "submit_solution",
        "arguments": '{"ok": true}',
        "call_id": "call_finish",
        "id": "fc_finish",
    }


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


def _http_error(status_code: int = 500) -> requests.exceptions.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    response._content = b"backend failed"
    return requests.exceptions.HTTPError("backend failed", response=response)


def test_run_agent_calls_pre_backend_hook_before_user_message_backend(monkeypatch):
    order: list[str] = []

    def _fake_process_user_ask(_messages, _state, _ctx):
        order.append("backend")
        return [_submit_solution_call()], "resp_1"

    def _pre_backend_hook(_messages, _mode):
        order.append(f"pre:{_mode}")

    monkeypatch.setattr(processing_module, "process_user_ask", _fake_process_user_ask)

    result = run_agent(
        [{"role": "user", "content": "hello", "id": "m1"}],
        _make_state(),
        _make_ctx(),
        pre_backend_compaction_hook=_pre_backend_hook,
    )

    assert result == '{"ok": true}'
    assert order == ["pre:user_ask", "backend"]


def test_run_agent_calls_pre_backend_hook_before_tool_output_backend(monkeypatch):
    order: list[str] = []

    def _fake_process_tool_outputs(_messages, _state, _ctx):
        order.append("backend")
        return [_submit_solution_call()], "resp_1"

    def _pre_backend_hook(_messages, _mode):
        order.append(f"pre:{_mode}")

    monkeypatch.setattr(
        processing_module, "process_tool_outputs", _fake_process_tool_outputs
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

    assert result == '{"ok": true}'
    assert order == ["pre:function_call_output", "backend"]


def test_run_agent_responses_mode_does_not_call_pre_backend_hook(monkeypatch):
    """Режим responses не должен вызывать pre-backend compaction (см. spec Step 0)."""
    hook_calls: list[str] = []

    def _pre_backend_hook(_messages, mode):
        hook_calls.append(str(mode))

    def _fake_process_user_ask(_messages, _state, _ctx):
        return [_submit_solution_call()], "resp_1"

    monkeypatch.setattr(processing_module, "process_user_ask", _fake_process_user_ask)

    result = run_agent(
        [{"role": "user", "content": "hello", "id": "m1"}],
        _make_state(),
        _make_ctx(backend_api_mode="responses"),
        pre_backend_compaction_hook=_pre_backend_hook,
    )

    assert result == '{"ok": true}'
    assert hook_calls == []


def test_chat_completions_http_error_retries_with_reduced_max_tokens(monkeypatch):
    max_tokens_seen: list[int] = []
    use_retry_seen: list[bool] = []

    def _fake_request_backend(data, *_args, **kwargs):
        max_tokens_seen.append(data["max_tokens"])
        use_retry_seen.append(bool(kwargs.get("use_retry")))
        if len(max_tokens_seen) < 3:
            raise _http_error(500)
        return _chat_response()

    monkeypatch.setattr(
        request_policy,
        "request_backend_chat_completions",
        _fake_request_backend,
    )
    monkeypatch.setattr(request_policy.time, "sleep", lambda _seconds: None)

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
    assert use_retry_seen == [True, True, True]


def test_run_agent_propagates_backend_connection_error(monkeypatch):
    """После исчерпания transport-ретраев не подменяем ошибку no-tool-call bounce."""

    def _failing_process_user_ask(_messages, _state, _ctx):
        raise requests.exceptions.ConnectionError("network is down")

    monkeypatch.setattr(
        processing_module, "process_user_ask", _failing_process_user_ask
    )

    with pytest.raises(requests.exceptions.ConnectionError, match="network is down"):
        run_agent(
            [{"role": "user", "content": "hello", "id": "m1"}],
            _make_state(),
            _make_ctx(),
        )


def test_run_agent_propagates_backend_http_error(monkeypatch):
    def _failing_process_user_ask(_messages, _state, _ctx):
        raise _http_error(503)

    monkeypatch.setattr(
        processing_module, "process_user_ask", _failing_process_user_ask
    )

    with pytest.raises(requests.exceptions.HTTPError):
        run_agent(
            [{"role": "user", "content": "hello", "id": "m1"}],
            _make_state(),
            _make_ctx(),
        )


def test_chat_completions_connection_error_does_not_lower_max_tokens(monkeypatch):
    max_tokens_seen: list[int] = []

    def _fake_request_backend(data, *_args, **_kwargs):
        max_tokens_seen.append(data["max_tokens"])
        raise requests.exceptions.ConnectionError("network is down")

    monkeypatch.setattr(
        request_policy,
        "request_backend_chat_completions",
        _fake_request_backend,
    )
    monkeypatch.setattr(request_policy.time, "sleep", lambda _seconds: None)

    with pytest.raises(requests.exceptions.ConnectionError):
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


def test_submit_solution_clears_current_user_messages_on_success():
    state = _make_state()
    state.current_user_messages = ["актуальный вопрос", "уточнение"]

    result = run_agent(
        [_submit_solution_call()],
        state,
        _make_ctx(strict_submit_solution_validation=False),
    )

    assert result == '{"ok": true}'
    assert state.current_user_messages == []
    synthetic_outputs = [
        m
        for m in state.messages
        if m.get("type") == "function_call_output" and m.get("call_id") == "call_finish"
    ]
    assert len(synthetic_outputs) == 1
    assert "Task outcome submitted." in synthetic_outputs[0]["output"]


def test_submit_solution_validation_failure_fails_open(monkeypatch):
    state = _make_state()
    state.current_user_messages = ["актуальный вопрос"]

    def _raise_validation(*_args, **_kwargs):
        raise RuntimeError("validation backend failure")

    monkeypatch.setattr(
        intent_guard_guard,
        "validate_final_report_vs_user_intent",
        _raise_validation,
    )

    result = run_agent(
        [_submit_solution_call()],
        state,
        _make_ctx(strict_submit_solution_validation=True),
    )

    assert result == '{"ok": true}'
    assert state.current_user_messages == []
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
        _make_ctx(strict_submit_solution_validation=False),
    )

    assert result == '{"ok": true}'
    outputs = [m for m in state.messages if m.get("type") == "function_call_output"]
    by_call = {m.get("call_id"): m for m in outputs}
    assert "call_finish" in by_call
    assert "call_other" in by_call
    assert "Task outcome submitted." in by_call["call_finish"]["output"]
    assert "skipped_on_cycle_stop" in by_call["call_other"]["output"]


def test_submit_solution_validation_receives_context_and_current_scope(monkeypatch):
    state = _make_state()
    state.user_messages_log = ["старый вопрос", "актуальный вопрос", "уточнение"]
    state.current_user_messages = ["актуальный вопрос", "уточнение"]
    captured: dict[str, object] = {}

    def _fake_validation(**kwargs):
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
        "validate_final_report_vs_user_intent",
        _fake_validation,
    )

    result = run_agent(
        [_submit_solution_call()],
        state,
        _make_ctx(strict_submit_solution_validation=True),
    )

    assert result == '{"ok": true}'
    assert captured["current_user_requests"] == ["актуальный вопрос", "уточнение"]
    assert captured["context_user_messages"] == ["старый вопрос"]


def test_finish_gate_builds_trace_digest_from_state_messages(monkeypatch):
    from unittest.mock import patch

    from intent_guard.trace_digest import FinishGateTraceDigest

    state = _make_state()
    state.messages = [
        {"role": "user", "content": "сделай X"},
        {
            "type": "function_call",
            "call_id": "t1",
            "name": "read_file",
            "arguments": {"path": "x.py"},
        },
    ]
    state.current_user_messages = ["сделай X"]
    state.user_messages_log = ["сделай X"]
    digest = FinishGateTraceDigest(
        trace_digest_text='<message n="1">\n[user] сделай X\n</message>',
        digest_chars=30,
    )
    validator_json = (
        '{"action":"accept","trace_ok":true,"report_ok":true,'
        '"trace_gaps":[],"report_mismatches":[]}'
    )

    with patch(
        "intent_guard.final_report_consistency.build_finish_gate_trace_digest",
        return_value=digest,
    ) as mock_build:
        with patch(
            "intent_guard.final_report_consistency.request_chat_completion_text",
            return_value=validator_json,
        ):
            run_agent(
                [_submit_solution_call()],
                state,
                _make_ctx(strict_submit_solution_validation=True),
            )

    mock_build.assert_called_once()
    call_state = mock_build.call_args.kwargs.get("state") or mock_build.call_args[0][0]
    assert call_state is state


def test_finish_gate_rejection_still_appends_cycle_stop_tool_outputs(monkeypatch):
    """При отклонении quality/finish gate pending tool_calls должны закрываться
    синтетическими function_call_output (как при успехе), иначе chat/completions
    получает невалидную цепочку assistant.tool_calls → user."""
    recorded: list[list[str | None]] = []

    def _reject_validation(**_kwargs):
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

    real_append = processing_module.append_cycle_stop_tool_outputs_for_batch

    def _spy_append_cycle_stop(**kwargs):
        tool_calls = kwargs.get("tool_calls") or []
        recorded.append([t.get("call_id") for t in tool_calls])
        return real_append(**kwargs)

    monkeypatch.setattr(
        intent_guard_guard,
        "validate_final_report_vs_user_intent",
        _reject_validation,
    )
    monkeypatch.setattr(
        processing_module,
        "append_cycle_stop_tool_outputs_for_batch",
        _spy_append_cycle_stop,
    )

    def _stub_user_ask(_messages, _state, _ctx):
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

    monkeypatch.setattr(processing_module, "process_user_ask", _stub_user_ask)

    state = _make_state(max_steps=0)
    state.user_messages_log = ["user question"]
    state.current_user_messages = ["user question"]

    run_agent(
        [_submit_solution_call()],
        state,
        _make_ctx(strict_submit_solution_validation=True),
    )

    assert recorded == [["call_finish"]]
    assert any(
        m.get("type") == "function_call_output" and m.get("call_id") == "call_finish"
        for m in state.messages
    )
