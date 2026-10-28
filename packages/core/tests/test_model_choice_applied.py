"""Применённый выбор рана и живое событие его смены (спека ручки модели §5.2).

Ран — настоящий исполнитель и цикл, скриптуется только ответ модели. Точка
подготовки вызова проверяется напрямую: ретрай того же такта — повторная
подготовка без закрытия такта.
"""

import logging

import pytest
from gc_core import model_choice
from gc_core import processing_responses as pr
from gc_core.agent_executor import AgentExecutor
from gc_core.auto_routing import state_ops
from gc_core.auto_routing.policy import Decision
from gc_core.model_choice import ModelChoiceApplied
from gc_core.model_router import ModelRouter
from gc_core.persistence.model_choice_record import ModelChoiceRecord
from gc_core.run_context import acreate_run_context
from gc_core.states import State
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_core.turn_boundary import begin_turn
from gc_core.utils.logging_module import release_run_logger
from parity_harness import (
    ScriptedBackend,
    assistant_tool_call,
    install,
    parity_config,
)

FINISH_ARGS = {
    "solution_type": "full_solution",
    "task": "Выполнить контрольную задачу",
    "task_report": "Проверка завершена",
    "handoff": "{}",
}
REGISTRY = {
    "models": {
        alias: {
            "model": alias,
            "url": f"http://{alias}/v1/chat/completions",
            "token": f"Bearer test-{alias}",
        }
        for alias in ("fast", "mid", "big")
    },
    "slots": {"light": "fast", "default": "mid", "max": "big"},
}
CONFIG = {
    "agent.max_steps": 15,
    "agent.strict_turn_finish_validation": False,
    "agent.auto_routing_safe_level": "default",
    "text_answer_mode": "deny",
    "intent_scope.tools": {},
    **REGISTRY,
}


class _Backend(ScriptedBackend):
    """Сценарий ходов; `on_call(номер)` — смена выбора во время запроса."""

    def __init__(self, turns, on_call):
        super().__init__(turns)
        self._on_call = on_call
        self.models = []

    def next_payload(self, request_payload):
        self.models.append((request_payload or {}).get("model"))
        self._on_call(len(self.models))
        return super().next_payload(request_payload)


def _read_choice(selected):
    """Выбор окна; исключение вместо выбора — канал выбора бросает."""
    value = selected["model"]
    if isinstance(value, Exception):
        raise value
    return value


def _work():
    return assistant_tool_call("exec_calculator", {"expression": "3 * 4"})


def _finish():
    return assistant_tool_call("turn_finish", FINISH_ARGS, "finish")


async def _run(
    tmp_path,
    monkeypatch,
    initial,
    turns,
    changes,
    *,
    stop_on=None,
    hook=None,
    executor_spy=None,
):
    """Ран с выбором `initial`; `changes[n]` — выбор, заданный во время
    n-го запроса (исключение — канал выбора бросает с этого запроса);
    `stop_on` — номер запроса, во время которого человек
    жмёт стоп; `hook` — свой приёмник события вместо списка.
    Возвращает события и модели запросов."""
    selected = {"model": initial}
    cfg = parity_config(tmp_path, monkeypatch, **CONFIG)
    monkeypatch.chdir(tmp_path)
    executor = None

    def on_call(number):
        if number in changes:
            selected["model"] = changes[number]
        if number == stop_on:
            executor.stop_run()

    backend = install(monkeypatch, _Backend(turns, on_call))
    ctx = await acreate_run_context(
        cfg, choice_provider=lambda: _read_choice(selected)
    )
    events = []
    ctx.on_model_choice_applied = hook or events.append
    executor = AgentExecutor(ctx)
    if executor_spy is not None:
        executor_spy(executor)
    try:
        await executor.arun(
            ["Выполни контрольную задачу"],
            thread_id="choice",
            system="cli",
            start_new_thread=True,
            message_ids=["input-0"],
        )
    finally:
        release_run_logger(ctx.logger)
    backend.assert_clean()
    return events, backend.models


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    """Главный трейс у точки подготовки: `await prepared(выбор)` даёт
    выбор, контекст, State и события. Применённый выбор — как на старте
    рана. Логгеры контекстов освобождаются после теста."""
    loggers = []

    async def make(initial):
        selected = {"model": initial}
        cfg = parity_config(tmp_path, monkeypatch, **CONFIG)
        ctx = await acreate_run_context(
            cfg, choice_provider=lambda: selected["model"]
        )
        loggers.append(ctx.logger)
        state = State(
            verbose=False,
            model="m",
            tools_spec=ctx.tools_spec,
            tools_descr=ctx.tools_description,
            skills_list=[],
            max_steps=10,
            system_prompt_id="test",
            use_server_subagents=False,
        )
        state.backend_api_mode = "chat_completions"
        begin_turn(state, ctx)
        state.applied_choice = initial
        events = []
        ctx.on_model_choice_applied = events.append
        return selected, ctx, state, events

    yield make
    for logger in loggers:
        release_run_logger(logger)


