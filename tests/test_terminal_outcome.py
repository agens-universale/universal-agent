"""TDD: State.terminal_outcome — поле и его установка в точках терминирования."""
import importlib
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from states import State


def _make_state(max_steps=2):
    # Сигнатура из существующих тестов (tests/test_processing_responses_feedback.py):
    return State(
        verbose=False, model="CodeChat", tools_spec=[], tools_descr=[],
        skills_list=[], max_steps=max_steps, system_prompt_id="v1",
        use_server_subagents=False,
    )


def test_terminal_outcome_defaults_none():
    assert _make_state().terminal_outcome is None


# ---------------------------------------------------------------------------
# Поведенческий тест: step_limit устанавливает terminal_outcome
# ---------------------------------------------------------------------------

def teardown_module(_module) -> None:
    for name in list(sys.modules):
        if name == "tools" or name.startswith("tools."):
            sys.modules.pop(name, None)
    real_tools = importlib.import_module("tools")
    proc = sys.modules.get("processing_responses")
    if proc is not None:
        proc.apply_tool = real_tools.apply_tool
        proc.WrongFunctionArgumentsException = real_tools.WrongFunctionArgumentsException
    cycle_stop_mod = sys.modules.get("message_policy.cycle_stop")
    if cycle_stop_mod is not None:
        cycle_stop_mod.apply_tool = real_tools.apply_tool


processing_module = None
run_agent = None


def setup_module(_module) -> None:
    global processing_module, run_agent
    _stub_tools_modules()
    import processing_responses as pm
    from processing_responses import run_agent as ra
    processing_module = pm
    run_agent = ra
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
        raise AssertionError("apply_tool should not be called in this test")

    def _is_unknown_tool_output(_output):
        return False

    tools_pkg.apply_tool = _apply_tool
    tools_pkg.WrongFunctionArgumentsException = _WrongFunctionArgumentsException
    tools_pkg.is_unknown_tool_output = _is_unknown_tool_output
    root = Path(__file__).resolve().parents[1]
    tools_dir = root / "src" / "tools"
    tools_pkg.__path__ = [str(tools_dir)]
    sys.modules["tools"] = tools_pkg


def _make_ctx(**overrides):
    import logging
    cfg = {
        "backend_api_mode": "chat_completions",
        "completions_url": "http://backend.local/v1/chat/completions",
        "chat_completions_max_tokens": 100,
        "chat_completions_min_response_tokens": 30,
        "backend_request_timeout_sec": 30,
        "use_ssl": True,
        "use_retry": False,
        "console_trace": False,
        "no_tool_call_max_count": 5,
        "strict_turn_finish_validation": False,
    }
    cfg.update(overrides)
    return SimpleNamespace(
        config_dict=cfg,
        logger=logging.getLogger("test_terminal_outcome"),
        auth_connector=None,
    )


def _text_response_from_backend():
    """Стаб-ответ бэкенда: текстовый, без tool-call → бесконечный цикл до step_limit."""
    return [{"role": "assistant", "type": "message", "content": "Обрабатываю..."}]


def test_step_limit_sets_terminal_outcome(monkeypatch):
    """Когда шаги исчерпаны, run_agent выставляет terminal_outcome='step_limit'."""
    call_count = {"n": 0}

    def _fake_process_user_ask(_messages, _state, _ctx):
        call_count["n"] += 1
        return _text_response_from_backend(), "resp_stub"

    monkeypatch.setattr(processing_module, "process_user_ask", _fake_process_user_ask)

    state = _make_state(max_steps=1)
    result = run_agent(
        [{"role": "user", "content": "привет", "id": "m1"}],
        state,
        _make_ctx(),
    )

    assert "step" in result.lower() or "limit" in result.lower()
    assert state.terminal_outcome == "step_limit"


def _function_call_batch(n: int, step: int) -> list:
    """Стаб-ответ бэкенда: батч из n tool-call'ов."""
    return [
        {
            "type": "function_call",
            "name": "session_think",
            "arguments": json.dumps({"thought": f"t{step}-{i}"}),
            "call_id": f"call_{step}_{i}",
            "id": f"fc_{step}_{i}",
            "status": "completed",
        }
        for i in range(n)
    ]


def test_tool_call_limit_sets_terminal_outcome(monkeypatch):
    def _fake_process_user_ask(_messages, _state, _ctx):
        return _function_call_batch(2, 1), "resp_stub"

    monkeypatch.setattr(processing_module, "process_user_ask", _fake_process_user_ask)
    monkeypatch.setattr(processing_module, "apply_tool", lambda **_kw: "ok")

    state = _make_state(max_steps=10)
    result = run_agent(
        [{"role": "user", "content": "привет", "id": "m1"}],
        state,
        _make_ctx(max_tool_calls=2),
    )
    assert state.terminal_outcome == "tool_call_limit"
    assert state.tool_calls >= 2
    assert "tool calls of 2 limit" in result


def test_tool_calls_counter_counts_batch_none_limit(monkeypatch):
    """max_tool_calls отсутствует: батч из 3 считается, останова по вызовам нет."""
    calls = {"n": 0}

    def _fake_process_user_ask(_messages, _state, _ctx):
        calls["n"] += 1
        return _function_call_batch(3, calls["n"]), "resp_stub"

    def _fake_process_tool_outputs(_messages, _state, _ctx):
        return _text_response_from_backend(), "resp_stub"

    monkeypatch.setattr(processing_module, "process_user_ask", _fake_process_user_ask)
    monkeypatch.setattr(processing_module, "process_tool_outputs", _fake_process_tool_outputs)
    monkeypatch.setattr(processing_module, "apply_tool", lambda **_kw: "ok")

    state = _make_state(max_steps=2)
    run_agent(
        [{"role": "user", "content": "привет", "id": "m1"}],
        state,
        _make_ctx(),
    )
    assert state.tool_calls == 3
    assert state.terminal_outcome == "step_limit"  # лимит вызовов не сработал


def test_token_limit_sets_terminal_outcome_aux_category(monkeypatch):
    """Бюджет тратят и aux-категории (summary), не только main."""
    def _fake_process_user_ask(_messages, state, _ctx):
        state.usage.add("summary", input_tokens=100, output_tokens=5)
        return _text_response_from_backend(), "resp_stub"

    monkeypatch.setattr(processing_module, "process_user_ask", _fake_process_user_ask)

    state = _make_state(max_steps=5)
    result = run_agent(
        [{"role": "user", "content": "привет", "id": "m1"}],
        state,
        _make_ctx(session_token_limit=10),
    )
    assert state.terminal_outcome == "token_limit"
    assert "session token limit" in result


def test_token_limit_ignores_thread_cumulative(monkeypatch):
    """Resume-кейс: накопленный до рана usage не съедает бюджет рана."""
    def _fake_process_user_ask(_messages, _state, _ctx):
        return _text_response_from_backend(), "resp_stub"

    monkeypatch.setattr(processing_module, "process_user_ask", _fake_process_user_ask)

    state = _make_state(max_steps=1)
    state.usage.add("main", input_tokens=900, output_tokens=100)  # «прошлые» раны треда
    state.run_start_usage_meta = state.usage.to_meta()  # как делает agent_executor
    result = run_agent(
        [{"role": "user", "content": "привет", "id": "m1"}],
        state,
        _make_ctx(session_token_limit=500),
    )
    # ран ничего не потратил → до token_limit не дошли, упёрлись в шаги
    assert state.terminal_outcome == "step_limit"
    assert "token" not in result
