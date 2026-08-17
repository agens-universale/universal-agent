# Импортируем ОБА символа из одного module-top-биндинга: autouse-фикстура
# ensure_real_tools_package перегружает tools.* между сбором и прогоном, поэтому
# импорт исключения внутри теста дал бы ДРУГОЙ класс → pytest.raises не ловил бы
# исключение, поднятое parse_and_validate (старый класс). Один импорт — одна личность.
from gc_core.tools.arguments_validation import WrongFunctionArgumentsException, parse_and_validate_tool_arguments

_SPEC = {
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            # поле-объект через anyOf без строковой ветки (как mcp ...testUnit.create.attributes)
            "attributes": {"anyOf": [{"type": "object"}, {"type": "null"}]},
        },
        "required": ["summary"],
        "additionalProperties": False,
    }
}

def test_double_encoded_object_is_coerced():
    # attributes пришёл JSON-строкой второго уровня
    args = '{"summary": "TC1", "attributes": "{\\"test_level\\": \\"UI\\"}"}'
    out = parse_and_validate_tool_arguments(
        tool_name="t", arguments=args, tool_spec=_SPEC,
        allow_empty_for_empty_schema=False,
    )
    assert out["attributes"] == {"test_level": "UI"}  # развёрнут в объект

def test_string_valid_as_is_not_touched():
    spec = {"parameters": {"type": "object",
            "properties": {"x": {"anyOf": [{"type": "string"}, {"type": "object"}]}},
            "required": [], "additionalProperties": False}}
    out = parse_and_validate_tool_arguments(
        tool_name="t", arguments='{"x": "123"}', tool_spec=spec,
        allow_empty_for_empty_schema=False,
    )
    assert out["x"] == "123"  # строка валидна по строковой ветке → НЕ коэрсим в 123

def test_unrecoverable_still_rejected():
    import pytest
    # строка, чей json.loads даёт скаляр (не dict/list) → коэрция не применяется
    args = '{"summary": "TC1", "attributes": "42"}'
    with pytest.raises(WrongFunctionArgumentsException):
        parse_and_validate_tool_arguments(
            tool_name="t", arguments=args, tool_spec=_SPEC,
            allow_empty_for_empty_schema=False,
        )
