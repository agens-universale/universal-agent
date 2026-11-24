"""Exercise handoff provenance at the judge request boundary."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gc_core import processing_responses
from gc_core.intent_guard.final_report_consistency import (
    avalidate_final_report_vs_user_intent,
)
from gc_core.intent_guard.text_answer_judge import ashould_return_text_answer
from gc_core.intent_guard.turn_finish_emit import get_function_call_output_text
from gc_core.states import State
from gc_core.summarize import llm
from gc_core.tools.registry import get_builtin_tools_data
from gc_testkit.intent_fixtures import seed_state_intent
from gc_testkit.memory_agent_store import MemoryAgentStore
from gc_testkit.settings import fake_ctx

_NORMALIZATION = "finish_handoff_normalization"
_CUSTOM_PROMPT = "Compare the work and report independently using the input."
_OLD_MISMATCH = "The handoff must be a JSON-encoded string."
_ARTIFACT_MISMATCH = (
    "The handoff names wrong.txt, while the trace shows report.txt."
)
_TRACE_GAP = "No verification of the written artifact is visible."


def _rejection(mismatch, *, gaps=()):
    return json.dumps(
        {
            "action": "fix_report_and_continue" if gaps else "fix_report",
            "trace_ok": not gaps,
            "report_ok": False,
            "trace_gaps": list(gaps),
            "report_mismatches": [mismatch],
            "is_question": False,
        }
    )


def _arguments(handoff, *, artifact="report.txt"):
    return {
        "solution_type": "full_solution",
        "task": "Write and verify the report.",
        "task_report": f"Wrote {artifact}.",
        "handoff": handoff,
    }


def _state():
    state = State(
        verbose=False,
        model="test-model",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=1,
        system_prompt_id="test",
        use_server_subagents=False,
    )
    seed_state_intent(state, current="Write and verify the report.")
    return state


@pytest.fixture
def ctx():
    context = fake_ctx(
        {
            "backend.api_mode": "chat_completions",
            "agent.strict_turn_finish_validation": True,
            "agent.limit_trace_summary": False,
            "text_answer_mode": "judge",
        },
        auxiliary_llm=SimpleNamespace(judge_max_tokens=8192),
    )
    context.tools_data = {
        "turn_finish": get_builtin_tools_data(context)["turn_finish"]
    }
    return context


@pytest.fixture
def model(monkeypatch):
    completion = AsyncMock(
        side_effect=AssertionError("Unexpected judge request")
    )
    monkeypatch.setattr(llm, "arequest_chat_completion_text", completion)
    return completion


def _payloads(model, consumer):
    payloads = []
    for index, call in enumerate(model.await_args_list):
        request = call.kwargs
        assert request["consumer"] == consumer
        assert request["request_label"] == (
            "intent_guard_finish_gate_retry"
            if index
            else "intent_guard_finish_gate"
        )
        marker = "\nOriginal input (unchanged):\n" if index else "\nInput:\n"
        payloads.append(
            json.loads(request["messages"][1]["content"].split(marker, 1)[1])
        )
    return payloads


async def _finish(monkeypatch, ctx, state, arguments):
    async def unexpected_main_request(*args, **kwargs):
        pytest.fail("Unexpected main model request")

    monkeypatch.setattr(
        processing_responses, "aprocess_tool_outputs", unexpected_main_request
    )
    await processing_responses.arun_agent(
        [
            {
                "type": "function_call",
                "name": "turn_finish",
                "arguments": json.dumps(arguments),
                "call_id": "current",
                "id": "finish_call",
            }
        ],
        state,
        ctx,
    )
    return json.loads(get_function_call_output_text(state.messages, "current"))


@pytest.mark.parametrize(
    "retry", [False, True], ids=["initial", "custom-retry"]
)
async def test_finish_dispatch_explains_current_handoff(
    monkeypatch, ctx, model, retry
):
    state = _state()
    state.messages = [
        {
            "type": "function_call_output",
            "call_id": "previous_write",
            "output": "Wrote report.txt",
        },
        {
            "type": "function_call_output",
            "call_id": "previous_finish",
            "output": json.dumps(
                {
                    "ok": False,
                    "status": "rejected_by_finish_gate",
                    "report_mismatches": [_OLD_MISMATCH],
                }
            ),
        },
    ]
    artifact = "wrong.txt" if retry else "report.txt"
    mismatch = _ARTIFACT_MISMATCH if retry else _OLD_MISMATCH
    gaps = [_TRACE_GAP] if retry else []
    rejection = _rejection(mismatch, gaps=gaps)
    if retry:
        ctx.agent_store = MemoryAgentStore(
            prompts={"finish_gate": _CUSTOM_PROMPT}
        )
    model.side_effect = (
        ["Incomplete verdict", rejection] if retry else [rejection]
    )

    handoff = {"artifacts": [artifact]}
    arguments = _arguments(json.dumps(handoff), artifact=artifact)
    output = await _finish(monkeypatch, ctx, state, arguments)

    payloads = _payloads(model, "finish_gate")
    assert len(payloads) == (2 if retry else 1)
    if retry:
        assert payloads[0] == payloads[1]
        for call in model.await_args_list:
            assert call.kwargs["messages"][0] == {
                "role": "system",
                "content": _CUSTOM_PROMPT,
            }
    payload = payloads[0]
    assert payload["finish_handoff_arguments"] == {
        **arguments,
        "handoff": handoff,
    }
    assert "Wrote report.txt" in payload["trace_digest_text"]
    assert _OLD_MISMATCH in payload["trace_digest_text"]
    assert "wrong.txt" not in payload["trace_digest_text"]
    assert output["status"] == "rejected_by_finish_gate"
    assert output["finish_gate"]["action"] == json.loads(rejection)["action"]
    assert output["finish_gate"]["trace_gaps"] == gaps
    assert output["finish_gate"]["report_mismatches"] == [mismatch]
    explanation = payload.get(_NORMALIZATION, "")
    assert "current turn_finish.handoff" in explanation
    assert "JSON-encoded string" in explanation
    assert "decoded object at finish_handoff_arguments.handoff" in explanation
    assert "Earlier finish-gate refusals" in explanation


@pytest.mark.parametrize("handoff", ['{"broken":', "[]", "42", {}])
async def test_invalid_wire_handoff_stops_before_judge(
    monkeypatch, ctx, model, handoff
):
    output = await _finish(monkeypatch, ctx, _state(), _arguments(handoff))
    model.assert_not_awaited()
    assert output["ok"] is False
    if isinstance(handoff, dict):
        assert output["error"]["type"] == "invalid_arguments"
        assert "string" in output["error"]["message"]
    else:
        assert output["status"] == "rejected"
        assert output["error"]["type"] == "invalid_argument"
        assert output["error"]["field"] == "handoff"


@pytest.mark.parametrize("handoff", [{}, '{"broken":', "[]", "42", "{}"])
async def test_judge_attests_only_successful_string_decode(
    ctx, model, handoff
):
    model.side_effect = [_rejection(_OLD_MISMATCH)]
    await avalidate_final_report_vs_user_intent(
        ctx=ctx,
        state=_state(),
        current_user_requests=[],
        finish_handoff_arguments=_arguments(handoff),
    )
    (payload,) = _payloads(model, "finish_gate")
    decoded = handoff == "{}"
    assert (_NORMALIZATION in payload) is decoded
    assert payload["finish_handoff_arguments"]["handoff"] == (
        {} if decoded else handoff
    )


@pytest.mark.parametrize(
    "json_like", [False, True], ids=["plain", "wire-lookalike"]
)
async def test_text_answer_judge_does_not_attest_handoff(
    ctx, model, json_like
):
    expected = "Here is the final answer."
    text = expected
    if json_like:
        text = json.dumps(_arguments('{"artifacts":["report.txt"]}'))
        expected = _arguments({"artifacts": ["report.txt"]})
    model.side_effect = ["Incomplete verdict", _rejection(_ARTIFACT_MISMATCH)]

    decision = await ashould_return_text_answer(ctx, _state(), text)

    first, retry = _payloads(model, "text_answer_judge")
    assert first == retry
    assert _NORMALIZATION not in first
    assert first["finish_handoff_arguments"] == expected
    assert not decision.accept
    assert _ARTIFACT_MISMATCH in decision.reject_feedback
