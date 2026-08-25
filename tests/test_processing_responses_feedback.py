import importlib
import sys
import types
from pathlib import Path

from message_policy.user_message_policy import TECHNICAL_TYPE_ERROR_MESSAGE
from message_policy.user_message_policy import build_invalid_tool_call_feedback_message
from states import State
from types import SimpleNamespace
from utils.convertations import sanitize_messages_for_chat_completions


def teardown_module(_module) -> None:
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
    tools_pkg.is_unknown_tool_output = lambda _output: False
    root = Path(__file__).resolve().parents[1]
    tools_dir = root / "src" / "tools"
    tools_pkg.__path__ = [str(tools_dir)]
    sys.modules["tools"] = tools_pkg


processing_module = None


def setup_module(_module) -> None:
    global processing_module
    _stub_tools_modules()
    import processing_responses as pm

    processing_module = pm
    pm.apply_tool = sys.modules["tools"].apply_tool


def test_make_technical_feedback_message_wraps_generic_feedback():
    msg = processing_module._make_technical_feedback_user_message(
        "There is no tool call in Agent response. Please send appropriate tool call"
    )
    assert msg["role"] == "user"
    assert msg["id"].startswith("m_")
    assert '<technical type="technical">' in msg["content"]


def test_make_technical_feedback_message_wraps_schema_errors_as_error_message():
    msg = processing_module._make_technical_feedback_user_message(
        "Tool Argument Validation Failed. Expected schema: {'type': 'object'}",
        technical_type=TECHNICAL_TYPE_ERROR_MESSAGE,
    )
    assert msg["role"] == "user"
    assert '<technical type="error_message">' in msg["content"]


def test_sanitize_messages_for_chat_completions_replaces_invalid_tool_call():
    messages = [
        {
            "id": "fc_1",
            "type": "function_call",
            "call_id": "call_1",
            "name": "fs_write_file",
            "arguments": '{"path": "/tmp/demo.md"',
            "status": "completed",
        }
    ]

    sanitized = sanitize_messages_for_chat_completions(
        messages,
        invalid_tool_call_factory=build_invalid_tool_call_feedback_message,
    )
    assert len(sanitized) == 1
    assert sanitized[0]["role"] == "user"
    assert '<technical type="error_message">' in sanitized[0]["content"]
    assert "fs_write_file" in sanitized[0]["content"]


def test_request_backend_canonical_sanitizes_state_history_for_chat_completions(monkeypatch):
    captured = {}

    def _fake_create_call_headers(_state, _ctx, resolved=None):
        return {"Authorization": "Bearer fake"}

    def _fake_request_backend_chat_completions(data, *_args, **_kwargs):
        captured["messages"] = data.get("messages", [])
        return {
            "id": "chat_1",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "ok"},
                }
            ],
        }

    monkeypatch.setattr(
        processing_module, "create_call_headers", _fake_create_call_headers
    )
    monkeypatch.setattr(
        "request_backend_policy.request_backend_chat_completions",
        _fake_request_backend_chat_completions,
    )

    state = State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=2,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    state.messages = [
        {
            "id": "fc_old",
            "type": "function_call",
            "call_id": "call_old",
            "name": "fs_write_file",
            "arguments": '{"path": "/tmp/demo.md"',
            "status": "completed",
        }
    ]
    ctx = SimpleNamespace(
        config_dict={
            "backend_api_mode": "chat_completions",
            "completions_url": "http://backend.local/v1/chat/completions",
            "use_ssl": True,
            "use_retry": False,
            "backend_request_timeout_sec": 600,
        },
        logger=None,
    )

    processing_module._request_backend_canonical(
        [{"role": "user", "content": "next", "id": "m_1"}],
        state,
        ctx,
    )

    payload_messages = captured["messages"]
    assert len(payload_messages) == 2
    assert payload_messages[0]["role"] == "user"
    assert "<technical type=\"error_message\">" in payload_messages[0]["content"]
    assert payload_messages[1]["role"] == "user"
