"""Переполнение окна на запросе человека: сводка заменяет историю, запрос
доходит до модели в повторе — даже если после него в том же шаге записано
техническое сообщение (подсказка без ответа инструмента)."""

import pytest
from gc_core.agent_executor import AgentExecutor
from helpers.context_overflow import overflow_then_summarize
from parity_harness import (
    ScriptedBackend,
    assistant_tool_call,
    install,
    judge_accept,
    parity_config,
)


@pytest.fixture(autouse=True)
def _stub_turn_finish(monkeypatch):
    from gc_core import processing_responses as pr
    from helpers.turn_finish_apply import stub_turn_finish_apply

    real = pr.aapply_tool

    async def _apply(**kw):
        if kw.get("tool_name") == "turn_finish":
            return await stub_turn_finish_apply(**kw)
        return await real(**kw)

    monkeypatch.setattr(pr, "aapply_tool", _apply)


def _finish_turn():
    return [
        assistant_tool_call(
            "turn_finish",
            {
                "solution_type": "direct_answer",
                "task_report": "ok",
                "handoff": "{}",
            },
            call_id="c1",
        ),
        judge_accept(),
    ]


async def _context(tmp_path, monkeypatch, persist):
    """Контекст прогона: ``step`` — локальная запись, ``turn`` — запись
    durable-исполнителя (шаги копятся до точки сброса). Локально ``turn``
    запрещён только проверкой на входе: её здесь снимаем, остальной
    durable-контекст (порты Temporal) для записи не нужен."""
    from gc_core import agent_executor
    from gc_core import run_context as rc

    monkeypatch.setattr(
        agent_executor, "_require_supported_persist_mode", lambda *_: None
    )
    cfg = parity_config(
        tmp_path, monkeypatch, **{"thread_store.persist": persist}
    )
    return await rc.acreate_run_context(cfg)


def _emit_nudge_note(monkeypatch):
    """Подсказка без ответа инструмента — отдельное техническое сообщение,
    записанное после реплики человека в том же шаге."""
    from gc_core.message_policy import nudges

    real_fill = nudges.afill_nudge_buffer

    async def _fill(buffer, *args, **kwargs):
        await real_fill(buffer, *args, **kwargs)
        buffer.emit("NUDGE-NOTE")

    monkeypatch.setattr(nudges, "afill_nudge_buffer", _fill)


def _live(request):
    """Сообщения запроса к модели после сводки (системный промпт и сводка
    — первые два)."""
    messages = request["messages"]
    assert "dialog_summary" in messages[1]["content"]
    return messages[2:]


def _asks(live, text):
    return [m["role"] for m in live if m.get("content") == text]


@pytest.mark.parametrize("persist", ["step", "turn"])
@pytest.mark.parametrize("times", [1, 2, 3])
async def test_nudge_note_after_request_does_not_lose_it_on_overflow(
    tmp_path, monkeypatch, times, persist
):
    """Повтор после переполнения — тот же шаг: сколько бы раз окно ни
    переполнялось, запрос человека остаётся живым сообщением."""
    _emit_nudge_note(monkeypatch)
    backend = install(monkeypatch, ScriptedBackend(_finish_turn()))
    overflowed = overflow_then_summarize(monkeypatch, times=times)
    ctx = await _context(tmp_path, monkeypatch, persist)

    await AgentExecutor(ctx).arun(["hello there"], thread_id="t1")

    assert len(overflowed) == times
    assert _asks(_live(backend.calls[0]), "hello there") == ["user"]


@pytest.mark.parametrize("persist", ["step", "turn"])
async def test_queued_message_after_overflow_is_a_new_step(
    tmp_path, monkeypatch, persist
):
    """Реплика из очереди после переполнения — новый шаг: и при повторном
    переполнении она остаётся живым сообщением."""
    from gc_core import processing_responses as pr

    _emit_nudge_note(monkeypatch)
    backend = install(monkeypatch, ScriptedBackend(_finish_turn()))
    overflowed = overflow_then_summarize(monkeypatch, times=2)
    executor = None
    overflowing = pr.aprocess_user_ask

    async def _queue_on_first(messages, state, ctx):
        if not overflowed:
            executor.add_message({"role": "user", "content": "second q"})
        return await overflowing(messages, state, ctx)

    monkeypatch.setattr(pr, "aprocess_user_ask", _queue_on_first)
    ctx = await _context(tmp_path, monkeypatch, persist)
    executor = AgentExecutor(ctx)

    await executor.arun(["hello there"], thread_id="t1")

    assert len(overflowed) == 2
    assert _asks(_live(backend.calls[0]), "second q") == ["user"]
