"""Фрагмент полученных аргументов в ответе на битый вызов.

Битые аргументы уходят на бэкенд как ``{}``; что модель отправила и где
сломалось — видно только из ответа тула.
"""

import pytest
from gc_core.execution.arguments_error import WrongFunctionArgumentsException
from gc_core.tools.arguments_validation import (
    parse_and_validate_tool_arguments,
)


def _feedback(received_args):
    return WrongFunctionArgumentsException(
        message="некорректный JSON",
        received_args=received_args,
        tool_name="fs_write_file",
    ).to_llm_feedback()


def test_cut_off_arguments_show_start_and_the_place_of_the_cut():
    cut = '{"path": "a.md", "content": "' + "а" * 5000 + "КОНЕЦ"

    feedback = _feedback(cut)

    assert f"Received arguments ({len(cut)} chars" in feedback
    assert '{"path": "a.md", "content": "' in feedback
    assert "аКОНЕЦ" in feedback
    assert "chars omitted]" in feedback
    assert "а" * 1000 not in feedback


def test_error_in_the_middle_shows_its_surroundings():
    broken = (
        '{"path": "a.md", "content": "'
        + "x" * 3000
        + "ОШИБКА\\q"
        + "y" * 3000
        + '"}'
    )

    feedback = _feedback(broken)

    assert "ОШИБКА\\q" in feedback
    assert "y" * 3000 not in feedback


def test_short_broken_arguments_are_shown_whole():
    feedback = _feedback('{"path": "a.md", "content": "нача')

    assert '{"path": "a.md", "content": "нача' in feedback
    assert "omitted" not in feedback


def test_json_that_is_not_an_object_is_shown():
    assert "[1, 2]" in _feedback("[1, 2]")


def test_valid_object_is_not_repeated():
    """Ошибка схемы: аргументы уходят как есть, модель видит их в вызове."""
    feedback = _feedback('{"path": "a.md", "bogus": 1}')

    assert "Received arguments" not in feedback
    assert "bogus" not in feedback


def test_deeply_nested_arguments_do_not_crash_the_feedback():
    """Модель зациклилась на ``[`` до лимита — ответ тула всё равно есть."""
    feedback = _feedback("[" * 200000)

    assert "Received arguments (200000 chars, middle omitted)" in feedback


def test_backticks_inside_arguments_do_not_close_the_block():
    feedback = _feedback('{"content": "```python\nprint(1)\n```\nобры')

    assert "\n````\n" in feedback


def _schema_error(arguments):
    spec = {
        "name": "fs_read_file",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }
    with pytest.raises(WrongFunctionArgumentsException) as caught:
        parse_and_validate_tool_arguments(
            tool_name="fs_read_file",
            arguments=arguments,
            tool_spec=spec,
            allow_empty_for_empty_schema=False,
            prepared=arguments if isinstance(arguments, dict) else None,
        )
    return caught.value


def test_dict_arguments_are_reported_as_json_not_python_repr():
    """Словарь от бэкенда уходит своим JSON — фрагмент не нужен."""
    error = _schema_error({"path": 1})

    assert error.received_args == '{"path": 1}'
    assert "Received arguments" not in error.to_llm_feedback()


def test_missing_arguments_are_not_shown_as_none():
    error = _schema_error(None)

    assert "None" not in error.to_llm_feedback()
