"""Переполнение окна на запросе человека: сводка заменяет историю, запрос
доходит до модели в повторе — даже если после него в том же шаге записано
техническое сообщение (подсказка без ответа инструмента)."""

import pytest
from gc_core.agent_executor import AgentExecutor
from parity_harness import (
    ScriptedBackend,
    assistant_tool_call,
    install,
    judge_accept,
    parity_config,
)

OVERFLOW = (
    "Requested token count exceeds the model's maximum context "
    "length of 131072 tokens. Please reduce the number of tokens"
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


def overflow_once_then_summarize(monkeypatch):
    """Первый запрос к модели — переполнение окна; сводка — настоящая
    компакция с подменённым вызовом суммаризатора."""
    from gc_core import processing_responses as pr

    real = pr.aprocess_user_ask
    overflowed = []

    async def _overflow_once(messages, state, ctx):
        if not overflowed:
            overflowed.append(True)
            raise ValueError(OVERFLOW)
        return await real(messages, state, ctx)

    async def _summarize(**_kwargs):
        return {
            "raw_summary": "<task_and_context>итог</task_and_context>",
            "num_batches": 1,
            "last_dialog_steps": "",
            "trace": {"memory_patch": {"executed": False}},
        }

    monkeypatch.setattr(pr, "aprocess_user_ask", _overflow_once)
    monkeypatch.setattr(
        "gc_core.summarize.compaction.asummarize_dialog_via_chat_completions",
        _summarize,
    )
    return overflowed


async def test_nudge_note_after_request_does_not_lose_it_on_overflow(
    tmp_path, monkeypatch
):
    from gc_core import run_context as rc
    from gc_core.message_policy import nudges

    real_fill = nudges.afill_nudge_buffer

    async def _fill(buffer, *args, **kwargs):
        await real_fill(buffer, *args, **kwargs)
        buffer.emit("NUDGE-NOTE")

    monkeypatch.setattr(nudges, "afill_nudge_buffer", _fill)
    backend = install(monkeypatch, ScriptedBackend(_finish_turn()))
    overflowed = overflow_once_then_summarize(monkeypatch)
    ctx = await rc.acreate_run_context(parity_config(tmp_path, monkeypatch))

    await AgentExecutor(ctx).arun(["hello there"], thread_id="t1")

    assert overflowed == [True]
    retried = backend.calls[0]["messages"]
    assert "dialog_summary" in retried[1]["content"]
    asks = [m for m in retried if m.get("content") == "hello there"]
    assert [m["role"] for m in asks] == ["user"]
