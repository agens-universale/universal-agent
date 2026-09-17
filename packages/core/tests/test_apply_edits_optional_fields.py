# NOTE: import exceptions at MODULE level. tests/conftest.py reloads tools.* mid-session
# (ensure_real_tools_package), so an in-function import binds a different class object than
# the one parse_and_validate_tool_arguments raises → pytest.raises would miss it.
import pytest

from gc_core.tools.arguments_validation import WrongFunctionArgumentsException, parse_and_validate_tool_arguments
from gc_core.tools.fs.apply_file_edits import ApplyFileEdits


def _spec() -> dict:
    return ApplyFileEdits.tool_description()


def test_edit_item_marks_nullable_fields_optional():
    item = _spec()["parameters"]["properties"]["edits"]["items"]
    assert set(item["x-optional-keys"]) == {
        "expected_matches",
        "occurrence",
    }
    assert item["required"] == [
        "search",
        "replace",
    ]


def test_minimal_edit_validates():
    args = parse_and_validate_tool_arguments(
        tool_name="fs_apply_file_edits",
        arguments={
            "path": "/testbed/pkg/mod.py",
            "edits": [{"search": "def f():", "replace": "def f(x):"}],
        },
        tool_spec=_spec(),
        allow_empty_for_empty_schema=False,
    )
    assert args["edits"][0]["search"] == "def f():"


def test_full_edit_still_validates():
    args = parse_and_validate_tool_arguments(
        tool_name="fs_apply_file_edits",
        arguments={
            "path": "/testbed/pkg/mod.py",
            "edits": [
                {
                    "search": "a",
                    "replace": "b",
                    "expected_matches": 1,
                    "occurrence": "first",
                }
            ],
        },
        tool_spec=_spec(),
        allow_empty_for_empty_schema=False,
    )
    assert args["edits"][0]["occurrence"] == "first"


def test_edit_without_search_is_still_rejected():
    with pytest.raises(WrongFunctionArgumentsException):
        parse_and_validate_tool_arguments(
            tool_name="fs_apply_file_edits",
            arguments={"path": "/testbed/a.py", "edits": [{"replace": "b"}]},
            tool_spec=_spec(),
            allow_empty_for_empty_schema=False,
        )
