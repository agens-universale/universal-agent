import json

from intent_log import ACTION_TOOL_AGGREGATED, ACTION_TOOL_RAW, ACTION_USER_ASK
from intent_log.user_turn import (
    append_user_ask_deduped,
    append_user_asks_from_messages,
    init_subagent_open_turn,
)
from intent_scope.append import append_tool_aggregated_to_current, append_tool_raw_to_current
from message_policy.user_message_policy import extract_real_user_message_text
from persistence.state_serializer import StateSerializer
from states import State


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
        state.current_user_intent, step_batch, user_turn_seq=state.user_turn_seq
    )

    assert _user_ask_contents(state.current_user_intent) == ["do the thing"]
    assert state.user_intent_log == []


def test_init_subagent_open_turn_without_policy_filter():
    assignment = (
        'Assignment:\n{"task": "fix foo"}\n\n'
        "<current_context>scope hint</current_context>"
    )
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
            state2.current_user_intent, text, user_turn_seq=state2.user_turn_seq
        )
    assert state2.current_user_intent == []


def test_seed_user_ask_appends_only_to_current():
    from intent_log import append_user_ask

    state = _make_state()
    state.user_intent_log = append_user_ask(
        [], "old intent 1", user_turn_seq=0, ts="2026-05-30T11:35:08Z"
    )
    state.flush_current_user_intent()
    state.current_user_intent = append_user_ask(
        [], "active intent 1", user_turn_seq=state.user_turn_seq, ts="2026-05-30T11:35:08Z"
    )
    new_user_message = {"role": "user", "content": "fresh request", "id": "m_new"}

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
    for idx, body in enumerate(bodies):
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
    from intent_log import append_user_ask

    state = _make_state()
    state.user_intent_log = append_user_ask(
        [], "closed turn", user_turn_seq=0, ts="2026-05-30T11:35:08Z"
    )
    state.flush_current_user_intent()
    state.current_user_intent = append_user_ask(
        [], "line a", user_turn_seq=state.user_turn_seq, ts="2026-05-30T11:35:08Z"
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
    append_tool_aggregated_to_current(state, "ask_human", "first")
    append_tool_aggregated_to_current(state, "ask_human", "first")
    append_tool_aggregated_to_current(state, "submit_solution", "second")

    assert len(state.current_user_intent) == 2
    assert state.current_user_intent[0].action == ACTION_TOOL_AGGREGATED
    assert state.current_user_intent[0].content == "first"
    assert state.current_user_intent[1].content == "second"
    assert state.user_intent_log == []


def test_append_tool_raw_dedups_within_current():
    state = _make_state()
    payload = {"task_report": "done"}
    output = {"ok": True}
    append_tool_raw_to_current(state, "submit_solution", payload, output)
    append_tool_raw_to_current(state, "submit_solution", payload, output)

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


def test_state_to_dict_roundtrip_is_json_serializable_and_restores_shared_and_store():
    state = _make_state()
    state.shared.working_memory["state"] = "entry"
    state.shared.notes_md = "notes"
    state.shared.fetch_url_cache["https://example.com"] = {"content": "ok"}

    from tools.session.stored_outputs.tool_call_outputs_store import (  # noqa: E402
        ToolCallOutputRecord,
    )

    state.tool_call_outputs_store.put(
        ToolCallOutputRecord(
            output_ref="out_1",
            source_tool="read_file",
            call_id="call_1",
            total_chars=2,
            text="ok",
            truncated_in_message=False,
        )
    )

    payload = StateSerializer.serialize(state)
    raw_json = json.dumps(payload, ensure_ascii=False)
    restored = StateSerializer.deserialize(json.loads(raw_json))

    assert restored.shared.working_memory["state"] == "entry"
    assert StateSerializer.serialize(restored)["state_schema_version"] == "v2"
    assert restored.shared.notes_md == "notes"
    assert restored.shared.fetch_url_cache["https://example.com"] == {"content": "ok"}
    rec = restored.tool_call_outputs_store.peek("out_1")
    assert rec is not None
    assert rec.text == "ok"
    assert rec.truncated_in_message is False


def test_state_roundtrip_with_monster_payload_preserves_core_runtime_fields():
    from intent_log import append_user_ask

    state = State(
        verbose=True,
        model="CodeChat-Pro",
        tools_spec=[
            {"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "write_file", "parameters": {"type": "object"}}},
        ],
        tools_descr=[{"name": "read_file", "desc": "Read file"}, {"name": "write_file", "desc": "Write file"}],
        skills_list=[{"name": "skill_a", "version": 1}, {"name": "skill_b", "version": 2}],
        max_steps=99,
        system_prompt_id="v2-monster",
        use_server_subagents=True,
        chat_completions_system_prompt="  custom system prompt  ",
        parent_state_id="parent_123",
        mode="agent",
        call_id="call_monster",
        specific_role="orchestrator",
        agent_md_path=None,
        limit_project_metadata=False,
        thread_id="thread_777",
    )
    state.x_target = "sandbox-api"
    state.previous_response_id = "resp_prev"
    state.last_backend_response_id = "resp_last"
    state.steps = 42
    state.stop_requested = True
    state.user_intent_log = append_user_ask(
        [], "msg one", user_turn_seq=0, ts="2026-05-30T11:35:08Z"
    )
    state.flush_current_user_intent()
    state.user_intent_log = append_user_ask(
        state.user_intent_log, "msg two", user_turn_seq=state.user_turn_seq, ts="2026-05-30T11:35:08Z"
    )
    state.flush_current_user_intent()
    state.current_user_intent = append_user_ask(
        [], "active one", user_turn_seq=state.user_turn_seq, ts="2026-05-30T11:35:08Z"
    )
    state.current_user_intent = append_user_ask(
        state.current_user_intent,
        "active two",
        user_turn_seq=state.user_turn_seq,
        ts="2026-05-30T11:35:08Z",
    )
    state.user_turn_seq = 2
    state.call_metadata["extra"] = {
        "nested": [{"k": "v"}, {"n": 1}, {"flags": [True, False, None]}],
        "meta": {"a": 1, "b": "x"},
    }
    state.messages = [
        {"id": "u1", "role": "user", "content": "Сделай отчёт", "extra": {"source": "cli"}},
        {
            "id": "a1",
            "role": "assistant",
            "content": [{"type": "text", "text": "готово"}, {"type": "tool_call", "name": "read_file"}],
            "tokens": {"input": 120, "output": 55},
        },
        {"id": "t1", "type": "function_call", "name": "memory_files", "arguments": "{\"action\":\"list\"}"},
    ]

    state.shared.working_memory.update(
        {
            "state": "line1\nline2",
            "knowledge": "decision: keep explicit serializers",
            "goal": "long text " * 5,
        }
    )
    state.shared.notes_md = "# Notes\n- item1\n- item2"
    state.shared.fetch_url_cache["https://example.com/a"] = {"content": "AAA", "ts": "2026-05-06T11:00:00Z"}
    state.shared.fetch_url_cache["https://example.com/b"] = {"content": "BBB", "ts": "2026-05-06T11:01:00Z"}

    from tools.session.stored_outputs.tool_call_outputs_store import (  # noqa: E402
        ToolCallOutputRecord,
    )

    state.tool_call_outputs_store.set_max_entries(5)
    state.tool_call_outputs_store.put(
        ToolCallOutputRecord(
            output_ref="out_alpha",
            source_tool="read_file",
            call_id="call_a",
            total_chars=3,
            text="abc",
            truncated_in_message=True,
        )
    )
    state.tool_call_outputs_store.put(
        ToolCallOutputRecord(
            output_ref="out_beta",
            source_tool="search_files",
            call_id="call_b",
            total_chars=4,
            text="wxyz",
            truncated_in_message=False,
        )
    )

    raw = StateSerializer.serialize(state)
    blob = json.dumps(raw, ensure_ascii=False)
    restored = StateSerializer.deserialize(json.loads(blob))

    assert restored.verbose is True
    assert restored.default_model == "CodeChat-Pro"
    assert restored.system_prompt_id == "v2-monster"
    assert restored.use_server_subagents is True
    assert restored.chat_completions_system_prompt == "custom system prompt"
    assert restored.parent_state_id == "parent_123"
    assert restored.mode == "agent"
    assert restored.call_id == "call_monster"
    assert restored.thread_id == "thread_777"
    assert restored.x_target == "sandbox-api"
    assert restored.previous_response_id == "resp_prev"
    assert restored.last_backend_response_id == "resp_last"
    assert restored.steps == 42
    assert restored.stop_requested is True
    assert restored.max_steps == 99

    assert len(restored.messages) == 3
    assert restored.messages[1]["tokens"]["output"] == 55
    assert _user_ask_contents(restored.user_intent_log) == ["msg one", "msg two"]
    assert _user_ask_contents(restored.current_user_intent) == ["active one", "active two"]
    assert restored.user_turn_seq == 2
    assert restored.call_metadata["extra"]["nested"][2]["flags"] == [True, False, None]

    assert restored.shared.working_memory["state"] == "line1\nline2"
    assert restored.shared.working_memory["knowledge"] == "decision: keep explicit serializers"
    assert restored.shared.notes_md == "# Notes\n- item1\n- item2"
    assert restored.shared.fetch_url_cache["https://example.com/b"]["content"] == "BBB"

    assert restored.tool_call_outputs_store.max_entries == 5
    rec_alpha = restored.tool_call_outputs_store.peek("out_alpha")
    rec_beta = restored.tool_call_outputs_store.peek("out_beta")
    assert rec_alpha is not None and rec_alpha.truncated_in_message is True
    assert rec_beta is not None and rec_beta.text == "wxyz"
