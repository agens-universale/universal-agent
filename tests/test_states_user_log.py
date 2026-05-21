import json

from message_policy.user_message_policy import extract_real_user_message_text
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


def test_new_state_has_empty_user_intent_log():
    state = _make_state()
    assert state.user_intent_log == []
    assert state.current_user_intent == []


def test_append_current_user_intent_from_messages_only_updates_current():
    state = _make_state()
    new_user_message = {
        "role": "user",
        "content": "открой приложение Jazz и создай видеовстречу",
        "id": "m_abc",
    }

    state.append_current_user_intent_from_messages([new_user_message])

    assert state.current_user_intent == [
        "открой приложение Jazz и создай видеовстречу",
    ]
    assert state.user_intent_log == []


def test_append_current_user_intent_from_messages_is_idempotent_for_same_message():
    state = _make_state()
    msg = {"role": "user", "content": "ping", "id": "m_1"}
    state.append_current_user_intent_from_messages([msg])
    state.append_current_user_intent_from_messages([msg])
    assert state.current_user_intent == ["ping"]


def test_append_current_user_intent_preserves_initial_across_step_batches():
    state = _make_state()
    initial = {"role": "user", "content": "do the thing", "id": "m_user"}
    state.append_current_user_intent_from_messages([initial])

    step_batch = [
        {"role": "assistant", "content": "working on it", "id": "m_a1"},
        {"type": "function_call", "name": "some_tool", "arguments": "{}",
         "call_id": "c1", "id": "fc_1"},
    ]
    state.append_current_user_intent_from_messages(step_batch)

    assert state.current_user_intent == ["do the thing"]
    assert state.user_intent_log == []


def test_init_subagent_current_user_intent_without_policy_filter():
    assignment = (
        'Assignment:\n{"task": "fix foo"}\n\n'
        "<current_context>scope hint</current_context>"
    )
    state = _make_state()
    state.init_subagent_current_user_intent(assignment)

    assert state.current_user_intent == [assignment]
    assert state.user_intent_log == []
    assert state.finish_gate_rejection_count == 0

    state2 = _make_state()
    state2.seed_new_user_turn(
        {"role": "user", "content": assignment, "id": "m_sub"}
    )
    assert state2.current_user_intent == []


def test_seed_new_user_turn_appends_only_to_current():
    state = _make_state()
    state.current_user_intent = ["active intent 1"]
    state.user_intent_log = ["old intent 1"]
    new_user_message = {"role": "user", "content": "fresh request", "id": "m_new"}

    state.seed_new_user_turn(new_user_message)

    assert state.current_user_intent == ["active intent 1", "fresh request"]
    assert state.user_intent_log == ["old intent 1"]


def test_multiple_seed_new_user_turn_at_open_turn_like_agent_executor():
    """Как agent_executor: несколько CLI user в начале одного открытого хода."""
    state = _make_state()
    bodies = ["первый запрос", "второй запрос", "третий запрос"]
    for idx, body in enumerate(bodies):
        state.seed_new_user_turn(
            {"role": "user", "content": body, "id": f"m_user_{idx}"}
        )

    assert state.current_user_intent == bodies
    assert state.user_intent_log == []
    assert state.finish_gate_rejection_count == 0

    state.seed_new_user_turn(
        {"role": "user", "content": "второй запрос", "id": "m_user_dup"}
    )
    assert state.current_user_intent == bodies


def test_commit_closed_user_turn_flushes_current_to_log_without_dedup():
    state = _make_state()
    state.user_intent_log = ["closed turn"]
    state.current_user_intent = ["line a", "line b", "line a"]

    state.commit_closed_user_turn_to_intent_log()

    assert state.current_user_intent == []
    assert state.user_intent_log == ["closed turn", "line a", "line b", "line a"]


def test_append_intent_scope_delta_dedups_within_current():
    state = _make_state()
    state.append_intent_scope_delta("first", source="ask_human")
    state.append_intent_scope_delta("first", source="ask_human")
    state.append_intent_scope_delta("second", source="submit_solution")

    assert state.current_user_intent == ["first", "second"]
    assert state.user_intent_log == []


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
    state.shared.memory_files["journal"] = "entry"
    state.shared.notes_md = "notes"
    state.shared.fetch_url_cache["https://example.com"] = {"content": "ok"}

    from tools.stored_outputs.tool_call_outputs_store import (  # noqa: E402
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

    payload = state.to_dict()
    raw_json = json.dumps(payload, ensure_ascii=False)
    restored = State.from_dict(json.loads(raw_json))

    assert restored.shared.memory_files["journal"] == "entry"
    assert restored.shared.notes_md == "notes"
    assert restored.shared.fetch_url_cache["https://example.com"] == {"content": "ok"}
    rec = restored.tool_call_outputs_store.peek("out_1")
    assert rec is not None
    assert rec.text == "ok"
    assert rec.truncated_in_message is False


def test_state_roundtrip_with_monster_payload_preserves_core_runtime_fields():
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
    state.user_intent_log = ["msg one", "msg two"]
    state.current_user_intent = ["active one", "active two"]
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

    state.shared.memory_files.update(
        {
            "journal": "line1\nline2",
            "arch": "decision: keep explicit serializers",
            "notes": "long text " * 5,
        }
    )
    state.shared.notes_md = "# Notes\n- item1\n- item2"
    state.shared.fetch_url_cache["https://example.com/a"] = {"content": "AAA", "ts": "2026-05-06T11:00:00Z"}
    state.shared.fetch_url_cache["https://example.com/b"] = {"content": "BBB", "ts": "2026-05-06T11:01:00Z"}

    from tools.stored_outputs.tool_call_outputs_store import (  # noqa: E402
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

    raw = state.to_dict()
    blob = json.dumps(raw, ensure_ascii=False)
    restored = State.from_dict(json.loads(blob))

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
    assert restored.user_intent_log == ["msg one", "msg two"]
    assert restored.current_user_intent == ["active one", "active two"]
    assert restored.call_metadata["extra"]["nested"][2]["flags"] == [True, False, None]

    assert restored.shared.memory_files["journal"] == "line1\nline2"
    assert restored.shared.memory_files["arch"] == "decision: keep explicit serializers"
    assert restored.shared.notes_md == "# Notes\n- item1\n- item2"
    assert restored.shared.fetch_url_cache["https://example.com/b"]["content"] == "BBB"

    assert restored.tool_call_outputs_store.max_entries == 5
    rec_alpha = restored.tool_call_outputs_store.peek("out_alpha")
    rec_beta = restored.tool_call_outputs_store.peek("out_beta")
    assert rec_alpha is not None and rec_alpha.truncated_in_message is True
    assert rec_beta is not None and rec_beta.text == "wxyz"