class _Records(logging.Handler):
    """Сообщения записей логгера рана."""

    def __init__(self, logger):
        super().__init__()
        self.messages = []
        logger.addHandler(self)

    def emit(self, record):
        self.messages.append(record.getMessage())


def _raise(_event):
    raise RuntimeError("frontend is gone")


async def _tick(state, ctx):
    """Один такт главного цикла: подготовка вызова и закрытие такта."""
    resolved = await pr.aprepare_main_call(state, ctx)
    state_ops.close_tick(state, state._call_level)
    return resolved


async def test_run_start_takes_current_choice_without_event(
    tmp_path, monkeypatch
):
    events, models = await _run(
        tmp_path, monkeypatch, "max", [_work(), _finish()], {}
    )

    assert events == []
    assert models == ["big", "big"]


async def test_change_during_run_emits_once_on_next_main_call(
    tmp_path, monkeypatch
):
    events, models = await _run(
        tmp_path,
        monkeypatch,
        "light",
        [_work(), _work(), _finish()],
        {1: "max"},
    )

    assert events == [ModelChoiceApplied("max", "big")]
    assert models == ["fast", "big", "big"]


async def test_two_changes_before_next_call_emit_only_the_last(prepared):
    selected, ctx, state, events = await prepared("light")
    await _tick(state, ctx)
    selected["model"] = "max"
    selected["model"] = "mid"
    await _tick(state, ctx)

    assert events == [ModelChoiceApplied("mid", "mid")]
    assert state.applied_choice == "mid"


async def test_event_names_the_choice_of_the_prepared_call(
    prepared, monkeypatch
):
    selected, ctx, state, events = await prepared("light")
    real = pr.aroute_before_call

    async def route_then_change(state, ctx):
        result = await real(state, ctx)
        selected["model"] = "light"
        return result

    monkeypatch.setattr(pr, "aroute_before_call", route_then_change)
    selected["model"] = "max"
    resolved = await _tick(state, ctx)

    assert resolved.alias == "big"
    assert events == [ModelChoiceApplied("max", "big")]


async def test_retry_of_same_tick_emits_nothing(prepared):
    selected, ctx, state, events = await prepared("light")
    await pr.aprepare_main_call(state, ctx)
    selected["model"] = "max"
    retried = await pr.aprepare_main_call(state, ctx)

    assert retried.alias == "fast"
    assert events == []
    state_ops.close_tick(state, state._call_level)
    await _tick(state, ctx)
    assert events == [ModelChoiceApplied("max", "big")]


