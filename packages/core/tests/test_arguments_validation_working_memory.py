import json

import pytest

from gc_core.tools.arguments_validation import WrongFunctionArgumentsException, parse_and_validate_tool_arguments, resolve_tool_arguments_feedback_hint
from gc_core.tools.session.working_memory import WorkingMemory


def test_working_memory_path_hint_from_tool_hook():
    hint = WorkingMemory.wrong_arguments_feedback_hint(
        received_args=json.dumps({"action": "append", "path": "/tmp/foo.py", "content": "x"}),
        parsed_args={"action": "append", "path": "/tmp/foo.py", "content": "x"},
        _message="unknown field",
    )
    assert hint is not None
    assert "does NOT write files to disk" in hint
    assert "register" in hint
    assert "filesystem write tool" in hint
    assert "memory_files" not in hint


def test_working_memory_hook_returns_none_without_path():
    hint = WorkingMemory.wrong_arguments_feedback_hint(
        received_args=json.dumps({"action": "append", "content": "note"}),
        parsed_args={"action": "append", "content": "note"},
        _message="missing register",
    )
    assert hint is None


def test_parse_and_validate_attaches_tool_hint_on_schema_error():
    spec = WorkingMemory.tool_description()
    with pytest.raises(WrongFunctionArgumentsException) as exc_info:
        parse_and_validate_tool_arguments(
            tool_name="working_memory",
            arguments=json.dumps({"action": "append", "path": "/tmp/foo.py", "content": "x"}),
            tool_spec=spec,
            allow_empty_for_empty_schema=False,
            tool_fabric=WorkingMemory,
        )
    feedback = exc_info.value.to_llm_feedback()
    assert "does NOT write files to disk" in feedback
    assert exc_info.value.extra_hint is not None


def test_parse_and_validate_rejects_write_without_register():
    spec = WorkingMemory.tool_description()
    with pytest.raises(WrongFunctionArgumentsException):
        parse_and_validate_tool_arguments(
            tool_name="session_memory",
            arguments=json.dumps({"action": "write", "content": "x"}),
            tool_spec=spec,
            allow_empty_for_empty_schema=False,
            tool_fabric=WorkingMemory,
        )


def test_exception_extra_hint_in_feedback():
    exc = WrongFunctionArgumentsException(
        message="unknown field",
        received_args="{}",
        tool_name="working_memory",
        extra_hint="💡 Hint: custom\n",
    )
    assert "custom" in exc.to_llm_feedback()


def test_resolve_hint_delegates_to_tool_fabric():
    hint = resolve_tool_arguments_feedback_hint(
        WorkingMemory,
        received_args=json.dumps({"path": "/x"}),
        parsed_args={"path": "/x"},
        message="err",
    )
    assert hint is not None
