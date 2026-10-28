"""Card data survives truncation, completion and thread compaction."""

import json
from types import SimpleNamespace

import pytest

from gc_core import processing_responses as pr
from gc_core.agent_executor import AgentExecutor
from gc_core.auto_routing.pair_judge import build_pair_card
from gc_core.config_modules.root import AppConfig
from gc_core.intent_guard.guard import FinishGateDecision, FinishGateGuard
from gc_core.intent_log import append_final_answer, append_user_ask, flush
from gc_core.run_context import acreate_run_context
from gc_core.settings import LoadedSettings, RuntimeEnv
from gc_core.summarize.compaction import apply_summary_state_patch
from gc_core.threads.file_store import FileThreadStore
from gc_core.utils.logging_module import release_run_logger


def _card(asks, log=(), seq=0, messages=()):
    records = []
    for ask in asks:
        records = append_user_ask(records, ask, user_turn_seq=seq)
    return json.loads(
        build_pair_card(
            SimpleNamespace(
                current_user_intent=records,
                user_intent_log=list(log),
                user_turn_seq=seq,
                messages=list(messages),
                turn_messages_start=None,
            )
        )
    )


def test_long_request_preserves_initial_scope_and_final_instruction():
    card = _card(["INITIAL SCOPE " + "x" * 2900 + " FIX THE FINAL CRASH"])
    assert card["request"].startswith("INITIAL SCOPE")
    assert card["request"].endswith("FIX THE FINAL CRASH")
    assert len(card["request"]) <= 2000


def test_later_user_ask_survives_a_long_first_ask():
    assert _card(["x" * 3000, "Also verify cancellation"])["request"].endswith(
        "Also verify cancellation"
    )


def test_previous_request_and_outcome_keep_their_conclusions():
    current = append_user_ask(
        [], "START " + "x" * 2000 + " REPAIR THE TRANSACTION", user_turn_seq=0
    )
    current = append_final_answer(
        current,
        "REPORT " + "x" * 2000 + " NEXT: VERIFY ROLLBACK",
        user_turn_seq=0,
    )
    _, log, seq = flush(current, [], 0)
    previous = _card(["yes, do it"], log, seq)["previous_turn"]
    assert previous["request"].endswith("REPAIR THE TRANSACTION")
    assert previous["outcome"].endswith("NEXT: VERIFY ROLLBACK")
    assert (
        len(previous["request"]) <= 1000 and len(previous["outcome"]) <= 1000
    )


def test_missing_previous_outcome_is_explicit():
    _, log, seq = flush(
        append_user_ask([], "repair storage", user_turn_seq=0), [], 0
    )
    previous = _card(["continue"], log, seq)["previous_turn"]
    assert previous["outcome_available"] is False


