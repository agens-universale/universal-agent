"""Реплика посреди запуска не вытесняет ответы тулов и не обходит вызовы (спека
изображений §7.4, суженная планом).

К ответам тулов реплики дописываются; свежий ответ модели с вызовами очередь не
разбирает — вызовы исполняются, реплика ждёт следующего опроса; текстовый ответ
модели реплика подменяет, как раньше.
Очередь в тестах — drain, как у исполнителя (`AgentExecutor`) и у субагента
(`ChildInputScope.drain`).
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from gc_core import processing_responses as pr
from gc_core.loop_detector.constants import LOOP_WARN_KEY, READ_PROFILE
from gc_core.states import State
from gc_testkit import as_coro, run_coro
from gc_testkit.settings import fake_ctx
from helpers.turn_finish_apply import stub_turn_finish_apply_sync

_FINISH = {
    "id": "fc_finish",
    "type": "function_call",
    "call_id": "call_finish",
    "name": "turn_finish",
    "arguments": json.dumps(
        {
            "solution_type": "full_solution",
            "task_report": "done",
            "handoff": "{}",
        }
    ),
}
_HUMAN = {
    "role": "user",
    "content": "стоп, лучше посмотри b.py",
    "id": "m_mid",
}


def _read_call(call_id: str) -> dict:
    return {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "call_id": call_id,
        "name": "fs_read_file",
        "arguments": '{"path": "a.py"}',
    }


def _state() -> State:
    state = State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    state.backend_api_mode = "chat_completions"
    return state


def _ctx() -> SimpleNamespace:
    return fake_ctx(
        {
            "backend.api_mode": "chat_completions",
            "logging.console_trace": False,
            "agent.no_tool_call_max_count": 5,
            "agent.strict_turn_finish_validation": False,
        },
        tools_data={"fs_read_file": {LOOP_WARN_KEY: READ_PROFILE}},
        logger=logging.getLogger("test_pending_mid_run_batch"),
        auth_connector=None,
    )


class _Queue:
    """Очередь реплик: `drain` забирает всё, как `get_pending_messages` ядра."""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def drain(self) -> list[dict]:
        out = list(self.items)
        self.items.clear()
        return out


def _kinds(messages: list[dict]) -> list[str]:
    return [m.get("id") or m.get("type") for m in messages]


def _patch_tools(monkeypatch, on_tool=None) -> list[str]:
    executed: list[str] = []

    async def _apply(**kw):
        if kw["tool_name"] == "turn_finish":
            return stub_turn_finish_apply_sync(**kw)
        executed.append(kw["tool_name"])
        if on_tool is not None:
            on_tool()
        return json.dumps({"ok": True, "content": "print(1)"})

    monkeypatch.setattr(pr, "aapply_tool", _apply)
    monkeypatch.setattr(
        pr, "amaybe_update_intent_scope_from_tool", as_coro(lambda **_kw: None)
    )
    return executed


def test_human_after_tool_batch_keeps_outputs_and_tail(monkeypatch):
    """Реплика пришла, пока исполнялись тулы: ответы и напоминание записаны в state,
    за ними реплика; шаг — user_ask."""
    queue = _Queue()
    arrived: list = []

    def _arrive_once():
        if not arrived:
            arrived.append(True)
            queue.items.append(_HUMAN)

    _patch_tools(monkeypatch, on_tool=_arrive_once)
    batches: list = []

    async def _backend(messages, _state, _ctx):
        batches.append(list(messages))
        return [_FINISH], "resp_2"

    monkeypatch.setattr(pr, "aprocess_user_ask", _backend)
    monkeypatch.setattr(pr, "aprocess_tool_outputs", _backend)
    modes: list = []
    state = _state()

    result = run_coro(
        pr.arun_agent(
            [_read_call("c1"), _read_call("c2")],
            state,
            _ctx(),
            get_pending_messages=queue.drain,
            pre_backend_compaction_hook=lambda _msgs, mode: modes.append(mode),
        )
    )

    assert result == _FINISH["arguments"]
    assert modes == ["user_ask"]
    assert [m.get("type") or m.get("role") for m in batches[0]] == [
        "function_call_output",
        "function_call_output",
        "user",
        "user",
    ]
    assert "Loop detector:" in batches[0][2]["content"]
    assert batches[0][3] == _HUMAN
    assert _kinds(state.messages)[:2] == ["fc_c1", "fc_c2"]
    assert [m.get("type") for m in state.messages[2:4]] == [
        "function_call_output",
        "function_call_output",
    ]
    assert "Loop detector:" in state.messages[4]["content"]
    assert state.messages[5] == _HUMAN


def test_human_during_model_response_with_calls_waits_for_execution(
    monkeypatch,
):
    """Реплика пришла, пока модель отвечала вызовом: вызов исполнен, реплика —
    следующим шагом вместе с его ответом."""
    queue = _Queue()
    executed = _patch_tools(monkeypatch)
    batches: list = []

    async def _backend(messages, _state, _ctx):
        batches.append(list(messages))
        if len(batches) == 1:
            queue.items.append(_HUMAN)
            return [_read_call("c1")], "resp_1"
        return [_FINISH], "resp_2"

    monkeypatch.setattr(pr, "aprocess_user_ask", _backend)
    state = _state()

    result = run_coro(
        pr.arun_agent(
            [{"role": "user", "content": "прочитай a.py", "id": "m_start"}],
            state,
            _ctx(),
            get_pending_messages=queue.drain,
        )
    )

    assert result == _FINISH["arguments"]
    assert executed == ["fs_read_file"]
    assert [m.get("type") or m.get("role") for m in batches[1]] == [
        "function_call_output",
        "user",
    ]
    assert batches[1][1] == _HUMAN
    assert _kinds(state.messages)[:2] == ["m_start", "fc_c1"]
    assert state.messages[2]["type"] == "function_call_output"
    assert state.messages[3] == _HUMAN


def test_human_after_assistant_text_replaces_the_text(monkeypatch):
    """Реплика пришла, пока модель писала текст: подмена, как до правки, — к модели
    уходит одна реплика, текст ответа в state не попадает. Сужение спеки §7.4
    сознательное: дописанный к реплике ответ модели в responses ушёл бы в дельте
    повторно (он уже в цепочке сервера по previous_response_id)."""
    queue = _Queue()
    _patch_tools(monkeypatch)
    batches: list = []
    text = {
        "role": "assistant",
        "type": "message",
        "content": "Сейчас посмотрю.",
        "id": "m_text",
    }

    async def _backend(messages, _state, _ctx):
        batches.append(list(messages))
        if len(batches) == 1:
            queue.items.append(_HUMAN)
            return [text], "resp_1"
        return [_FINISH], "resp_2"

    monkeypatch.setattr(pr, "aprocess_user_ask", _backend)
    state = _state()

    result = run_coro(
        pr.arun_agent(
            [{"role": "user", "content": "прочитай a.py", "id": "m_start"}],
            state,
            _ctx(),
            get_pending_messages=queue.drain,
        )
    )

    assert result == _FINISH["arguments"]
    assert batches[1] == [_HUMAN]
    assert _kinds(state.messages)[:2] == ["m_start", "m_mid"]
    assert "m_text" not in _kinds(state.messages)
