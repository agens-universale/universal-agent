from gc_core.intent_log import (
    ACTION_TOOL_AGGREGATED,
    ACTION_TOOL_RAW,
    ACTION_USER_ASK,
)
from gc_core.intent_log.user_turn import (
    append_user_ask_deduped,
    append_user_asks_from_messages,
    init_subagent_open_turn,
)
from gc_core.intent_scope.append import (
    append_tool_aggregated_to_current,
    append_tool_raw_to_current,
)
from gc_core.message_policy.user_message_policy import (
    extract_real_user_message_text,
)
from gc_core.states import State


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


def _user_ask_contents(records) -> list[str]:
    return [
        str(record.content)
        for record in records
        if record.action == ACTION_USER_ASK
    ]


def test_new_state_has_empty_user_intent_log():
    state = _make_state()
    assert state.user_intent_log == []
    assert state.current_user_intent == []
    assert state.user_turn_seq == 0


def test_append_user_asks_from_messages_only_updates_current():
    state = _make_state()
    new_user_message = {
        "role": "user",
        "content": "открой приложение Jazz и создай видеовстречу",
        "id": "m_abc",
    }

    state.current_user_intent = append_user_asks_from_messages(
        state.current_user_intent,
        [new_user_message],
        user_turn_seq=state.user_turn_seq,
    )

    assert _user_ask_contents(state.current_user_intent) == [
        "открой приложение Jazz и создай видеовстречу",
    ]
    assert state.user_intent_log == []


def test_append_user_asks_from_messages_is_idempotent_for_same_message():
    state = _make_state()
    msg = {"role": "user", "content": "ping", "id": "m_1"}
    state.current_user_intent = append_user_asks_from_messages(
        state.current_user_intent, [msg], user_turn_seq=state.user_turn_seq
    )
    state.current_user_intent = append_user_asks_from_messages(
        state.current_user_intent, [msg], user_turn_seq=state.user_turn_seq
    )
    assert _user_ask_contents(state.current_user_intent) == ["ping"]


def test_append_user_asks_preserves_initial_across_step_batches():
    state = _make_state()
    initial = {"role": "user", "content": "do the thing", "id": "m_user"}
    state.current_user_intent = append_user_asks_from_messages(
        state.current_user_intent, [initial], user_turn_seq=state.user_turn_seq
    )

    step_batch = [
        {"role": "assistant", "content": "working on it", "id": "m_a1"},
        {
            "role": "tool",
            "type": "function_call",
            "name": "some_tool",
            "arguments": "{}",
            "call_id": "c1",
            "id": "fc_1",
        },
    ]
    state.current_user_intent = append_user_asks_from_messages(
        state.current_user_intent,
        step_batch,
        user_turn_seq=state.user_turn_seq,
    )

    assert _user_ask_contents(state.current_user_intent) == ["do the thing"]
    assert state.user_intent_log == []


def test_init_subagent_open_turn_without_policy_filter():
    assignment = 'Assignment:\n{"task": "fix foo"}\n\n<current_context>scope hint</current_context>'
    state = _make_state()
    init_subagent_open_turn(state, assignment)

    assert _user_ask_contents(state.current_user_intent) == [assignment]
    assert state.user_intent_log == []
    assert state.user_turn_seq == 0
    assert state.finish_gate_rejection_count == 0

    state2 = _make_state()
    text = extract_real_user_message_text(
        {"role": "user", "content": assignment, "id": "m_sub"}
    )
    if text:
        state2.current_user_intent = append_user_ask_deduped(
            state2.current_user_intent,
            text,
            user_turn_seq=state2.user_turn_seq,
        )
    assert state2.current_user_intent == []


