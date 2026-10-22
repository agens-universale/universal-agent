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


def _child_ctx() -> SimpleNamespace:
    return SimpleNamespace(
        run_id="run_c1",
        parent_call_id="call_sub",
        subagent_progress_sink=None,
        logger=None,
    )


def _msg(text: str) -> dict:
    return {"role": "user", "content": text, "id": f"m_{text}"}


async def _arun_child(ctx, fake_run_agent) -> None:
    runner = SubagentRunner(ctx, _orchestrator_state(), _cfg())
    with patch.object(runner, "_build_child_ctx", return_value=_child_ctx()):
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
