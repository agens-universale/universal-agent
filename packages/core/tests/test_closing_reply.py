"""Текст ответа, закрывшего ход, доходит до исполнителя (спека конца хода).

Скриптуется только HTTP-ответ модели; исполнитель, цикл и тулы настоящие.
"""

import json
from copy import deepcopy

import httpx
from gc_core.agent_executor import AgentExecutor
from gc_core.execution.state import ExecutionState
from gc_core.run_context import acreate_run_context
from gc_core.states import State
from gc_core.utils import connect_backend
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
CONFIG = {
    "agent.max_steps": 15,
    "agent.strict_turn_finish_validation": False,
    "text_answer_mode": "deny",
    "intent_scope.tools": {},
}


def _with_text(turn, text):
    turn = deepcopy(turn)
    turn["choices"][0]["message"]["content"] = text
    return turn


def _finish(text=None, call_id="finish"):
    turn = assistant_tool_call("turn_finish", FINISH_ARGS, call_id)
    return _with_text(turn, text) if text else turn


def _work(text):
    turn = assistant_tool_call("exec_calculator", {"expression": "3 * 4"})
    return _with_text(turn, text)


async def _run(tmp_path, monkeypatch, turns, **config):
    cfg = parity_config(tmp_path, monkeypatch, **{**CONFIG, **config})
    monkeypatch.chdir(tmp_path)
    backend = install(monkeypatch, ScriptedBackend(turns))
    ctx = await acreate_run_context(cfg)
    executor = AgentExecutor(ctx)
    try:
        result = await executor.arun(
            ["Выполни контрольную задачу"],
            thread_id="closing",
            system="cli",
            start_new_thread=True,
            message_ids=["input-0"],
        )
    finally:
        release_run_logger(ctx.logger)
    backend.assert_clean()
    return executor, result


async def test_closing_reply_holds_text_of_finishing_response(
    tmp_path, monkeypatch
):
    executor, result = await _run(tmp_path, monkeypatch, [_finish("Пока!")])

    assert executor.last_closing_reply == "Пока!"
    assert json.loads(result)["task_report"] == "Проверка завершена"


async def test_closing_reply_empty_when_finish_response_has_no_text(
    tmp_path, monkeypatch
):
    executor, _ = await _run(
        tmp_path, monkeypatch, [_work("Смотрю"), _finish()]
    )

    assert executor.last_closing_reply == ""


def _responses_turn(*items):
    return {
        "id": "resp",
        "output": list(items),
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }


def _install_responses(monkeypatch, turns):
    """SSE-ответы режима responses: последний чанк — response.completed."""
    queue = list(turns)

    def handler(_request):
        response = queue.pop(0)
        event = {"type": "response.completed", "response": response}
        return httpx.Response(
            200,
            text=f"data: {json.dumps(event, ensure_ascii=False)}\n\n",
            headers={"content-type": "text/event-stream"},
        )

    monkeypatch.setattr(
        connect_backend,
        "get_async_client",
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return queue


async def test_closing_reply_in_responses_backend_mode(tmp_path, monkeypatch):
    cfg = parity_config(
        tmp_path,
        monkeypatch,
        **CONFIG,
        **{
            "backend.api_mode": "responses",
            "backend.completions_url": "http://parity.invalid/v1/responses",
        },
    )
    monkeypatch.chdir(tmp_path)
    queue = _install_responses(
        monkeypatch,
        [
            _responses_turn(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Пока!"}],
                },
                {
                    "type": "function_call",
                    "call_id": "finish",
                    "name": "turn_finish",
                    "arguments": json.dumps(FINISH_ARGS, ensure_ascii=False),
                },
            )
        ],
    )
    ctx = await acreate_run_context(cfg)
    executor = AgentExecutor(ctx)
    try:
        result = await executor.arun(
            ["Выполни контрольную задачу"],
            thread_id="closing",
            system="cli",
            start_new_thread=True,
            message_ids=["input-0"],
        )
    finally:
        release_run_logger(ctx.logger)

    assert queue == []
    assert executor.last_closing_reply == "Пока!"
    assert json.loads(result)["task_report"] == "Проверка завершена"


def test_closing_reply_not_in_snapshot():
    state = State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="test",
        use_server_subagents=False,
    )
    state.closing_reply = "Пока!"

    snapshot = state.to_execution_snapshot()
    restored = ExecutionState.from_execution_snapshot(snapshot)

    assert "closing_reply" not in snapshot["data"]
    assert restored.closing_reply == ""
