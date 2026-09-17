import importlib
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from gc_testkit.paths import module_dir


def teardown_module(_module) -> None:
    for name in list(sys.modules):
        if name == "gc_core.tools" or name.startswith("gc_core.tools."):
            sys.modules.pop(name, None)
    real_tools = importlib.import_module("gc_core.tools")
    proc = sys.modules.get("gc_core.processing_responses")
    if proc is not None:
        proc.apply_tool = real_tools.apply_tool
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

    def _apply_tool(*_args, **_kwargs):
        raise AssertionError("apply_tool should not be called in this test")

    tools_pkg.apply_tool = _apply_tool
    tools_pkg.WrongFunctionArgumentsException = _WrongFunctionArgumentsException
    tools_dir = module_dir("tools")
    tools_pkg.__path__ = [str(tools_dir)]
    sys.modules["gc_core.tools"] = tools_pkg


processing_module = None
State = None


def setup_module(_module) -> None:
    global processing_module, State
    _stub_tools_modules()
    from gc_core import processing_responses as proc
    from gc_core.states import State as St

    processing_module = proc
    proc.apply_tool = sys.modules["gc_core.tools"].apply_tool
    State = St


def test_chat_completions_logs_raw_backend_payload_on_conversion_error(caplog, monkeypatch):
    def _fake_create_call_headers(_state, _ctx, resolved=None):
        return {"Authorization": "Bearer fake"}

    def _fake_create_call_data(_messages, _state, **_kwargs):
        return {"model": "CodeChat", "messages": [{"role": "user", "content": "ping"}], "tools": []}

    def _fake_request_policy(*_args, **_kwargs):
        # Нет поля choices -> внутри конвертации будет KeyError('choices').
        return (
            {"id": "chat_1", "object": "chat.completion", "model": "CodeChat"},
            {"model": "CodeChat", "messages": [{"role": "user", "content": "ping"}], "tools": []},
        )

    monkeypatch.setattr(
        processing_module, "create_call_headers", _fake_create_call_headers
    )
    monkeypatch.setattr(
        processing_module, "create_call_data_completions", _fake_create_call_data
    )
    monkeypatch.setattr(
        processing_module,
        "request_chat_completions_with_policy",
        _fake_request_policy,
    )

    logger = logging.getLogger("test_backend_payload_logging")
    ctx = SimpleNamespace(
        config_dict={
            "backend_api_mode": "chat_completions",
            "completions_url": "http://backend.local/v1/chat/completions",
            "use_ssl": True,
            "use_retry": False,
            "backend_request_timeout_sec": 600,
        },
        logger=logger,
    )
    state = State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=1,
        system_prompt_id="v1",
        use_server_subagents=False,
    )

    with caplog.at_level(logging.ERROR):
        try:
            processing_module._request_backend_canonical(
                [{"role": "user", "content": "hello", "id": "m_1"}],
                state,
                ctx,
            )
            assert False, "Expected KeyError('choices')"
        except KeyError as exc:
            assert str(exc) == "'choices'"

    log_text = caplog.text
    assert "Failed to convert chat/completions payload." in log_text
    assert "context=" in log_text
    assert "payload=" in log_text
    assert '"id": "chat_1"' in log_text
    assert '"object": "chat.completion"' in log_text
