import json

import pytest
from gc_core.tools.arguments_validation import (
    WrongFunctionArgumentsException,
    parse_and_validate_tool_arguments,
)
from gc_core.tools.session.working_memory import WorkingMemory


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


def test_parse_and_validate_rejects_write_without_content():
    spec = WorkingMemory.tool_description()
    with pytest.raises(WrongFunctionArgumentsException):
        parse_and_validate_tool_arguments(
            tool_name="session_memory",
            arguments=json.dumps({"action": "write", "register": "goal"}),
            tool_spec=spec,
            allow_empty_for_empty_schema=False,
            tool_fabric=WorkingMemory,
        )


def test_parse_and_validate_list_accepts_empty_content():
    spec = WorkingMemory.tool_description()
    parsed = parse_and_validate_tool_arguments(
        tool_name="session_memory",
        arguments=json.dumps(
            {"action": "list", "register": "goal", "content": ""}
        ),
        tool_spec=spec,
        allow_empty_for_empty_schema=False,
        tool_fabric=WorkingMemory,
    )
    assert parsed["content"] == ""


def test_exception_extra_hint_in_feedback():
    exc = WrongFunctionArgumentsException(
        message="unknown field",
        received_args="{}",
        tool_name="working_memory",
        extra_hint="💡 Hint: custom\n",
    )
    assert "custom" in exc.to_llm_feedback()
