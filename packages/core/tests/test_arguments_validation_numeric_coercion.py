# NOTE: import exceptions at MODULE level. tests/conftest.py reloads tools.* mid-session
# (ensure_real_tools_package), so an in-function import binds a different class object than
# the one parse_and_validate_tool_arguments raises -> pytest.raises would miss it.
import pytest
from gc_core.tools.arguments_validation import WrongFunctionArgumentsException, parse_and_validate_tool_arguments

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


# --- anyOf/oneOf numeric coercion (fix #3) ---

ANYOF_SPEC = {
    "type": "function",
    "name": "fs_search_and_replace",
    "description": "test",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "search": {"type": "string"},
            "replace": {"type": "string"},
            "occurrence": {
                "anyOf": [
                    {"type": "string", "enum": ["first", "last", "all"]},
                    {"type": "integer", "minimum": 1},
                ]
            },
        },
        "required": ["path", "search", "replace"],
        "additionalProperties": False,
    },
}


def _validate_anyof(args: dict) -> dict:
    return parse_and_validate_tool_arguments(
        tool_name="fs_search_and_replace",
        arguments=args,
        tool_spec=ANYOF_SPEC,
        allow_empty_for_empty_schema=False,
    )


def test_anyof_numeric_string_is_coerced():
    """A numeric string in an anyOf field with an integer branch is coerced.

    Regression for FS_UTF8_PROBLEMS.md case #3: LLMs sometimes send occurrence
    as the string "2" instead of the int 2. jsonschema anyOf does not match it
    (not enum, not integer), and _coerce_numeric_strings skipped anyOf fields
    (type=None). Now it locates the integer branch and coerces.
    """
    args = _validate_anyof(
        {"path": "/a", "search": "s", "replace": "r", "occurrence": "2"}
    )
    assert args["occurrence"] == 2
    assert isinstance(args["occurrence"], int)


def test_anyof_enum_string_not_coerced():
    """A non-numeric enum string stays a string (not coerced to a number)."""
    args = _validate_anyof(
        {"path": "/a", "search": "s", "replace": "r", "occurrence": "first"}
    )
    assert args["occurrence"] == "first"
    assert isinstance(args["occurrence"], str)


def test_anyof_integer_passes_through():
    """A plain integer in an anyOf field passes validation unchanged."""
    args = _validate_anyof(
        {"path": "/a", "search": "s", "replace": "r", "occurrence": 3}
    )
    assert args["occurrence"] == 3


def test_anyof_zero_string_rejected_by_minimum():
    """After coercion, "0" -> 0 must still fail minimum:1."""
    with pytest.raises(WrongFunctionArgumentsException):
        _validate_anyof(
            {"path": "/a", "search": "s", "replace": "r", "occurrence": "0"}
        )


def test_anyof_non_numeric_string_rejected():
    """A string that is neither numeric nor an enum value is rejected."""
    with pytest.raises(WrongFunctionArgumentsException):
        _validate_anyof(
            {"path": "/a", "search": "s", "replace": "r", "occurrence": "many"}
        )


def test_anyof_fractional_string_rejected():
    """A fractional string in an integer-branch anyOf is rejected (not integer)."""
    with pytest.raises(WrongFunctionArgumentsException):
        _validate_anyof(
            {"path": "/a", "search": "s", "replace": "r", "occurrence": "2.5"}
        )


def test_anyof_with_null_branch_still_coerces():
    """anyOf with a null branch (like apply_file_edits) still coerces numeric strings."""
    spec = {
        "type": "function",
        "name": "fs_apply_file_edits",
        "description": "test",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "occurrence": {
                    "anyOf": [
                        {"type": "string", "enum": ["first", "last", "all"]},
                        {"type": "integer", "minimum": 1},
                        {"type": "null"},
                    ]
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    }
    args = parse_and_validate_tool_arguments(
        tool_name="fs_apply_file_edits",
        arguments={"path": "/a", "occurrence": "2"},
        tool_spec=spec,
        allow_empty_for_empty_schema=False,
    )
    assert args["occurrence"] == 2
    assert isinstance(args["occurrence"], int)
