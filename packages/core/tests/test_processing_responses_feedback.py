import sys
import types

import pytest
from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_ERROR_MESSAGE,
    build_invalid_tool_call_feedback_message,
)
from gc_core.states import State
from gc_core.utils.convertations import sanitize_invalid_tool_calls
from gc_testkit import as_coro, run_coro
from gc_testkit.paths import module_dir
from gc_testkit.settings import fake_ctx
from helpers.module_snapshot import ModuleSnapshot

_SNAPSHOT: ModuleSnapshot | None = None


def teardown_module(_module) -> None:
    if _SNAPSHOT is not None:
        _SNAPSHOT.restore()


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
    global _SNAPSHOT
    _SNAPSHOT = ModuleSnapshot()
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


def test_sanitize_invalid_tool_calls_replaces_invalid_tool_call():
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

    sanitized = sanitize_invalid_tool_calls(
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
    """Ответ записан — вызов уходит парой, без подмены на user, но с ``{}``.

    Сырой обрезок бэкенд разбирает и отбивает 400 на каждом следующем ходе.
    """
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
    assert tool_calls[1]["function"]["arguments"] == "{}"
    assert state.messages[1]["arguments"] == broken_arguments
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


def _answered_call_on_the_wire(monkeypatch, api_mode, arguments):
    """(аргументы записанного вызова в запросе к бэкенду, state)."""
    captured = {}

    async def _chat(data, *_args, **_kwargs):
        captured["calls"] = [
            call["function"]["arguments"]
            for message in data["messages"]
            for call in message.get("tool_calls") or []
        ]
        return {
            "id": "chat_1",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "ok"},
                }
            ],
        }

    async def _responses(data, *_args, **_kwargs):
        captured["calls"] = [
            item["arguments"]
            for item in data["input"]
            if item.get("type") == "function_call"
        ]
        return {"id": "resp_1", "output": []}, {}

    monkeypatch.setattr(
        processing_module,
        "acreate_call_headers",
        as_coro(lambda _state, _ctx, resolved=None: {}),
    )
    monkeypatch.setattr(
        "gc_core.request_backend_policy.arequest_backend_chat_completions",
        _chat,
    )
    monkeypatch.setattr(processing_module, "arequest_backend", _responses)
    history = [
        {
            "id": "fc_1",
            "type": "function_call",
            "call_id": "call_1",
            "name": "fs_write_file",
            "arguments": arguments,
            "status": "completed",
        },
        {
            "id": "out_1",
            "type": "function_call_output",
            "call_id": "call_1",
            "output": '{"ok": false}',
        },
    ]
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
    state.messages = list(history)
    ctx = fake_ctx(
        {
            "backend.api_mode": api_mode,
            "backend.completions_url": "http://backend.local/v1",
            "backend.use_retry": False,
        },
        logger=None,
    )
    run_coro(
        processing_module._arequest_backend_canonical(
            [*history, {"role": "user", "content": "next", "id": "m_1"}],
            state,
            ctx,
            resolved=None,
            set_previous_response_id=False,
        )
    )
    return captured["calls"], state


@pytest.mark.parametrize("api_mode", ["chat_completions", "responses"])
def test_cut_off_arguments_go_as_empty_object(monkeypatch, api_mode):
    """Responses после промаха кэша шлёт полную историю — та же 400."""
    calls, state = _answered_call_on_the_wire(monkeypatch, api_mode, '{"a": ')
    assert calls == ["{}"]
    assert state.messages[0]["arguments"] == '{"a": '


@pytest.mark.parametrize("arguments", ["[]", "null", "5"])
def test_json_that_is_not_an_object_goes_as_empty_object(
    monkeypatch, arguments
):
    calls, state = _answered_call_on_the_wire(
        monkeypatch, "chat_completions", arguments
    )
    assert calls == ["{}"]
    assert state.messages[0]["arguments"] == arguments


def test_dict_arguments_go_as_their_json(monkeypatch):
    """Бэкенд отдал аргументы объектом — вызов отработал, данные не терять."""
    calls, state = _answered_call_on_the_wire(
        monkeypatch, "chat_completions", {"path": "отчёт.md"}
    )
    assert calls == ['{"path": "отчёт.md"}']
    assert state.messages[0]["arguments"] == {"path": "отчёт.md"}


def test_model_batch_keeps_json_that_is_not_an_object():
    """Строгость — только для запроса к бэкенду; батч модели как прежде."""
    call = {
        "type": "function_call",
        "call_id": "call_1",
        "name": "fs_read_file",
        "arguments": "[]",
    }
    assert sanitize_invalid_tool_calls(
        [call], invalid_tool_call_factory=lambda _message: None
    ) == [call]
