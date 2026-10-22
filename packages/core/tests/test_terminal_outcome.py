"""TDD: State.terminal_outcome — поле и его установка в точках терминирования."""

import importlib
import json
import sys
import types

from gc_core.auto_routing.policy import NO_TOOL_BOUNCE
from gc_core.states import State
from gc_testkit import run_coro
from gc_testkit.paths import module_dir
from gc_testkit.settings import fake_ctx


def _make_state(max_steps=2):
    # Сигнатура из существующих тестов (tests/test_processing_responses_feedback.py):
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


def test_terminal_outcome_defaults_none():
    assert _make_state().terminal_outcome is None


# ---------------------------------------------------------------------------
# Поведенческий тест: step_limit устанавливает terminal_outcome
# ---------------------------------------------------------------------------


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


processing_module = None
arun_agent = None


def setup_module(_module) -> None:
    global processing_module, arun_agent
    _stub_tools_modules()
    from gc_core import processing_responses as pm
    from gc_core.processing_responses import arun_agent as ra

    processing_module = pm
    arun_agent = ra
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
        raise AssertionError("apply_tool should not be called in this test")

    def _is_unknown_tool_output(_output):
        return False

    def _is_internal_tool_error_output(_output):
        return False

    tools_pkg.aapply_tool = _apply_tool
    tools_pkg.WrongFunctionArgumentsException = (
        _WrongFunctionArgumentsException
    )
    tools_pkg.is_unknown_tool_output = _is_unknown_tool_output
    tools_pkg.is_internal_tool_error_output = _is_internal_tool_error_output
    tools_dir = module_dir("tools")
    # `processing_responses` импортирует эти имена на уровне модуля. Без них
    # подмена пакета даёт ImportError, и файл падает целиком — но только
    # когда реальные модули не подтянул сосед по прогону. При случайном
    # порядке (pytest-randomly) это молчащая мина, а не свойство файла.
    tools_pkg.is_internal_tool_error_output = lambda *_a, **_kw: False
    tools_pkg.__path__ = [str(tools_dir)]
    sys.modules["gc_core.tools"] = tools_pkg


def _make_ctx(**overrides):
    import logging

    cfg = {
        "backend.api_mode": "chat_completions",
        "backend.completions_url": "http://backend.local/v1/chat/completions",
        "backend.chat_completions_max_tokens": 100,
        "backend.chat_completions_min_response_tokens": 30,
        "backend.request_timeout_sec": 30,
        "backend.use_ssl": True,
        "backend.use_retry": False,
        "logging.console_trace": False,
        "agent.no_tool_call_max_count": 5,
        "agent.strict_turn_finish_validation": False,
        # Без live LLM в unit-тестах: envelope возвращается с пустым summary.
        "agent.limit_trace_summary": False,
    }
    cfg.update(overrides)
    return fake_ctx(
        cfg,
        logger=logging.getLogger("test_terminal_outcome"),
        auth_connector=None,
    )


def _text_response_from_backend():
    """Стаб-ответ бэкенда: текстовый, без tool-call → бесконечный цикл до step_limit."""
    return [
        {"role": "assistant", "type": "message", "content": "Обрабатываю..."}
    ]


def test_step_limit_sets_terminal_outcome(monkeypatch):
    """Когда шаги исчерпаны, run_agent выставляет terminal_outcome='step_limit'."""
    call_count = {"n": 0}

    async def _fake_process_user_ask(_messages, _state, _ctx):
        call_count["n"] += 1
        return _text_response_from_backend(), "resp_stub"

    monkeypatch.setattr(
        processing_module, "aprocess_user_ask", _fake_process_user_ask
    )

    state = _make_state(max_steps=1)
    result = run_coro(
        arun_agent(
            [{"role": "user", "content": "привет", "id": "m1"}],
            state,
            _make_ctx(),
        )
    )

    _env = json.loads(result)
    assert _env["ok"] is False
    assert _env["status"] == "step_limit"
    assert (
        "step" in _env["message"].lower() or "limit" in _env["message"].lower()
    )
    assert "raw_trace_summary" in _env
    assert state.terminal_outcome == "step_limit"
    assert NO_TOOL_BOUNCE in state.tick_signals


