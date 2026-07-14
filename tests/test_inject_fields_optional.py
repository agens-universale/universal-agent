import pytest

from tools.arguments_validation import (
    WrongFunctionArgumentsException,
    parse_and_validate_tool_arguments,
)
from tools.inject_tool_fields import apply_injected_fields_to_spec
from tools.schema_optional import resolve_optional_property_keys

INJECT = {"reason": "One short sentence explaining the purpose of this call."}


def _spec() -> dict:
    return {
        "type": "function",
        "name": "fs_read_file",
        "description": "Read a file.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    }


def test_injected_reason_is_marked_optional():
    spec = _spec()
    apply_injected_fields_to_spec(spec, INJECT)
    params = spec["parameters"]

    assert params["required"][0] == "reason"          # API schema still demands it
    assert "reason" in resolve_optional_property_keys(params)   # local validation does not


def test_call_without_reason_validates():
    spec = _spec()
    apply_injected_fields_to_spec(spec, INJECT)

    args = parse_and_validate_tool_arguments(
        tool_name="fs_read_file",
        arguments={"path": "/testbed/a.py"},
        tool_spec=spec,
        allow_empty_for_empty_schema=False,
    )
    assert args == {"path": "/testbed/a.py"}


def test_call_with_reason_still_validates():
    spec = _spec()
    apply_injected_fields_to_spec(spec, INJECT)

    args = parse_and_validate_tool_arguments(
        tool_name="fs_read_file",
        arguments={"path": "/testbed/a.py", "reason": "read the module"},
        tool_spec=spec,
        allow_empty_for_empty_schema=False,
    )
    assert args["reason"] == "read the module"


def test_real_required_field_still_enforced():
    spec = _spec()
    apply_injected_fields_to_spec(spec, INJECT)

    with pytest.raises(WrongFunctionArgumentsException):
        parse_and_validate_tool_arguments(
            tool_name="fs_read_file",
            arguments={"reason": "no path given"},
            tool_spec=spec,
            allow_empty_for_empty_schema=False,
        )
