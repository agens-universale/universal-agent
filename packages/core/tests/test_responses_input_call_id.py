import json
from types import SimpleNamespace

import httpx
import pytest
from gc_core.agent_executor import AgentExecutor
from gc_core.execution.local import LocalOperations
from gc_core.run_context import acreate_run_context
from gc_core.utils import connect_backend
from gc_core.utils.connect_backend import create_call_data_responses
from gc_core.utils.logging_module import release_run_logger
from gc_testkit.scripted_model import FINISH
from parity_harness import ScriptedBackend, parity_config


def test_responses_input_strips_extra_call_id_from_message_items():
    state = SimpleNamespace(
        default_model="m",
        tools=[],
        call_metadata={},
        previous_response_id=None,
    )
    msgs = [
        {"role": "user", "content": "x", "id": "m1", "call_id": "call_1"},
        {
            "type": "function_call",
            "call_id": "call_2",
            "name": "f",
            "arguments": "{}",
        },
    ]
    data = create_call_data_responses(msgs, state)
    assert "call_id" not in data["input"][0]  # снят с user-сообщения
    assert data["input"][1]["call_id"] == "call_2"  # на function_call сохранён
    assert (
        msgs[0]["call_id"] == "call_1"
    )  # исходник НЕ мутирован (TUI/persist)


@pytest.mark.parametrize(
    "with_port", [False, True], ids=["local", "model-port"]
)
async def test_responses_replay_after_call_id_repair_keeps_previous_turns(
    tmp_path, monkeypatch, with_port
):
    def completed(response_id, *calls):
        return {
            "id": response_id,
            "model": "parity-model",
            "output": [
                {
                    "id": f"fc_{response_id}_{i}",
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                }
                for i, (call_id, name, arguments) in enumerate(calls)
            ],
            "usage": {"input_tokens": 12, "output_tokens": 4},
        }

    finish = FINISH["choices"][0]["message"]["tool_calls"][0]["function"]
    backend = ScriptedBackend(
        [
            completed(
                "first-finish", ("first", finish["name"], finish["arguments"])
            ),
            completed(
                "duplicate-call-ids",
                ("dup", "exec_calculator", '{"expression": "2+2"}'),
                ("dup", "exec_calculator", '{"expression": "3+3"}'),
            ),
            completed(
                "last-finish", ("last", finish["name"], finish["arguments"])
            ),
        ]
    )

    def respond(request):
        response = backend.next_payload(json.loads(request.content))
        event = {"type": "response.completed", "response": response}
        return httpx.Response(200, text=f"data: {json.dumps(event)}\n\n")

    monkeypatch.setattr(
        connect_backend,
        "get_async_client",
        lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    cfg = parity_config(
        tmp_path,
        monkeypatch,
        **{
            "backend.api_mode": "responses",
            "backend.completions_url": "http://parity.invalid/v1/responses",
            "agent.strict_turn_finish_validation": False,
        },
    )
    monkeypatch.chdir(tmp_path)
    ctx = await acreate_run_context(cfg)
    if with_port:
        ctx.operations = LocalOperations(ctx)
    executor = AgentExecutor(ctx)
    try:
        await executor.arun(
            ["First user turn."], thread_id="replay", start_new_thread=True
        )
        await executor.arun(["Second user turn."], thread_id="replay")
    finally:
        release_run_logger(ctx.logger)

    backend.assert_clean()
    assert backend.calls[1]["previous_response_id"] == "first-finish"
    continuation = backend.calls[2]
    assert continuation["previous_response_id"] is None
    assert any(
        "First user turn." in str(msg.get("content"))
        for msg in continuation["input"]
    )
    call_ids = [
        msg["call_id"]
        for msg in continuation["input"]
        if msg.get("type") == "function_call"
        and msg.get("name") == "exec_calculator"
    ]
    assert len(set(call_ids)) == 2
    assert {
        msg["call_id"]
        for msg in continuation["input"]
        if msg.get("type") == "function_call_output"
    }.issuperset(call_ids)
