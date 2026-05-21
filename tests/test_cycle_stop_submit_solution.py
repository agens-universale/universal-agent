"""submit_solution observation on cycle_stop — output echoes function_call.arguments."""

import json
from types import SimpleNamespace

from message_policy.cycle_stop import append_cycle_stop_tool_outputs_for_batch
from states import State
from tools.simple_tools.submit_solution import SubmitSolution


def _make_state() -> State:
    return State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=2,
        system_prompt_id="v1",
        use_server_subagents=False,
    )


def _make_ctx():
    return SimpleNamespace(config_dict={}, logger=None, tools_data={})


def test_output_from_cycle_stop_arguments_echoes_fields():
    arguments = json.dumps(
        {
            "solution_type": "partial_solution",
            "task_report": "Собран research_report.md",
            "comment": "часть URL 404",
            "handoff": '{"artifacts": ["research_report.md"]}',
        },
        ensure_ascii=False,
    )
    raw = SubmitSolution.output_from_cycle_stop_arguments(arguments)
    assert raw is not None
    payload = json.loads(raw)
    assert payload["tool"] == "submit_solution"
    assert payload["ok"] is True
    assert payload["solution_type"] == "partial_solution"
    assert payload["task_report"] == "Собран research_report.md"
    assert payload["comment"] == "часть URL 404"
    assert payload["handoff"] == {"artifacts": ["research_report.md"]}
    assert payload["message"] == "Task outcome submitted."
    assert "Dialog turn finished" not in raw
    assert "synthetic" not in payload


def test_append_cycle_stop_writes_submit_output_from_arguments_not_stub():
    state = _make_state()
    arguments = json.dumps(
        {
            "solution_type": "answer_question",
            "task_report": "Ответ на вопрос пользователя",
            "handoff": "{}",
        },
        ensure_ascii=False,
    )
    tool_calls = [
        {
            "type": "function_call",
            "name": "submit_solution",
            "arguments": arguments,
            "call_id": "call_finish",
            "id": "fc_finish",
        }
    ]

    append_cycle_stop_tool_outputs_for_batch(
        state=state,
        ctx=_make_ctx(),
        tool_calls=tool_calls,
        run_agent_func=lambda *_a, **_k: None,
        persist_messages_callback=None,
        backend_mode="chat_completions",
    )

    output_msg = next(
        m for m in state.messages if m.get("type") == "function_call_output"
    )
    payload = json.loads(output_msg["output"])
    assert payload["task_report"] == "Ответ на вопрос пользователя"
    assert payload["solution_type"] == "answer_question"
    assert "Dialog turn finished" not in output_msg["output"]


def test_invalid_arguments_fall_back_to_cycle_stop_error_without_apply_tool(monkeypatch):
    state = _make_state()

    def _apply_tool_should_not_run(**_kwargs):
        raise AssertionError("apply_tool must not run when arguments are not JSON")

    monkeypatch.setattr(
        "message_policy.cycle_stop.apply_tool",
        _apply_tool_should_not_run,
    )

    append_cycle_stop_tool_outputs_for_batch(
        state=state,
        ctx=_make_ctx(),
        tool_calls=[
            {
                "type": "function_call",
                "name": "submit_solution",
                "arguments": "not-json",
                "call_id": "call_finish",
                "id": "fc_finish",
            }
        ],
        run_agent_func=lambda *_a, **_k: None,
        persist_messages_callback=None,
        backend_mode="chat_completions",
    )

    payload = json.loads(state.messages[-1]["output"])
    assert payload["ok"] is False
    assert payload["status"] == "cycle_stop_output_error"