def test_step_limit_still_executes_pending_function_calls(monkeypatch):
    """Лимит шагов не отрезает уже присланный моделью batch tool calls.

    Сценарий: budget уже исчерпан (steps >= max_steps), в messages — function_call.
    Раньше run_agent стопорился до process_tool_calls → orphan call без output.
    Теперь batch исполняется (step += 1 после batch), затем step_limit на
    function_call_output без лишнего LLM-запроса.
    """
    applied = []

    async def _apply_tool(*, tool_name, arguments, **_kw):
        applied.append((tool_name, arguments))
        return json.dumps({"ok": True, "tool": tool_name})

    async def _fake_process_tool_outputs(*_a, **_k):
        raise AssertionError(
            "process_tool_outputs must not run after step_limit (no extra LLM round once budget is exhausted)"
        )

    monkeypatch.setattr(processing_module, "aapply_tool", _apply_tool)
    monkeypatch.setattr(
        processing_module, "aprocess_tool_outputs", _fake_process_tool_outputs
    )

    state = _make_state(max_steps=1)
    # Budget уже исчерпан; pending FC всё равно должны исполниться.
    state.steps = 1

    pending_batch = [
        {
            "type": "function_call",
            "name": "session_think",
            "arguments": json.dumps(
                {"thought": "must-run-despite-step-limit"}
            ),
            "call_id": "call_pending_1",
            "id": "fc_pending_1",
            "status": "completed",
        },
        {
            "type": "function_call",
            "name": "session_think",
            "arguments": json.dumps({"thought": "second-in-batch"}),
            "call_id": "call_pending_2",
            "id": "fc_pending_2",
            "status": "completed",
        },
    ]

    result = run_coro(arun_agent(pending_batch, state, _make_ctx()))

    assert len(applied) == 2, f"both pending tools must run, got {applied!r}"
    assert applied[0][0] == "session_think"
    assert applied[1][0] == "session_think"

    _env = json.loads(result)
    assert _env["ok"] is False
    assert _env["status"] == "step_limit"
    assert state.terminal_outcome == "step_limit"
    # После batch ход посчитан (steps стал 2), stop на следующем half-turn.
    assert state.steps >= 1

    # Outputs попали в state — trace парный, ничего не потеряно.
    outputs = [
        m for m in state.messages if m.get("type") == "function_call_output"
    ]
    assert {m.get("call_id") for m in outputs} >= {
        "call_pending_1",
        "call_pending_2",
    }


def test_step_counts_completed_model_turn_not_half_turn(monkeypatch):
    """Один serial tool round-trip = 1 step (не 2 half-turn).

    user_ask → LLM(FC) → exec batch → FCO → LLM(text bounce) → … до max_steps.
    При max_steps=2 успеваем исполнить 2 tool-батча, затем step_limit.
    """
    llm_rounds = {"n": 0}
    applied = []

    async def _fake_process_user_ask(_messages, _state, _ctx):
        llm_rounds["n"] += 1
        return _function_call_batch(1, llm_rounds["n"]), "resp_stub"

    async def _fake_process_tool_outputs(_messages, _state, _ctx):
        llm_rounds["n"] += 1
        # После tool outputs снова tool call, пока step budget позволяет.
        return _function_call_batch(1, llm_rounds["n"]), "resp_stub"

    async def _apply_tool(*, tool_name, arguments, **_kw):
        applied.append(tool_name)
        return json.dumps({"ok": True, "tool": tool_name})

    monkeypatch.setattr(
        processing_module, "aprocess_user_ask", _fake_process_user_ask
    )
    monkeypatch.setattr(
        processing_module, "aprocess_tool_outputs", _fake_process_tool_outputs
    )
    monkeypatch.setattr(processing_module, "aapply_tool", _apply_tool)

    state = _make_state(max_steps=2)
    result = run_coro(
        arun_agent(
            [{"role": "user", "content": "привет", "id": "m1"}],
            state,
            _make_ctx(),
        )
    )

    _env = json.loads(result)
    assert _env["status"] == "step_limit"
    assert state.terminal_outcome == "step_limit"
    assert state.steps == 2
    # 2 полных хода = 2 tool call'а (по одному serial tool на ход).
    assert len(applied) == 2
    assert state.tool_calls == 2


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
    async def _fake_process_user_ask(_messages, _state, _ctx):
        return _function_call_batch(2, 1), "resp_stub"

    monkeypatch.setattr(
        processing_module, "aprocess_user_ask", _fake_process_user_ask
    )

    async def _apply_tool(**_kw):
        return "ok"

    monkeypatch.setattr(processing_module, "aapply_tool", _apply_tool)

    state = _make_state(max_steps=10)
    result = run_coro(
        arun_agent(
            [{"role": "user", "content": "привет", "id": "m1"}],
            state,
            _make_ctx(**{"agent.max_tool_calls": 2}),
        )
    )
    _env = json.loads(result)
    assert _env["ok"] is False
    assert _env["status"] == "tool_call_limit"
    assert "2" in _env["message"]
    assert "raw_trace_summary" in _env
    assert state.terminal_outcome == "tool_call_limit"
    assert state.tool_calls >= 2


