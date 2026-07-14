# NOTE: import exceptions at MODULE level. tests/conftest.py reloads tools.* mid-session
# (ensure_real_tools_package), so an in-function import binds a different class object than
# the one parse_and_validate_tool_arguments raises -> pytest.raises would miss it.
import pytest

from tools.arguments_validation import (
    WrongFunctionArgumentsException,
    parse_and_validate_tool_arguments,
)

SPEC = {
    "type": "function",
    "name": "fs_read_file",
    "description": "Read a file.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer"},
            "limit": {"type": "integer"},
            "ratio": {"type": "number"},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}


def _validate(args: dict) -> dict:
    return parse_and_validate_tool_arguments(
        tool_name="fs_read_file",
        arguments=args,
        tool_spec=SPEC,
        allow_empty_for_empty_schema=False,
    )


def test_integer_sent_as_float_string_is_coerced():
    args = _validate({"path": "/a.py", "offset": "500.0", "limit": "200.0"})
    assert args["offset"] == 500
    assert args["limit"] == 200
    assert isinstance(args["offset"], int)


def test_integer_sent_as_plain_string_is_coerced():
    args = _validate({"path": "/a.py", "offset": "42"})
    assert args["offset"] == 42


def test_number_sent_as_string_is_coerced():
    args = _validate({"path": "/a.py", "ratio": "0.25"})
    assert args["ratio"] == 0.25


def test_fractional_value_for_integer_field_is_rejected():
    with pytest.raises(WrongFunctionArgumentsException):
        _validate({"path": "/a.py", "offset": "500.5"})


def test_non_numeric_string_is_rejected():
    with pytest.raises(WrongFunctionArgumentsException):
        _validate({"path": "/a.py", "offset": "many"})


def test_string_field_receiving_digits_is_not_coerced():
    args = _validate({"path": "123"})
    assert args["path"] == "123"
    assert isinstance(args["path"], str)
