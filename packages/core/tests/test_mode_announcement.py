"""Объявление режима каждому агенту: перед каждым запросом к модели.

Режим один на сессию и общий живой; отметка «что этому агенту объявлено» —
своя у каждого агента. У корня она в общем состоянии (мета треда), у
субагента — в его собственном состоянии.
"""

from __future__ import annotations

import json

import pytest
from gc_core import processing_responses as pr
from gc_core.execution.state import ExecutionState, SharedStateData
from gc_core.message_policy.plan_messages import (
    PLAN_ENTER_MESSAGE,
    PLAN_RESEARCH_ENTER_MESSAGE,
)
from gc_core.message_policy.user_message_policy import (
    wrap_technical_user_content,
)
from gc_core.processing_responses import arun_agent
from gc_core.security.permission_store import PermissionStore
from gc_core.states import State
from gc_core.tools.registry import get_builtin_tools_data
from gc_testkit import run_coro
from gc_testkit.settings import fake_ctx, make_settings


def _state(shared, *, parent_state_id=None):
    state = State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=5,
        system_prompt_id="v1",
        use_server_subagents=False,
        parent_state_id=parent_state_id,
    )
    state.shared = shared
    return state


def _ctx(shared):
    return fake_ctx(
        {
            "backend.api_mode": "chat_completions",
            "agent.strict_turn_finish_validation": False,
            "agent.no_tool_call_max_count": 5,
        },
        logger=None,
        auth_connector=None,
        permission_store=PermissionStore(shared.confirm_rules, shared=shared),
        # Настоящий реестр: тул плана в нём есть, корень его видит, ребёнок
        # — нет (`tools_for_mode`); от этого зависит текст объявления plan.
        tools_data=get_builtin_tools_data(make_settings()),
    )


class _Requested(Exception):
    """Цикл дошёл до запроса к модели; ``messages`` — что ушло бы в него."""

    def __init__(self, messages):
        super().__init__("requested")
        self.messages = messages


def _first_request(monkeypatch, messages, state, ctx):
    async def _request(sent, _state, _ctx):
        raise _Requested(list(sent))

    monkeypatch.setattr(pr, "aprocess_user_ask", _request)
    monkeypatch.setattr(pr, "aprocess_tool_outputs", _request)
    with pytest.raises(_Requested) as caught:
        run_coro(arun_agent(messages, state, ctx))
    return caught.value.messages


def _user(text, message_id):
    return {"role": "user", "content": text, "id": message_id}


def test_each_agent_keeps_its_own_mark_and_the_root_one_lives_in_shared():
    shared = SharedStateData()
    root = _state(shared)
    child = _state(shared, parent_state_id="root")
    root.mode_announced = "auto"
    child.mode_announced = "plan"
    assert shared.mode_announced == "auto"  # корень — в мете сессии
    assert (root.mode_announced, child.mode_announced) == ("auto", "plan")
    restored = ExecutionState.from_execution_snapshot(
        child.to_execution_snapshot()
    )
    assert restored.mode_announced == "plan"


def test_root_is_told_the_mode_at_start(monkeypatch):
    shared = SharedStateData(permission_mode="plan")
    state = _state(shared)
    sent = _first_request(
        monkeypatch, [_user("задача", "u1")], state, _ctx(shared)
    )
    assert sent[0]["content"] == "задача"
    assert sent[-1]["content"] == wrap_technical_user_content(
        PLAN_ENTER_MESSAGE
    )
    assert sent[-1] in state.messages  # ушло и в историю
    assert shared.mode_announced == "plan"


def test_child_is_told_the_mode_at_start_and_keeps_the_root_mark(
    monkeypatch,
):
    shared = SharedStateData(permission_mode="plan", mode_announced="auto")
    child = _state(shared, parent_state_id="root")
    sent = _first_request(
        monkeypatch, [_user("подзадача", "c1")], child, _ctx(shared)
    )
    # У ребёнка нет тула плана: «исследуй и верни находки текстом».
    assert sent[-1]["content"] == wrap_technical_user_content(
        PLAN_RESEARCH_ENTER_MESSAGE
    )
    assert child.mode_announced == "plan"
    assert shared.mode_announced == "auto"  # отметку корня ребёнок не тронул