def test_seed_user_ask_appends_only_to_current():
    from gc_core.intent_log import append_user_ask

    state = _make_state()
    state.user_intent_log = append_user_ask(
        [], "old intent 1", user_turn_seq=0, ts="2026-05-30T11:35:08Z"
    )
    state.flush_current_user_intent()
    state.current_user_intent = append_user_ask(
        [],
        "active intent 1",
        user_turn_seq=state.user_turn_seq,
        ts="2026-05-30T11:35:08Z",
    )
    new_user_message = {
        "role": "user",
        "content": "fresh request",
        "id": "m_new",
    }

    state.finish_gate_rejection_count = 0
    text = extract_real_user_message_text(new_user_message)
    state.current_user_intent = append_user_ask_deduped(
        state.current_user_intent, text, user_turn_seq=state.user_turn_seq
    )

    assert _user_ask_contents(state.current_user_intent) == [
        "active intent 1",
        "fresh request",
    ]
    assert _user_ask_contents(state.user_intent_log) == ["old intent 1"]


def test_multiple_user_asks_at_open_turn_like_agent_executor():
    state = _make_state()
    bodies = ["первый запрос", "второй запрос", "третий запрос"]
    for _idx, body in enumerate(bodies):
        state.finish_gate_rejection_count = 0
        state.current_user_intent = append_user_ask_deduped(
            state.current_user_intent,
            body,
            user_turn_seq=state.user_turn_seq,
        )

    assert _user_ask_contents(state.current_user_intent) == bodies
    assert state.user_intent_log == []
    assert state.finish_gate_rejection_count == 0

    state.current_user_intent = append_user_ask_deduped(
        state.current_user_intent,
        "второй запрос",
        user_turn_seq=state.user_turn_seq,
    )
    assert _user_ask_contents(state.current_user_intent) == bodies


def test_flush_moves_current_to_log_without_dedup():
    from gc_core.intent_log import append_user_ask

    state = _make_state()
    state.user_intent_log = append_user_ask(
        [], "closed turn", user_turn_seq=0, ts="2026-05-30T11:35:08Z"
    )
    state.flush_current_user_intent()
    state.current_user_intent = append_user_ask(
        [],
        "line a",
        user_turn_seq=state.user_turn_seq,
        ts="2026-05-30T11:35:08Z",
    )
    state.current_user_intent = append_user_ask(
        state.current_user_intent,
        "line b",
        user_turn_seq=state.user_turn_seq,
        ts="2026-05-30T11:35:08Z",
    )
    state.current_user_intent = append_user_ask(
        state.current_user_intent,
        "line a",
        user_turn_seq=state.user_turn_seq,
        ts="2026-05-30T11:35:08Z",
    )

    state.flush_current_user_intent()

    assert state.current_user_intent == []
    assert _user_ask_contents(state.user_intent_log) == [
        "closed turn",
        "line a",
        "line b",
        "line a",
    ]


def test_append_tool_aggregated_dedups_within_current():
    state = _make_state()
    append_tool_aggregated_to_current(state, "turn_ask_human", "first")
    append_tool_aggregated_to_current(state, "turn_ask_human", "first")
    append_tool_aggregated_to_current(state, "turn_finish", "second")

    assert len(state.current_user_intent) == 2
    assert state.current_user_intent[0].action == ACTION_TOOL_AGGREGATED
    assert state.current_user_intent[0].content == "first"
    assert state.current_user_intent[1].content == "second"
    assert state.user_intent_log == []


def test_append_tool_raw_dedups_within_current():
    state = _make_state()
    payload = {"task_report": "done"}
    output = {"ok": True}
    append_tool_raw_to_current(state, "turn_finish", payload, output)
    append_tool_raw_to_current(state, "turn_finish", payload, output)

    assert len(state.current_user_intent) == 1
    assert state.current_user_intent[0].action == ACTION_TOOL_RAW


def test_extract_real_user_message_text_skips_current_context_as_technical():
    msg = {
        "role": "user",
        "content": 'client has non-empty memory files; call `memory_files` with action="list" to see available files, then read what you need.',
    }
    out = extract_real_user_message_text(msg)
    assert out == ""


def test_extract_real_user_message_text_skips_validation_error_as_technical():
    msg = {
        "role": "user",
        "content": "❌ Tool Argument Validation Failed\nExpected schema: {'type': 'object'}",
    }
    out = extract_real_user_message_text(msg)
    assert out == ""


def test_extract_real_user_message_text_keeps_regular_user_message():
    msg = {"role": "user", "content": "Сделай рефакторинг parser слоя"}
    out = extract_real_user_message_text(msg)
    assert out == "Сделай рефакторинг parser слоя"
