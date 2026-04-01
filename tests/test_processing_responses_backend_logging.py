import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace


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

from processing_responses import _request_backend_canonical  # noqa: E402
from states import State  # noqa: E402


def test_chat_completions_logs_raw_backend_payload_on_conversion_error(caplog, monkeypatch):
    def _fake_create_call_headers(_state, _ctx):
        return {"Authorization": "Bearer fake"}

    def _fake_create_call_data(_messages, _state):
        return {"model": "CodeChat", "messages": [{"role": "user", "content": "ping"}], "tools": []}

    def _fake_request_backend(*_args, **_kwargs):
        # Нет поля choices -> внутри конвертации будет KeyError('choices').
        return {"id": "chat_1", "object": "chat.completion", "model": "CodeChat"}

    monkeypatch.setattr("processing_responses.create_call_headers", _fake_create_call_headers)
    monkeypatch.setattr("processing_responses.create_call_data_completions", _fake_create_call_data)
    monkeypatch.setattr("processing_responses.request_backend_chat_completions", _fake_request_backend)

    logger = logging.getLogger("test_backend_payload_logging")
    ctx = SimpleNamespace(
        config_dict={
            "backend_api_mode": "chat_completions",
            "completions_url": "http://backend.local/v1/chat/completions",
            "use_ssl": True,
            "use_retry": False,
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
            _request_backend_canonical(
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