def test_tool_calls_counter_counts_batch_none_limit(monkeypatch):
    """max_tool_calls отсутствует: батч из 3 считается, останова по вызовам нет."""
    calls = {"n": 0}

    async def _fake_process_user_ask(_messages, _state, _ctx):
        calls["n"] += 1
        return _function_call_batch(3, calls["n"]), "resp_stub"

    async def _fake_process_tool_outputs(_messages, _state, _ctx):
        return _text_response_from_backend(), "resp_stub"

    monkeypatch.setattr(
        processing_module, "aprocess_user_ask", _fake_process_user_ask
    )
    monkeypatch.setattr(
        processing_module, "aprocess_tool_outputs", _fake_process_tool_outputs
    )

    async def _apply_tool(**_kw):
        return "ok"

    monkeypatch.setattr(processing_module, "aapply_tool", _apply_tool)

    state = _make_state(max_steps=2)
    run_coro(
        arun_agent(
            [{"role": "user", "content": "привет", "id": "m1"}],
            state,
            _make_ctx(),
        )
    )
    assert state.tool_calls == 3
    assert state.terminal_outcome == "step_limit"  # лимит вызовов не сработал


def test_token_limit_sets_terminal_outcome_aux_category(monkeypatch):
    """Бюджет тратят и aux-категории (summary), не только main."""

    async def _fake_process_user_ask(_messages, state, _ctx):
        state.usage.add("summary", input_tokens=100, output_tokens=5)
        return _text_response_from_backend(), "resp_stub"

    monkeypatch.setattr(
        processing_module, "aprocess_user_ask", _fake_process_user_ask
    )

    state = _make_state(max_steps=5)
    result = run_coro(
        arun_agent(
            [{"role": "user", "content": "привет", "id": "m1"}],
            state,
            _make_ctx(**{"agent.session_token_limit": 10}),
        )
    )
    _env = json.loads(result)
    assert _env["ok"] is False
    assert _env["status"] == "token_limit"
    assert "token" in _env["message"].lower()
    assert "raw_trace_summary" in _env
    assert state.terminal_outcome == "token_limit"


def test_token_limit_ignores_thread_cumulative(monkeypatch):
    """Resume-кейс: накопленный до рана usage не съедает бюджет рана."""

    async def _fake_process_user_ask(_messages, _state, _ctx):
        return _text_response_from_backend(), "resp_stub"

    monkeypatch.setattr(
        processing_module, "aprocess_user_ask", _fake_process_user_ask
    )

    state = _make_state(max_steps=1)
    state.usage.add(
        "main", input_tokens=900, output_tokens=100
    )  # «прошлые» раны треда
    state.run_start_usage_meta = (
        state.usage.to_meta()
    )  # как делает agent_executor
    result = run_coro(
        arun_agent(
            [{"role": "user", "content": "привет", "id": "m1"}],
            state,
            _make_ctx(**{"agent.session_token_limit": 500}),
        )
    )
    # ран ничего не потратил → до token_limit не дошли, упёрлись в шаги
    _env = json.loads(result)
    assert _env["status"] == "step_limit"
    assert "token" not in _env["message"].lower()