async def test_auto_internal_level_change_emits_nothing(prepared, monkeypatch):
    selected, ctx, state, events = await prepared("auto")
    levels = iter(["light", "max", "default"])
    monkeypatch.setattr(
        pr,
        "two_level_decision",
        lambda *a, **k: Decision(next(levels), "test", (), True),
    )
    aliases = [(await _tick(state, ctx)).alias for _ in range(3)]

    assert aliases == ["fast", "big", "mid"]
    assert events == []


async def test_manual_same_model_as_auto_decision_is_a_change(prepared):
    selected, ctx, state, events = await prepared("auto")
    first = await _tick(state, ctx)
    selected["model"] = "mid"
    second = await _tick(state, ctx)

    assert first.alias == second.alias == "mid"
    assert events == [ModelChoiceApplied("mid", "mid")]


async def test_run_end_with_pending_choice_emits_at_run_end(
    tmp_path, monkeypatch
):
    events, models = await _run(
        tmp_path, monkeypatch, "light", [_finish()], {1: "max"}
    )

    assert events == [ModelChoiceApplied("max", "big", at_run_end=True)]
    assert models == ["fast"]


async def test_stop_with_pending_choice_emits_at_run_end(
    tmp_path, monkeypatch
):
    """Стоп рана при ожидающем выборе (Review Focus 3): событие — на
    завершении рана."""
    events, models = await _run(
        tmp_path, monkeypatch, "light", [_work()], {1: "max"}, stop_on=1
    )

    assert events == [ModelChoiceApplied("max", "big", at_run_end=True)]
    assert models == ["fast"]


async def test_failing_choice_hook_mid_run_does_not_abort_the_call(prepared):
    """Хук события посреди рана бросает — вызов готовится той моделью,
    выбор считается применённым (им ран и работает), сбой — в лог."""
    selected, ctx, state, _events = await prepared("light")
    ctx.on_model_choice_applied = _raise
    records = _Records(ctx.logger)
    await _tick(state, ctx)
    selected["model"] = "max"
    resolved = await _tick(state, ctx)

    assert resolved.alias == "big"
    assert state.applied_choice == "max"
    assert records.messages.count("model_choice_applied_failed") == 1


class _RunEndSpy:
    """Шпион завершения рана: шаги уборки по порядку и записи лога рана."""

    def __init__(self, monkeypatch):
        self.calls = []
        self.records = None
        leave = state_ops.leave

        def spy_leave(*args, **kwargs):
            self.calls.append("leave")
            return leave(*args, **kwargs)

        monkeypatch.setattr(state_ops, "leave", spy_leave)

    def __call__(self, executor):
        self.records = _Records(executor._ctx.logger)
        self._wrap(executor, "_arecord_turn_outcome", "record")
        self._wrap(executor, "_ateardown_run", "teardown")

    def _wrap(self, executor, name, step):
        real = getattr(executor, name)

        async def wrapped(*args, **kwargs):
            self.calls.append(step)
            return await real(*args, **kwargs)

        setattr(executor, name, wrapped)


async def test_failing_choice_hook_does_not_skip_run_teardown(
    tmp_path, monkeypatch
):
    """Хук события на завершении бросает — запись итога и уборка рана
    всё равно выполняются, ран завершается штатно."""
    spy = _RunEndSpy(monkeypatch)
    await _run(
        tmp_path,
        monkeypatch,
        "light",
        [_finish()],
        {1: "max"},
        hook=_raise,
        executor_spy=spy,
    )

    assert spy.calls[-3:] == ["leave", "record", "teardown"]
    assert "model_choice_applied_failed" in spy.records.messages


async def test_failing_choice_read_at_run_end_does_not_skip_teardown(
    tmp_path, monkeypatch
):
    """Канал выбора бросает на завершении рана — выход из рана, запись
    итога и уборка всё равно выполняются, событий нет, сбой — в лог."""
    spy = _RunEndSpy(monkeypatch)
    events, models = await _run(
        tmp_path,
        monkeypatch,
        "light",
        [_finish()],
        {1: RuntimeError("window is gone")},
        executor_spy=spy,
    )

    assert models == ["fast"]
    assert events == []
    assert spy.calls[-3:] == ["leave", "record", "teardown"]
    assert "model_choice_applied_failed" in spy.records.messages


