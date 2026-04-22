import sys
import types
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
from user_message_policy import extract_real_user_message_text  # noqa: E402


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
