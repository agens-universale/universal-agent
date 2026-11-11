"""Реплика субагенту, пришедшая в окне его завершения, достаётся родителю.

Пока работает субагент, `AgentExecutor.add_message` кладёт реплику в его
`ChildInputScope`. Реплика, пришедшая ПОСЛЕ последнего забора очереди ребёнком
и ДО выхода из `activate_child_input`, оставалась в очереди скоупа и пропадала
вместе с ним: ни родителю, ни в хвост прогона.

Двойник субагента ниже забирает очередь (первая реплика — его), а вторая
приходит уже после забора — ровно в это окно.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import patch

from gc_core.agent_executor import AgentExecutor
from gc_core.states import SharedStateData, State
from gc_core.tools.subagents.runner import SubagentRunner
from gc_testkit.develop_files import read_subagent_trace
from gc_testkit.settings import fake_ctx


def _orchestrator_state() -> State:
    state = State(
        verbose=False,
        model="test-model",
        tools_spec=[{"name": "fs_read_file"}],
        tools_descr=["read desc"],
        skills_list=[],
        max_steps=100,
        system_prompt_id="test-prompt",
        use_server_subagents=False,
        chat_completions_system_prompt="System\n<specific_role>x</specific_role>",
        shared=SharedStateData(),
    )
    state.backend_api_mode = "chat_completions"
    return state


def _cfg() -> dict:
    return {
        "agent.use_local_subagents": True,
        "backend.api_mode": "chat_completions",
        "paths.skills_data_path": "",
        "agent.model": "test-model",
        "agent.system_prompt_id": "test-prompt",
        "agent.subagent_max_steps": 30,
    }


def _ctx():
    return fake_ctx(
        _cfg(),
        auth_connector=None,
        logger=None,
        logging_path="",
        summary_trace_path="",
        tools_data={},
        tools_spec=[],
        tools_description=[],
        subagent_progress_sink=None,
        threads_dir=None,
        system_id=None,
        thread_id=None,
    )


def _child_ctx(**extra) -> SimpleNamespace:
    return SimpleNamespace(
        run_id="run_c1",
        parent_call_id="call_sub",
        subagent_progress_sink=None,
        logger=None,
        **extra,
    )


def _msg(text: str) -> dict:
    return {"role": "user", "content": text, "id": f"m_{text}"}


async def _arun_child(ctx, fake_run_agent, child_ctx=None) -> None:
    runner = SubagentRunner(ctx, _orchestrator_state(), _cfg())
    child_ctx = child_ctx or _child_ctx()
    with patch.object(runner, "_build_child_ctx", return_value=child_ctx):
        await runner.arun(
            "subagent_worker",
            {"task": "test"},
            fake_run_agent,
            parent_call_id="call_sub",
        )


def test_message_after_last_child_drain_reaches_parent_next_step():
    """Пришедшее после последнего забора ребёнка — родителю, без дублей."""
    ctx = _ctx()
    ex = AgentExecutor(ctx)
    drained: list = []

    async def fake_run_agent(messages, child_state, child_ctx, **kwargs):
        ex.add_message(_msg("ребёнку"))
        drained.extend(await kwargs["get_pending_messages"]())
        # Последний забор позади: ребёнок закрывает ход, а человек пишет.
        ex.add_message(_msg("в окно 1"))
        ex.add_message(_msg("в окно 2"))
        return "done"

    asyncio.run(_arun_child(ctx, fake_run_agent))

    assert [m["content"] for m in drained] == ["ребёнку"]
    next_step = asyncio.run(ex._atake_pending_messages())
    assert [m["content"] for m in next_step] == ["в окно 1", "в окно 2"], (
        "реплика из окна завершения субагента потерялась вместе со скоупом "
        "или забранное ребёнком досталось родителю второй раз"
    )


def test_late_enqueue_through_stale_scope_reaches_parent_in_order():
    """Поток, взявший скоуп до его закрытия, кладёт реплику уже после — она
    уходит родителю вслед за остатком, а не в мёртвую очередь."""
    ctx = _ctx()
    ex = AgentExecutor(ctx)
    stale: dict = {}

    async def fake_run_agent(messages, child_state, child_ctx, **kwargs):
        stale["scope"] = ctx.child_input_scope
        ex.add_message(_msg("остаток"))
        return "done"

    asyncio.run(_arun_child(ctx, fake_run_agent))
    assert ctx.child_input_scope is None

    t = threading.Thread(
        target=stale["scope"].enqueue, args=(_msg("опоздавшая"),)
    )
    t.start()
    t.join(timeout=2)
    ex.add_message(_msg("после"))

    next_step = asyncio.run(ex._atake_pending_messages())
    assert [m["content"] for m in next_step] == [
        "остаток",
        "опоздавшая",
        "после",
    ]


def test_message_handed_to_parent_is_not_in_child_trace(tmp_path):
    """Трасса ребёнка — только то, что он забрал.

    Запись в трассу делалась при ПОСТАНОВКЕ в очередь, поэтому реплика из окна
    завершения оставалась в треде ребёнка как обращённая к нему — без ответа,
    висящей при возобновлении, — хотя досталась родителю.
    """
    ctx = _ctx()
    ex = AgentExecutor(ctx)
    child_ctx = _child_ctx(
        threads_dir=str(tmp_path), system_id="cli", thread_id="task-left"
    )

    async def fake_run_agent(messages, child_state, child_ctx, **kwargs):
        ex.add_message(_msg("ребёнку"))
        await kwargs["get_pending_messages"]()
        ex.add_message(_msg("в окно"))
        return "done"

    asyncio.run(_arun_child(ctx, fake_run_agent, child_ctx))

    rows = read_subagent_trace(str(tmp_path), "cli", "task-left")
    assert [
        r["payload"]["content"] for r in rows if r.get("kind") == "user"
    ] == ["ребёнку"], "реплика, ушедшая родителю, записана за субагентом"
    next_step = asyncio.run(ex._atake_pending_messages())
    assert [m["content"] for m in next_step] == ["в окно"]


def test_trace_failure_does_not_drop_drained_messages(caplog):
    ctx = _ctx()
    executor = AgentExecutor(ctx)
    drained = []

    async def persist(*args):
        raise OSError("disk unavailable")

    async def fake_run(messages, state, child, **kwargs):
        executor.add_message(_msg("first"))
        executor.add_message(_msg("second"))
        drained.extend(await kwargs["get_pending_messages"]())
        return "done"

    with patch("gc_core.tools.subagents.runner._apersist_child_user", persist):
        asyncio.run(_arun_child(ctx, fake_run))
    assert [message["content"] for message in drained] == ["first", "second"]
    assert caplog.text.count("subagent_trace_write_failed") == 2


def test_unconsumed_messages_are_not_shown_in_child_feed(monkeypatch):
    ctx = _ctx()
    executor = AgentExecutor(ctx)
    shown = []

    def emit(ctx, state, kind, **details):
        if kind == "user_message":
            shown.append(details["prose"])

    monkeypatch.setattr("gc_core.tools.subagents.runner.emit_progress", emit)

    async def fake_run(messages, state, child, **kwargs):
        executor.add_message(_msg("consumed"))
        await kwargs["get_pending_messages"]()
        executor.add_message(_msg("parent"))
        return "done"

    asyncio.run(_arun_child(ctx, fake_run))
    assert shown == ["consumed"]
    assert [
        m["content"] for m in asyncio.run(executor._atake_pending_messages())
    ] == ["parent"]