_READ = {
    "id": "id_c1",
    "type": "function_call",
    "call_id": "c1",
    "name": "fs_read_file",
    "arguments": '{"path": "a.py"}',
}


def _switch_mode_during_the_tool(monkeypatch, shared, mode):
    async def fake_apply(**kw):
        shared.permission_mode = mode  # Shift+Tab, пока шёл тул
        return json.dumps({"tool": kw["tool_name"], "ok": True})

    async def _noop(**_kw):
        return None

    monkeypatch.setattr(pr, "aapply_tool", fake_apply)
    monkeypatch.setattr(
        pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res
    )
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", _noop)


@pytest.mark.parametrize(
    "parent_state_id, told",
    [(None, PLAN_ENTER_MESSAGE), ("root", PLAN_RESEARCH_ENTER_MESSAGE)],
)
def test_mode_switched_during_a_batch_is_told_on_the_next_request(
    monkeypatch, parent_state_id, told
):
    shared = SharedStateData(permission_mode="edit")
    state = _state(shared, parent_state_id=parent_state_id)
    _switch_mode_during_the_tool(monkeypatch, shared, "plan")
    sent = _first_request(monkeypatch, [dict(_READ)], state, _ctx(shared))
    assert sent[0]["type"] == "function_call_output"
    assert sent[-1]["content"] == wrap_technical_user_content(told)
    assert state.mode_announced == "plan"


def test_the_text_follows_the_tool_set_not_the_role(monkeypatch):
    """Корень без тула плана в реестре (тул не собран) получает текст
    «верни находки текстом», как ребёнок."""
    shared = SharedStateData(permission_mode="plan")
    state = _state(shared)
    ctx = _ctx(shared)
    ctx.tools_data = {
        name: entry
        for name, entry in ctx.tools_data.items()
        if name != "turn_propose_plan"
    }
    sent = _first_request(monkeypatch, [_user("задача", "u1")], state, ctx)
    assert sent[-1]["content"] == wrap_technical_user_content(
        PLAN_RESEARCH_ENTER_MESSAGE
    )


def test_a_stop_before_the_request_leaves_no_announcement(monkeypatch):
    """Стоп во время подготовки запроса (судья пары, сжатие): объявления в
    истории нет и отметка не сдвинута — следующий прогон скажет о режиме."""
    shared = SharedStateData(permission_mode="plan")
    state = _state(shared)
    stop = {"requested": False}

    async def _request(sent, _state, _ctx):
        raise AssertionError("основной запрос после стопа")

    monkeypatch.setattr(pr, "aprocess_user_ask", _request)

    def compaction(_messages, _mode):
        stop["requested"] = True

    result = run_coro(
        arun_agent(
            [_user("задача", "u1")],
            state,
            _ctx(shared),
            check_stop=lambda: stop["requested"],
            pre_backend_compaction_hook=compaction,
        )
    )
    assert result is None
    notice = wrap_technical_user_content(PLAN_ENTER_MESSAGE)
    assert all(m.get("content") != notice for m in state.messages)
    assert shared.mode_announced is None


def test_the_announcement_is_the_last_message_before_the_request(
    monkeypatch,
):
    """Объявление режима — последним, после подсказки об инструментах: так
    его не опередит ни одна вставка подготовки запроса."""
    shared = SharedStateData(permission_mode="plan")
    state = _state(shared)
    hint = {"role": "user", "content": "подсказка об инструментах"}

    def _with_hint(messages, *_args, **_kwargs):
        return [*messages, hint]

    async def _request(sent, _state, _ctx):
        raise _Requested(list(sent))

    monkeypatch.setattr(pr, "apply_tool_usage_notice_before_llm", _with_hint)
    monkeypatch.setattr(pr, "aprocess_user_ask", _request)
    with pytest.raises(_Requested) as caught:
        run_coro(
            arun_agent(
                [_user("задача", "u1")], state, _ctx(shared), session_meta={}
            )
        )
    sent = caught.value.messages
    assert sent[-2] == hint
    assert sent[-1]["content"] == wrap_technical_user_content(
        PLAN_ENTER_MESSAGE
    )