@pytest.mark.parametrize(
    "gate_outcome", ["accept", "skipped", "fail_open_error", "fail_open_limit"]
)
@pytest.mark.parametrize("old_contract", [False, True])
async def test_finish_outcome_survives_compaction_and_meta_reload(
    tmp_path, monkeypatch, gate_outcome, old_contract
):
    monkeypatch.chdir(tmp_path)
    config = AppConfig.model_validate(
        {
            "level": "default",
            "logging": {"save_logs": False, "console_trace": False},
            "backend": {"api_mode": "chat_completions", "vision": False},
            "agent": {
                "use_local_subagents": False,
                "stop_tools": ["project_memory"],
            },
            "intent_scope": {"tools": {}},
        }
    )
    store = FileThreadStore(tmp_path / "threads")
    ctx = await acreate_run_context(
        LoadedSettings(
            settings=config, runtime=RuntimeEnv(launch_root=str(tmp_path))
        ),
        thread_store=store,
    )
    ctx.routing_contract_v2 = not old_contract
    report = "INITIAL FINDINGS " + "x" * 3000 + " NEXT: REPAIR ROLLBACK"

    async def backend(messages, state, ctx, **kwargs):
        return (
            [
                {
                    "type": "function_call",
                    "id": "f",
                    "call_id": "f",
                    "status": "completed",
                    "name": "turn_finish",
                    "arguments": json.dumps(
                        {
                            "solution_type": "partial_solution",
                            "task": "repair storage",
                            "task_report": report,
                            "handoff": "{}",
                        }
                    ),
                }
            ],
            "reply",
            {},
        )

    async def gate(self, **kwargs):
        return FinishGateDecision(outcome=gate_outcome)

    monkeypatch.setattr(pr, "_arequest_backend_canonical", backend)
    monkeypatch.setattr(FinishGateGuard, "aevaluate_turn_finish", gate)
    try:
        executor = AgentExecutor(ctx)
        await executor.arun(
            ["repair storage"], system="test", thread_id="resume"
        )
        state = ctx.active_state
        apply_summary_state_patch(
            state=state,
            raw_summary="Previous work was summarized",
            last_dialog_steps="",
            summarized_messages_count=len(state.messages),
            can_propose_plan=True,
        )
        from gc_testkit.session_meta import merge_state_into_meta
        from gc_core.intent_log import deserialize_list

        meta = merge_state_into_meta({}, state)
        restored = SimpleNamespace(
            user_intent_log=deserialize_list(meta["user_intent_log"]),
            user_turn_seq=meta["user_turn_seq"],
            messages=state.messages,
            current_user_intent=append_user_ask(
                [], "yes, do it", user_turn_seq=meta["user_turn_seq"]
            ),
            turn_messages_start=len(state.messages),
        )
        previous = json.loads(build_pair_card(restored))["previous_turn"]
        if old_contract:
            assert previous["outcome_available"] is False
            return
        assert previous["outcome"].startswith("partial_solution")
        assert previous["outcome"].endswith("NEXT: REPAIR ROLLBACK")
        assert len(previous["outcome"]) <= 1000
        if gate_outcome.startswith("fail_open"):
            assert gate_outcome in previous["outcome"]
    finally:
        release_run_logger(ctx.logger)


def test_old_card_shape_remains_available_for_durable_replay():
    state = SimpleNamespace(
        current_user_intent=append_user_ask(
            [], "BEGIN " + "x" * 3000 + " END", user_turn_seq=0
        ),
        user_intent_log=[],
        user_turn_seq=0,
        messages=[],
        turn_messages_start=None,
    )
    card = json.loads(build_pair_card(state, legacy=True))
    assert card["request"].startswith("BEGIN")
    assert not card["request"].endswith("END")
    assert len(card["request"]) == 2000


def test_an_older_finish_is_not_used_for_an_unfinished_previous_turn():
    old_finish = {
        "type": "function_call",
        "name": "turn_finish",
        "arguments": json.dumps(
            {"solution_type": "full_solution", "task_report": "OLDER TASK"}
        ),
    }
    _, log, seq = flush(
        append_user_ask([], "NEW TASK", user_turn_seq=0), [], 0
    )
    previous = _card(
        ["continue"],
        log,
        seq,
        [old_finish, {"role": "user", "content": "NEW TASK"}],
    )["previous_turn"]
    assert previous["outcome_available"] is False


