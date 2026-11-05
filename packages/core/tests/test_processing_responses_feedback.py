import importlib
import sys
import types

from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_ERROR_MESSAGE,
    build_invalid_tool_call_feedback_message,
)
from gc_core.states import State
from gc_core.utils.convertations import sanitize_messages_for_chat_completions
from gc_testkit import as_coro, run_coro
from gc_testkit.paths import module_dir
from gc_testkit.settings import fake_ctx


def teardown_module(_module) -> None:
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


def _stub_tools_modules() -> None:
    for name in list(sys.modules):
        if name == "gc_core.tools" or name.startswith("gc_core.tools."):
            sys.modules.pop(name, None)

    tools_pkg = types.ModuleType("gc_core.tools")

    class _WrongFunctionArgumentsException(Exception):
        def to_llm_feedback(self):
            return str(self)

    async def _apply_tool(*_args, **_kwargs):
        raise AssertionError("apply_tool should not be called in this test")

    tools_pkg.aapply_tool = _apply_tool
    tools_pkg.WrongFunctionArgumentsException = (
        _WrongFunctionArgumentsException
    )
    tools_pkg.is_unknown_tool_output = lambda _output: False
    tools_dir = module_dir("tools")
    # `processing_responses` импортирует эти имена на уровне модуля. Без них
    # подмена пакета даёт ImportError, и файл падает целиком — но только
    # когда реальные модули не подтянул сосед по прогону. При случайном
    # порядке (pytest-randomly) это молчащая мина, а не свойство файла.
    tools_pkg.is_internal_tool_error_output = lambda *_a, **_kw: False
    tools_pkg.__path__ = [str(tools_dir)]
    sys.modules["gc_core.tools"] = tools_pkg


processing_module = None


def setup_module(_module) -> None:
    global processing_module
    _stub_tools_modules()
    from gc_core import processing_responses as pm

    processing_module = pm
    pm.aapply_tool = sys.modules["gc_core.tools"].aapply_tool


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


def test_request_backend_canonical_sanitizes_state_history_for_chat_completions(
    monkeypatch,
):
    captured = {}

    def _fake_create_call_headers(_state, _ctx, resolved=None):
        return {"Authorization": "Bearer fake"}

    async def _fake_request_backend_chat_completions(data, *_args, **_kwargs):
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
        processing_module,
        "acreate_call_headers",
        as_coro(_fake_create_call_headers),
    )
    monkeypatch.setattr(
        "gc_core.request_backend_policy.arequest_backend_chat_completions",
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
    ctx = fake_ctx(
        {
            "backend.api_mode": "chat_completions",
            "backend.completions_url": "http://backend.local/v1/chat/completions",
            "backend.use_ssl": True,
            "backend.use_retry": False,
            "backend.request_timeout_sec": 600,
        },
        logger=None,
    )

    run_coro(
        processing_module._arequest_backend_canonical(
            [{"role": "user", "content": "next", "id": "m_1"}],
            state,
            ctx,
            resolved=None,
        )
    )

    payload_messages = captured["messages"]
    assert len(payload_messages) == 2
    assert payload_messages[0]["role"] == "user"
    assert '<technical type="error_message">' in payload_messages[0]["content"]
    assert payload_messages[1]["role"] == "user"


def test_request_keeps_malformed_tool_call_that_already_has_output(
    monkeypatch,
):
    """Ответ уже записан — вызов с битым JSON уходит парой, без подмены на user."""
    captured = {}

    def _fake_create_call_headers(_state, _ctx, resolved=None):
        return {"Authorization": "Bearer fake"}

    async def _fake_request_backend_chat_completions(data, *_args, **_kwargs):
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
        processing_module,
        "acreate_call_headers",
        as_coro(_fake_create_call_headers),
    )
    monkeypatch.setattr(
        "gc_core.request_backend_policy.arequest_backend_chat_completions",
        _fake_request_backend_chat_completions,
    )

    broken_arguments = '{"path": '
    error_output = '{"ok": false, "message": "некорректный JSON"}'
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
            "id": "fc_good",
            "type": "function_call",
            "call_id": "good",
            "name": "fs_read_file",
            "arguments": '{"path": "ok.txt"}',
            "status": "completed",
        },
        {
            "id": "fc_bad",
            "type": "function_call",
            "call_id": "bad",
            "name": "fs_read_file",
            "arguments": broken_arguments,
            "status": "completed",
        },
        {
            "id": "out_good",
            "type": "function_call_output",
            "call_id": "good",
            "output": '{"ok": true}',
        },
        {
            "id": "out_bad",
            "type": "function_call_output",
            "call_id": "bad",
            "output": error_output,
        },
    ]
    ctx = fake_ctx(
        {
            "backend.api_mode": "chat_completions",
            "backend.completions_url": "http://backend.local/v1/chat/completions",
            "backend.use_ssl": True,
            "backend.use_retry": False,
            "backend.request_timeout_sec": 600,
        },
        logger=None,
    )

    run_coro(
        processing_module._arequest_backend_canonical(
            [{"role": "user", "content": "next", "id": "m_1"}],
            state,
            ctx,
            resolved=None,
        )
    )

    payload_messages = captured["messages"]
    assistant = next(
        message
        for message in payload_messages
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    tool_calls = assistant["tool_calls"]
    assert [call["id"] for call in tool_calls] == ["good", "bad"]
    assert tool_calls[1]["function"]["arguments"] == broken_arguments
    bad_tool = next(
        message
        for message in payload_messages
        if message.get("role") == "tool"
        and message.get("tool_call_id") == "bad"
    )
    assert bad_tool["content"] == error_output
    assistant_at = payload_messages.index(assistant)
    bad_at = payload_messages.index(bad_tool)
    assert assistant_at < bad_at
    assert all(
        message.get("role") == "tool"
        for message in payload_messages[assistant_at + 1 : bad_at + 1]
    )
    assert not any(
        message.get("role") == "user"
        and "error_message" in str(message.get("content"))
        for message in payload_messages
    )
