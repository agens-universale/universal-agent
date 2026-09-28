"""tools.tool_arguments_valid — булев ответ по тем же правилам, что и apply_tool."""

from gc_core.tools import tool_arguments_valid

_SPEC = {
    "name": "turn_ask_human",
    "parameters": {
        "type": "object",
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
        "additionalProperties": False,
    },
}
_TOOLS_DATA = {
    "turn_ask_human": {
        "spec": _SPEC,
        "tool_fabric": None,
        "injected_fields": [],
    }
}


def test_valid_arguments():
    assert (
        tool_arguments_valid(
            "turn_ask_human", '{"question": "which one?"}', _TOOLS_DATA
        )
        is True
    )


def test_unexpected_field_is_invalid():
    assert (
        tool_arguments_valid("turn_ask_human", '{"bogus": 1}', _TOOLS_DATA)
        is False
    )


def test_malformed_json_is_invalid():
    assert (
        tool_arguments_valid("turn_ask_human", '{"question": ', _TOOLS_DATA)
        is False
    )


def test_unknown_tool_is_not_judged():
    assert (
        tool_arguments_valid("no_such_tool", '{"bogus": 1}', _TOOLS_DATA)
        is True
    )