async def test_failing_previous_model_resolve_emits_no_event(
    prepared, monkeypatch
):
    """Резолв прежней модели для записи бросает — событие без записи
    смены не уходит: хук не зовётся, сбой — в лог."""
    selected, ctx, state, events = await prepared("light")
    records = _Records(ctx.logger)
    monkeypatch.setattr(model_choice, "model_of", _raise_resolve)
    selected["model"] = "max"
    resolved = await _tick(state, ctx)

    assert resolved.alias == "big"
    assert events == []
    assert records.messages.count("model_choice_applied_failed") == 1


def _raise_resolve(_router, _choice):
    raise RuntimeError("registry is gone")


async def test_subagent_calls_emit_nothing(prepared):
    selected, ctx, state, events = await prepared("light")
    ctx.model_router = ModelRouter(
        ctx,
        choice_provider=lambda: selected["model"],
        is_subagent_child=True,
    )
    selected["model"] = "max"
    await _tick(state, ctx)

    assert events == []
    assert state.applied_choice == "light"


async def _streams(tmp_path):
    """История, итоги хода и смены модели треда прогона."""
    store = FileThreadStore(str(tmp_path / "threads"))
    key = ThreadKey(system="cli", thread="choice")
    return [
        [e.data for e in await store.read_stream(key, name)]
        for name in ("messages", "turns", "model_choices")
    ]


async def test_change_during_run_is_written_where_it_applied(
    tmp_path, monkeypatch
):
    """Запись встаёт за последним сообщением перед применением: после
    вывода тула первого шага, до ответа второго."""
    await _run(
        tmp_path,
        monkeypatch,
        "light",
        [_work(), _work(), _finish()],
        {1: "max"},
    )

    messages, _turns, (choice,) = await _streams(tmp_path)
    assert (
        choice["model"] == ModelChoiceRecord("max", "big", "fast").to_payload()
    )
    at = [m.get("id") for m in messages].index(choice["after"])
    assert messages[at].get("type") == "function_call_output"
    assert at + 1 < len(messages)


async def test_change_at_run_end_is_written_after_turn_outcome(
    tmp_path, monkeypatch
):
    """На завершении рана смена — за последним сообщением, как итог хода:
    у одного сообщения читатели ставят итог хода раньше черты."""
    await _run(tmp_path, monkeypatch, "light", [_finish()], {1: "max"})

    messages, (turn,), (choice,) = await _streams(tmp_path)
    assert (
        choice["model"] == ModelChoiceRecord("max", "big", "fast").to_payload()
    )
    assert choice["after"] == turn["after"] == messages[-1]["id"]


async def test_run_without_change_writes_no_record(tmp_path, monkeypatch):
    await _run(tmp_path, monkeypatch, "max", [_work(), _finish()], {})

    messages, turns, choices = await _streams(tmp_path)
    assert messages and turns
    assert choices == []


async def test_applied_choice_reaches_recorder_with_previous_choice(prepared):
    selected, ctx, state, _events = await prepared("light")
    records = []

    async def recorder(record):
        records.append(record)

    ctx.record_model_choice = recorder
    selected["model"] = "max"
    await _tick(state, ctx)
    await _tick(state, ctx)

    assert records == [ModelChoiceRecord("max", "big", "fast")]


async def test_subagent_calls_record_nothing(prepared):
    selected, ctx, state, _events = await prepared("light")
    records = []

    async def recorder(record):
        records.append(record)

    ctx.record_model_choice = recorder
    ctx.model_router = ModelRouter(
        ctx,
        choice_provider=lambda: selected["model"],
        is_subagent_child=True,
    )
    selected["model"] = "max"
    await _tick(state, ctx)

    assert records == []
