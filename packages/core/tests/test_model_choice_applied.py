"""Применённый выбор рана и живое событие его смены (спека ручки модели §5.2).

Ран — настоящий исполнитель и цикл, скриптуется только ответ модели. Точка
подготовки вызова проверяется напрямую: ретрай того же такта — повторная
подготовка без закрытия такта.
"""

import json
import logging

from gc_core import processing_responses as pr
from gc_core.agent_executor import AgentExecutor
from gc_core.auto_routing import state_ops
from gc_core.auto_routing.policy import Decision
from gc_core.model_choice import ModelChoiceApplied
from gc_core.model_router import ModelRouter
from gc_core.persistence.model_choice_record import (
    ModelChoiceRecord,
    parse_model_choice,
)
from gc_core.run_context import acreate_run_context
from gc_core.states import State
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
    n-го запроса; `stop_on` — номер запроса, во время которого человек
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
        cfg, choice_provider=lambda: selected["model"]
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


async def _prepared(tmp_path, monkeypatch, initial):
    """Главный трейс у точки подготовки: выбор, контекст, State, события.
    Применённый выбор — как на старте рана."""
    selected = {"model": initial}
    cfg = parity_config(tmp_path, monkeypatch, **CONFIG)
    ctx = await acreate_run_context(
        cfg, choice_provider=lambda: selected["model"]
    )
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


async def test_two_changes_before_next_call_emit_only_the_last(
    tmp_path, monkeypatch
):
    selected, ctx, state, events = await _prepared(
        tmp_path, monkeypatch, "light"
    )
    await _tick(state, ctx)
    selected["model"] = "max"
    selected["model"] = "mid"
    await _tick(state, ctx)

    assert events == [ModelChoiceApplied("mid", "mid")]
    assert state.applied_choice == "mid"


async def test_event_names_the_choice_of_the_prepared_call(
    tmp_path, monkeypatch
):
    selected, ctx, state, events = await _prepared(
        tmp_path, monkeypatch, "light"
    )
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


async def test_retry_of_same_tick_emits_nothing(tmp_path, monkeypatch):
    selected, ctx, state, events = await _prepared(
        tmp_path, monkeypatch, "light"
    )
    await pr.aprepare_main_call(state, ctx)
    selected["model"] = "max"
    retried = await pr.aprepare_main_call(state, ctx)

    assert retried.alias == "fast"
    assert events == []
    state_ops.close_tick(state, state._call_level)
    await _tick(state, ctx)
    assert events == [ModelChoiceApplied("max", "big")]


async def test_auto_internal_level_change_emits_nothing(tmp_path, monkeypatch):
    selected, ctx, state, events = await _prepared(
        tmp_path, monkeypatch, "auto"
    )
    levels = iter(["light", "max", "default"])
    monkeypatch.setattr(
        pr,
        "two_level_decision",
        lambda *a, **k: Decision(next(levels), "test", (), True),
    )
    aliases = [(await _tick(state, ctx)).alias for _ in range(3)]

    assert aliases == ["fast", "big", "mid"]
    assert events == []


async def test_manual_same_model_as_auto_decision_is_a_change(
    tmp_path, monkeypatch
):
    selected, ctx, state, events = await _prepared(
        tmp_path, monkeypatch, "auto"
    )
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


async def test_failing_choice_hook_mid_run_does_not_abort_the_call(
    tmp_path, monkeypatch
):
    """Хук события посреди рана бросает — вызов готовится той моделью,
    выбор считается применённым (им ран и работает), сбой — в лог."""
    selected, ctx, state, _events = await _prepared(
        tmp_path, monkeypatch, "light"
    )
    ctx.on_model_choice_applied = _raise
    records = _Records(ctx.logger)
    await _tick(state, ctx)
    selected["model"] = "max"
    resolved = await _tick(state, ctx)

    assert resolved.alias == "big"
    assert state.applied_choice == "max"
    assert records.messages.count("model_choice_applied_failed") == 1


async def test_failing_choice_hook_does_not_skip_run_teardown(
    tmp_path, monkeypatch
):
    """Хук события на завершении бросает — запись итога и уборка рана
    всё равно выполняются, ран завершается штатно."""
    calls = []

    records = []

    def spy(executor):
        records.append(_Records(executor._ctx.logger))
        record = executor._arecord_turn_outcome
        teardown = executor._ateardown_run

        async def arecord(*args, **kwargs):
            calls.append("record")
            return await record(*args, **kwargs)

        async def ateardown(*args, **kwargs):
            calls.append("teardown")
            return await teardown(*args, **kwargs)

        executor._arecord_turn_outcome = arecord
        executor._ateardown_run = ateardown

    await _run(
        tmp_path,
        monkeypatch,
        "light",
        [_finish()],
        {1: "max"},
        hook=_raise,
        executor_spy=spy,
    )

    assert calls[-2:] == ["record", "teardown"]
    assert "model_choice_applied_failed" in records[0].messages


async def test_subagent_calls_emit_nothing(tmp_path, monkeypatch):
    selected, ctx, state, events = await _prepared(
        tmp_path, monkeypatch, "light"
    )
    ctx.model_router = ModelRouter(
        ctx,
        choice_provider=lambda: selected["model"],
        is_subagent_child=True,
    )
    selected["model"] = "max"
    await _tick(state, ctx)

    assert events == []
    assert state.applied_choice == "light"


def _thread_rows(tmp_path):
    path = tmp_path / "threads" / "data_cli_choice.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def _choice_rows(rows):
    return [row for row in rows if row["type"] == "model_choice"]


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

    rows = _thread_rows(tmp_path)
    (record,) = _choice_rows(rows)
    at = rows.index(record)
    before = [row for row in rows[:at] if row["type"] == "message"]
    assert parse_model_choice(record) == ModelChoiceRecord(
        "max", "big", "fast"
    )
    assert record["seq"] == before[-1]["seq"]
    assert before[-1]["payload"].get("type") == "function_call_output"
    assert rows[at + 1]["type"] == "message"


async def test_change_at_run_end_is_written_after_turn_outcome(
    tmp_path, monkeypatch
):
    await _run(tmp_path, monkeypatch, "light", [_finish()], {1: "max"})

    rows = _thread_rows(tmp_path)
    assert [row["type"] for row in rows[-2:]] == [
        "turn_outcome",
        "model_choice",
    ]
    assert parse_model_choice(rows[-1]) == ModelChoiceRecord(
        "max", "big", "fast"
    )
    assert rows[-1]["seq"] == rows[-2]["seq"]


async def test_run_without_change_writes_no_record(tmp_path, monkeypatch):
    await _run(tmp_path, monkeypatch, "max", [_work(), _finish()], {})

    assert _choice_rows(_thread_rows(tmp_path)) == []


async def test_applied_choice_reaches_recorder_with_previous_choice(
    tmp_path, monkeypatch
):
    selected, ctx, state, _events = await _prepared(
        tmp_path, monkeypatch, "light"
    )
    records = []

    async def recorder(record):
        records.append(record)

    ctx.record_model_choice = recorder
    selected["model"] = "max"
    await _tick(state, ctx)
    await _tick(state, ctx)

    assert records == [ModelChoiceRecord("max", "big", "fast")]


async def test_subagent_calls_record_nothing(tmp_path, monkeypatch):
    selected, ctx, state, _events = await _prepared(
        tmp_path, monkeypatch, "light"
    )
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
