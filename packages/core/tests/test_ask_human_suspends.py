"""Приостановка хода на вопросе (``ask_human_suspends``).

Канал без живого человека (A2A): вопрос модели (``turn_ask_human``) не ждёт
ответа в процессе, а завершает ход — история с вызовом без ответа,
``SessionState.pending_question``, аренда отпущена. Следующий ход с ответом
(``arun(..., answer=…)``) кладёт ответ выводом того же вызова и продолжает
цикл — на любом инстансе и после рестарта. Ход без ответа закрывает вопрос
«без ответа», пары вызов–результат целы.
"""

import json

import pytest
from gc_core.aio import AsyncAgent
from gc_core.threads.models import ThreadKey
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore
from parity_harness import (
    ScriptedBackend,
    assistant_tool_call,
    install,
    judge_accept,
    parity_config,
)

KEY = ThreadKey(system="sdk", thread="t-ask")
QUESTION = "A или B?"
ASK = assistant_tool_call(
    "turn_ask_human", {"question": QUESTION}, call_id="ask1"
)
FINISH = assistant_tool_call(
    "turn_finish",
    {
        "solution_type": "direct_answer",
        "task_report": "сделано по варианту",
        "handoff": "{}",
    },
    call_id="fin1",
)


@pytest.fixture(autouse=True)
def _real_ask_stub_finish(monkeypatch):
    """Вопрос — настоящий тул; финал — заглушка (как в приёмке SDK)."""
    from gc_core import processing_responses as pr
    from helpers.turn_finish_apply import stub_turn_finish_apply

    real = pr.aapply_tool

    async def apply(**kwargs):
        if kwargs.get("tool_name") == "turn_ask_human":
            return await real(**kwargs)
        return await stub_turn_finish_apply(**kwargs)

    monkeypatch.setattr(pr, "aapply_tool", apply)


async def _agent(cfg, store):
    return await AsyncAgent.create(
        cfg, thread_store=store, ask_human_suspends=True
    )


def _outputs(payload: dict) -> list[dict]:
    return [m for m in payload.get("messages", []) if m.get("role") == "tool"]


async def test_question_ends_the_turn_and_waits_in_the_session(
    tmp_path, monkeypatch
):
    cfg = parity_config(tmp_path, monkeypatch)
    backend = install(monkeypatch, ScriptedBackend([ASK]))
    store = MemoryThreadStore()
    async with await _agent(cfg, store) as agent:
        await agent.run("выбери вариант", thread_id=KEY.thread)
        assert agent.executor.last_terminal_outcome == "input_required"
        assert agent.pending_question == {
            "call_id": "ask1",
            "question": QUESTION,
        }
    backend.assert_clean()
    record = await store.get_thread(KEY)
    assert record.state.pending_question == {
        "call_id": "ask1",
        "question": QUESTION,
    }
    assert record.lease is None, "аренда держится, хотя ход ждёт ответа"
    history = [e.data for e in await store.read_stream(KEY, "messages")]
    assert history[-1].get("type") == "function_call"
    assert history[-1].get("call_id") == "ask1"
    assert not any(
        m.get("type") == "function_call_output" and m.get("call_id") == "ask1"
        for m in history
    )


async def test_answer_continues_the_turn_after_a_restart(
    tmp_path, monkeypatch
):
    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend([ASK]))
    store = MemoryThreadStore()
    async with await _agent(cfg, store) as agent:
        await agent.run("выбери вариант", thread_id=KEY.thread)
    # Другой процесс (рестарт): ответ ложится выводом того же вызова.
    backend = install(monkeypatch, ScriptedBackend([FINISH, judge_accept()]))
    async with await _agent(cfg, store) as agent:
        result = await agent.run([], answer="B", thread_id=KEY.thread)
        assert agent.pending_question is None
    assert result
    backend.assert_clean()
    [answer] = [
        o for o in _outputs(backend.calls[0]) if o["tool_call_id"] == "ask1"
    ]
    body = json.loads(answer["content"])
    assert (body["question"], body["response"]) == (QUESTION, "B")
    record = await store.get_thread(KEY)
    assert record.state.pending_question is None


async def test_new_message_without_an_answer_closes_the_question(
    tmp_path, monkeypatch
):
    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend([ASK]))
    store = MemoryThreadStore()
    async with await _agent(cfg, store) as agent:
        await agent.run("выбери вариант", thread_id=KEY.thread)
        backend = install(
            monkeypatch, ScriptedBackend([FINISH, judge_accept()])
        )
        await agent.run("забудь, сделай по-своему", thread_id=KEY.thread)
    backend.assert_clean()
    [closed] = [
        o for o in _outputs(backend.calls[0]) if o["tool_call_id"] == "ask1"
    ]
    assert json.loads(closed["content"])["ok"] is False
    record = await store.get_thread(KEY)
    assert record.state.pending_question is None


async def test_subagent_does_not_suspend_the_parent_turn():
    """Ход родителя посреди батча субагента не приостановить: вопрос из
    субагента — отказ, а не приостановка."""
    from types import SimpleNamespace

    from gc_core.tools.turn.ask_human import AskHuman
    from gc_testkit.settings import fake_ctx

    ctx = fake_ctx(
        {},
        logger=None,
        ask_human_suspends=True,
        parent_call_id="parent-1",
        subagent_tool_name="subagent_worker",
        subagent_progress_sink=None,
    )
    state = SimpleNamespace(pending_question=None)
    tool = AskHuman(ctx=ctx, state=state, call_id="ask-child")
    out = json.loads(await tool.acall(question=QUESTION))
    assert out["ok"] is False
    assert out["error"]["type"] == "question_unavailable"
    assert state.pending_question is None


async def test_answer_is_not_replayed_into_the_tail_turn(
    tmp_path, monkeypatch
):
    """Хвост хода с ответом (``send`` на излёте) доигрывается своим ходом
    без ответа: вопроса уже нет, повтор ответа уронил бы весь ``run``."""
    import importlib

    ae = importlib.import_module("gc_core.agent_executor")
    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend([ASK]))
    store = MemoryThreadStore()
    async with await _agent(cfg, store) as agent:
        await agent.run("выбери вариант", thread_id=KEY.thread)
        tail_finish = assistant_tool_call(
            "turn_finish",
            {
                "solution_type": "direct_answer",
                "task_report": "и это сделано",
                "handoff": "{}",
            },
            call_id="fin2",
        )
        backend = install(
            monkeypatch,
            ScriptedBackend(
                [FINISH, judge_accept(), tail_finish, judge_accept()]
            ),
        )
        real_teardown = ae._aclose_mcp_and_release
        late = [{"role": "user", "content": "и ещё", "id": "m-tail"}]

        async def teardown(owner_reset):
            if late:
                agent.send(late.pop())
            await real_teardown(owner_reset)

        monkeypatch.setattr(ae, "_aclose_mcp_and_release", teardown)
        await agent.run([], answer="B", thread_id=KEY.thread)
    backend.assert_clean()
