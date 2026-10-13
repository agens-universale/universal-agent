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


def test_the_package_reexports_the_function_of_the_submodule():
    """Функция живёт в подмодуле: его настоящим импортируют и те наборы, что
    подменяют пакет `gc_core.tools` заглушкой. Пакет отдаёт тот же объект.
    Импорт в теле теста — через `sys.modules` на момент вызова: плагин тест-кита
    после каждого теста возвращает настоящий пакет одним поколением модулей."""
    import importlib

    submodule = importlib.import_module("gc_core.tools.arguments_validation")
    package = importlib.import_module("gc_core.tools")

    assert package.tool_arguments_valid is submodule.tool_arguments_valid
    assert (
        submodule.tool_arguments_valid.__module__
        == "gc_core.tools.arguments_validation"
    )
    assert "tool_arguments_valid" in package.__all__
    assert "tool_arguments_valid" in submodule.__all__


def test_injected_service_field_is_removed_before_validation():
    import copy

    from gc_core.tools.inject_tool_fields import inject_property_into_spec

    entry = copy.deepcopy(_TOOLS_DATA["turn_ask_human"])
    entry["injected_fields"] = inject_property_into_spec(
        entry["spec"],
        field_name="service_metric",
        property_schema={"type": "number"},
    )
    assert (
        tool_arguments_valid(
            "turn_ask_human",
            '{"question": "which one?", "service_metric": {"bad": true}}',
            {"turn_ask_human": entry},
        )
        is True
    )
    assert (
        tool_arguments_valid(
            "turn_ask_human",
            '{"service_metric": 0.9}',
            {"turn_ask_human": entry},
        )
        is False
    )


def test_invalid_arguments_feedback_keeps_original_service_fields():
    import pytest

    from gc_core.tools.arguments_validation import (
        WrongFunctionArgumentsException,
        parse_and_validate_tool_arguments,
        prepare_tool_arguments,
    )

    entry = {
        **_TOOLS_DATA["turn_ask_human"],
        "injected_fields": ["service_metric"],
    }
    raw = '{"service_metric": 0.9}'
    prepared, _ = prepare_tool_arguments(raw, entry)
    with pytest.raises(WrongFunctionArgumentsException) as exc:
        parse_and_validate_tool_arguments(
            tool_name="turn_ask_human",
            arguments=raw,
            tool_spec=entry["spec"],
            allow_empty_for_empty_schema=True,
            prepared=prepared,
        )
    assert exc.value.received_args == raw
