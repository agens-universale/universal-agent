import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests


def _stub_tools_modules() -> None:
    for name in list(sys.modules):
        if name == "tools" or name.startswith("tools."):
            sys.modules.pop(name, None)

    tools_pkg = types.ModuleType("tools")

    class _WrongFunctionArgumentsException(Exception):
        def to_llm_feedback(self):
            return str(self)

    def _apply_tool(*_args, **_kwargs):
        raise AssertionError("apply_tool should not be called in this test")

    tools_pkg.apply_tool = _apply_tool
    tools_pkg.WrongFunctionArgumentsException = _WrongFunctionArgumentsException
    root = Path(__file__).resolve().parents[1]
    tools_dir = root / "src" / "tools"
    tools_pkg.__path__ = [str(tools_dir)]
    sys.modules["tools"] = tools_pkg


_stub_tools_modules()

import processing_responses as processing_module  # noqa: E402
import request_backend_policy as request_policy  # noqa: E402
from processing_responses import run_agent  # noqa: E402
from states import State  # noqa: E402


def _make_state(*, max_steps: int = 5) -> State:
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
    assert use_retry_seen == [True, False, False]


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
