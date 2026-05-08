import sys
import types
import json
from pathlib import Path


def _stub_tools_modules() -> None:
    # Mirrors the stubbing pattern from test_processing_responses_feedback —
    # `states` imports `tools` indirectly; we don't need the real tools here.
    for name in list(sys.modules):
        if name == "tools" or name.startswith("tools."):
            sys.modules.pop(name, None)
    tools_pkg = types.ModuleType("tools")

    def _apply_tool(*_args, **_kwargs):
        raise AssertionError("apply_tool should not be called in this test")

    class _WrongFunctionArgumentsException(Exception):
        pass

    tools_pkg.apply_tool = _apply_tool
    tools_pkg.WrongFunctionArgumentsException = _WrongFunctionArgumentsException
    root = Path(__file__).resolve().parents[1]
    tools_pkg.__path__ = [str(root / "src" / "tools")]
    sys.modules["tools"] = tools_pkg


_stub_tools_modules()

from states import State  # noqa: E402
from message_policy.user_message_policy import extract_real_user_message_text  # noqa: E402


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


def test_new_state_has_empty_user_messages_log():
    """Fresh State always starts with an empty user_messages_log."""
    state = _make_state()
    assert state.user_messages_log == []


def test_append_user_messages_to_log_seeds_initial_user_turn():
    """Regression for finish_gate rejecting valid submit_solution with
    'user_messages_log is empty'. agent_executor.run_agent must seed
    the log with the current user message BEFORE run_step/finish_gate;
    the _persist_*_batch callbacks only see step_messages from subsequent
    assistant turns, never the initiating user message."""
    state = _make_state()
    new_user_message = {
        "role": "user",
        "content": "открой приложение Jazz и создай видеовстречу",
        "id": "m_abc",
    }

    state.append_user_messages_to_log([new_user_message])

    assert state.user_messages_log == [
        "открой приложение Jazz и создай видеовстречу",
    ]


def test_append_user_messages_to_log_is_idempotent_for_same_message():
    """Calling append twice with the same message must not duplicate it —
    safe to re-seed on branches that may run the init path more than once."""
    state = _make_state()
    msg = {"role": "user", "content": "ping", "id": "m_1"}
    state.append_user_messages_to_log([msg])
    state.append_user_messages_to_log([msg])
    assert state.user_messages_log == ["ping"]


def test_append_user_messages_to_log_preserves_initial_across_step_batches():
    """After seeding with the initial user turn, subsequent step_messages
    batches (assistant output + tool calls) must NOT wipe the initial entry."""
    state = _make_state()
    initial = {"role": "user", "content": "do the thing", "id": "m_user"}
    state.append_user_messages_to_log([initial])

    # Subsequent step batch: assistant reply + tool call — no real user msg.
    step_batch = [
        {"role": "assistant", "content": "working on it", "id": "m_a1"},
        {"type": "function_call", "name": "some_tool", "arguments": "{}",
         "call_id": "c1", "id": "fc_1"},
    ]
    state.append_user_messages_to_log(step_batch)

    assert state.user_messages_log == ["do the thing"]


def test_seed_new_user_turn_appends_to_current_scope_and_global_log():
    state = _make_state()
    state.current_user_messages = ["active intent 1"]
    state.user_messages_log = ["old intent 1", "active intent 1"]
    new_user_message = {"role": "user", "content": "fresh request", "id": "m_new"}

    state.seed_new_user_turn(new_user_message)

    assert state.current_user_messages == ["active intent 1", "fresh request"]
    assert state.user_messages_log == [
        "old intent 1",
        "active intent 1",
        "fresh request",
    ]


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

    # Наполняем store, чтобы проверить его сериализацию/десериализацию.
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
    """Проверка round-trip на максимально насыщенном state payload."""
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
    state.user_messages_log = ["msg one", "msg two"]
    state.current_user_messages = ["active one", "active two"]
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

    # Core scalar/runtime fields.
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

    # Complex collections.
    assert len(restored.messages) == 3
    assert restored.messages[1]["tokens"]["output"] == 55
    assert restored.user_messages_log == ["msg one", "msg two"]
    assert restored.current_user_messages == ["active one", "active two"]
    assert restored.call_metadata["extra"]["nested"][2]["flags"] == [True, False, None]

    # Shared dataclass and nested maps.
    assert restored.shared.memory_files["journal"] == "line1\nline2"
    assert restored.shared.memory_files["arch"] == "decision: keep explicit serializers"
    assert restored.shared.notes_md == "# Notes\n- item1\n- item2"
    assert restored.shared.fetch_url_cache["https://example.com/b"]["content"] == "BBB"

    # Tool output store.
    assert restored.tool_call_outputs_store.max_entries == 5
    rec_alpha = restored.tool_call_outputs_store.peek("out_alpha")
    rec_beta = restored.tool_call_outputs_store.peek("out_beta")
    assert rec_alpha is not None and rec_alpha.truncated_in_message is True
    assert rec_beta is not None and rec_beta.text == "wxyz"