async def test_rejected_finish_does_not_become_the_previous_turn_outcome(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    cfg = AppConfig.model_validate(
        {
            "backend": {"api_mode": "chat_completions", "vision": False},
            "agent": {
                "use_local_subagents": False,
                "stop_tools": ["project_memory"],
            },
            "logging": {"save_logs": False, "console_trace": False},
            "intent_scope": {"tools": {}},
        }
    )
    ctx = await acreate_run_context(
        LoadedSettings(
            settings=cfg, runtime=RuntimeEnv(launch_root=str(tmp_path))
        ),
        thread_store=FileThreadStore(tmp_path / "threads"),
    )
    reports = iter(["REJECTED INCOMPLETE REPORT", "COMPLETED REPORT"])
    decisions = iter(["reject", "accept"])

    async def backend(messages, state, ctx, **kwargs):
        report = next(reports)
        return (
            [
                {
                    "type": "function_call",
                    "id": report,
                    "call_id": report,
                    "status": "completed",
                    "name": "turn_finish",
                    "arguments": json.dumps(
                        {
                            "solution_type": "full_solution",
                            "task": "task",
                            "task_report": report,
                            "handoff": "{}",
                        }
                    ),
                }
            ],
            report,
            {},
        )

    async def gate(self, **kwargs):
        return FinishGateDecision(
            outcome=next(decisions), feedback_text="Continue the task"
        )

    monkeypatch.setattr(pr, "_arequest_backend_canonical", backend)
    monkeypatch.setattr(FinishGateGuard, "aevaluate_turn_finish", gate)
    try:
        await AgentExecutor(ctx).arun(
            ["task"], system="test", thread_id="reject-then-accept"
        )
        finals = [
            r.content
            for r in ctx.active_state.user_intent_log
            if r.action == "final_answer"
        ]
        assert finals == ["full_solution: COMPLETED REPORT"]
    finally:
        release_run_logger(ctx.logger)


@pytest.mark.parametrize(
    "status",
    [
        "submitted",
        "rejected_by_finish_gate",
        "rejected_with_ok_true",
        "missing",
    ],
)
def test_legacy_finish_requires_an_allowed_output(status):
    _, log, seq = flush(
        append_user_ask([], "OLD TASK", user_turn_seq=0), [], 0
    )
    messages = [
        {"role": "user", "content": "OLD TASK"},
        {
            "type": "function_call",
            "name": "turn_finish",
            "call_id": "f",
            "arguments": json.dumps(
                {"solution_type": "full_solution", "task_report": "OLD REPORT"}
            ),
        },
    ]
    if status != "missing":
        messages.append(
            {
                "type": "function_call_output",
                "call_id": "f",
                "output": json.dumps(
                    {
                        "ok": status in ("submitted", "rejected_with_ok_true"),
                        "status": "rejected_by_finish_gate"
                        if status == "rejected_with_ok_true"
                        else status,
                    }
                ),
            }
        )
    state = SimpleNamespace(
        current_user_intent=append_user_ask([], "continue", user_turn_seq=seq),
        user_intent_log=log,
        user_turn_seq=seq,
        messages=messages,
        turn_messages_start=len(messages),
    )
    previous = json.loads(build_pair_card(state))["previous_turn"]
    if status == "submitted":
        assert previous["outcome"] == "full_solution: OLD REPORT"
    else:
        assert previous["outcome_available"] is False


def test_resume_does_not_attach_open_turn_finish_to_a_closed_turn():
    _, log, seq = flush(
        append_user_ask([], "CLOSED TASK A", user_turn_seq=0), [], 0
    )
    messages = [
        {"role": "user", "content": "CLOSED TASK A"},
        {"role": "user", "content": "OPEN TASK B"},
        {
            "type": "function_call",
            "name": "turn_finish",
            "call_id": "b",
            "arguments": json.dumps(
                {
                    "solution_type": "partial_solution",
                    "task_report": "REJECTED B",
                }
            ),
        },
        {
            "type": "function_call_output",
            "call_id": "b",
            "output": json.dumps(
                {
                    "ok": False,
                    "status": "rejected_by_finish_gate",
                    "finish_gate": {"action": "reject"},
                }
            ),
        },
    ]
    state = SimpleNamespace(
        current_user_intent=append_user_ask(
            [], "OPEN TASK B", user_turn_seq=seq
        ),
        user_intent_log=log,
        user_turn_seq=seq,
        messages=messages,
        turn_messages_start=len(messages),
    )
    previous = json.loads(build_pair_card(state))["previous_turn"]
    assert previous["request"] == "CLOSED TASK A"
    assert previous["outcome_available"] is False


@pytest.mark.parametrize(
    "runtime_outcome", ["fail_open_error", "fail_open_limit"]
)
def test_old_allowed_finish_preserves_unconfirmed_gate_status(runtime_outcome):
    _, log, seq = flush(
        append_user_ask([], "OLD TASK", user_turn_seq=0), [], 0
    )
    messages = [
        {"role": "user", "content": "OLD TASK"},
        {
            "type": "function_call",
            "name": "turn_finish",
            "call_id": "f",
            "arguments": json.dumps(
                {"solution_type": "full_solution", "task_report": "OLD REPORT"}
            ),
        },
        {
            "type": "function_call_output",
            "call_id": "f",
            "output": json.dumps(
                {
                    "ok": True,
                    "status": "submitted",
                    "finish_gate": {"runtime_outcome": runtime_outcome},
                }
            ),
        },
    ]
    state = SimpleNamespace(
        current_user_intent=append_user_ask([], "continue", user_turn_seq=seq),
        user_intent_log=log,
        user_turn_seq=seq,
        messages=messages,
        turn_messages_start=len(messages),
    )
    outcome = json.loads(build_pair_card(state))["previous_turn"]["outcome"]
    assert runtime_outcome in outcome
    assert outcome.endswith("OLD REPORT")
