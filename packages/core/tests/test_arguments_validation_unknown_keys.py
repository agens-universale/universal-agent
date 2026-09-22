# NOTE: import exceptions at MODULE level. tests/conftest.py reloads tools.* mid-session
# (ensure_real_tools_package), so an in-function import binds a different class object than
# the one parse_and_validate_tool_arguments raises -> pytest.raises would miss it.
import json

import pytest

from gc_testkit import run_coro

from gc_core.tools import aapply_tool
from gc_core.tools.arguments_validation import (
    WrongFunctionArgumentsException,
    annotate_output_with_dropped_arguments,
    parse_and_validate_tool_arguments,
)

SPEC = {
    "type": "function",
    "name": "exec_command",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer"},
        },
        "required": ["command"],
        "additionalProperties": False,
    },
}


def _validate(arguments: dict | str, *, spec: dict | None = None) -> dict:
    return parse_and_validate_tool_arguments(
        tool_name="exec_command",
        arguments=arguments,
        tool_spec=spec or SPEC,
        allow_empty_for_empty_schema=False,
    )


def test_unknown_top_level_key_is_dropped_and_call_proceeds():
    dropped: list[str] = []
    args = parse_and_validate_tool_arguments(
        tool_name="exec_command",
        arguments={"command": "ls", "description": "List transform directory"},
        tool_spec=SPEC,
        allow_empty_for_empty_schema=False,
        dropped_unknown=dropped,
    )
    assert args == {"command": "ls"}
    assert dropped == ["description"]


def test_several_unknown_keys_are_dropped():
    args = _validate({"command": "ls", "description": "why", "reason": "because"})
    assert args == {"command": "ls"}


def test_unknown_key_plus_numeric_string_still_recovers():
    args = _validate({"command": "ls", "timeout": "30", "description": "why"})
    assert args == {"command": "ls", "timeout": 30}


def test_unknown_key_does_not_hide_a_missing_required():
    dropped: list[str] = []
    with pytest.raises(WrongFunctionArgumentsException):
        parse_and_validate_tool_arguments(
            tool_name="exec_command",
            arguments={"description": "why", "timeout": 5},
            tool_spec=SPEC,
            allow_empty_for_empty_schema=False,
            dropped_unknown=dropped,
        )
    assert dropped == []


def test_nested_unknown_key_still_rejected():
    spec = {
        "parameters": {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"search": {"type": "string"}},
                        "required": ["search"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["edits"],
            "additionalProperties": False,
        }
    }
    with pytest.raises(WrongFunctionArgumentsException):
        _validate({"edits": [{"search": "a", "note": "extra"}]}, spec=spec)


def test_annotate_json_object_puts_note_first():
    raw = annotate_output_with_dropped_arguments(
        '{"ok": true, "stdout": "hi"}',
        ["description", "reason"],
    )
    payload = json.loads(raw)
    assert list(payload)[0] == "dropped_arguments"
    assert payload["dropped_arguments"] == (
        "Fields description, reason were dropped because they are not in the tool schema."
    )
    assert payload["ok"] is True


def test_annotate_plain_text_prefixes_note():
    raw = annotate_output_with_dropped_arguments("done", ["description"])
    assert raw == "Field description was dropped because it is not in the tool schema.\ndone"


def test_apply_tool_output_mentions_dropped_fields():
    class Echo:
        def __init__(self, **_kwargs):
            pass

        # Контракт тула в ветке — `acall`; синхронного входа нет ни у одного
        # настоящего тула, и двойник обязан повторять форму, а не прошлую.
        async def acall(self, command: str, timeout: int | None = None) -> str:
            return json.dumps({"ok": True, "command": command, "timeout": timeout})

    tools = {
        "exec_command": {
            "tool_fabric": Echo,
            "spec": SPEC,
            "injected_fields": [],
        }
    }
    raw = run_coro(aapply_tool(
        "exec_command",
        {"command": "ls", "description": "List dir", "timeout": "3"},
        tools_data_=tools,
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["command"] == "ls"
    assert payload["timeout"] == 3
    assert payload["dropped_arguments"] == (
        "Field description was dropped because it is not in the tool schema."
    )


def test_open_schema_still_drops_extra_keys():
    """additionalProperties — контракт для модели, не разрешение оставить ключ."""
    spec = {
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": True,
        }
    }
    dropped: list[str] = []
    args = parse_and_validate_tool_arguments(
        tool_name="exec_command",
        arguments={"command": "ls", "description": "not a parameter"},
        tool_spec=spec,
        allow_empty_for_empty_schema=False,
        dropped_unknown=dropped,
    )
    assert args == {"command": "ls"}
    assert dropped == ["description"]
