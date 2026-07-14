"""The three Wrong-arguments classes observed on SWE-bench Verified (13.07): 222 of 254 rejections."""

import pytest

from tools import build_tools_data
from tools.arguments_validation import parse_and_validate_tool_arguments

CONFIG = {
    "inject_tool_fields": {
        "reason": "One short, concrete sentence explaining the purpose of this tool call."
    },
    "stop_tools": [],
}


@pytest.fixture(scope="module")
def specs() -> dict:
    tools_data, _, _, _ = build_tools_data(CONFIG)
    return {name: entry["spec"] for name, entry in tools_data.items()}


def _validate(specs: dict, tool: str, args: dict) -> dict:
    return parse_and_validate_tool_arguments(
        tool_name=tool,
        arguments=args,
        tool_spec=specs[tool],
        allow_empty_for_empty_schema=False,
    )


def test_class_1_missing_reason(specs):
    args = _validate(specs, "fs_read_file", {"path": "/testbed/a.py"})
    assert args["path"] == "/testbed/a.py"


def test_class_2_minimal_edits(specs):
    args = _validate(
        specs,
        "fs_apply_file_edits",
        {"reason": "r", "path": "/testbed/a.py", "edits": [{"search": "a", "replace": "b"}]},
    )
    assert args["edits"][0]["replace"] == "b"


def test_class_3_numeric_strings(specs):
    args = _validate(
        specs,
        "fs_read_file",
        {"reason": "r", "path": "/testbed/a.py", "offset": "500.0", "limit": "200.0"},
    )
    assert (args["offset"], args["limit"]) == (500, 200)
