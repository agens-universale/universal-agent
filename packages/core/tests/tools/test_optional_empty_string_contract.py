"""Контракт «пустая строка = не задано» должен выполняться, а не только обещаться.

Схема, уходящая в LLM, объявляет все свойства обязательными (strict-режим
OpenAI), поэтому необязательное поле нельзя опустить — вместо этого к описанию
дописывается подсказка «не нужно → пришли пустую строку». Модель ей следовала и
получала отказ:

    path='newline': '' is not one of ['preserve', 'lf', 'crlf']

Причина — порядок операций: Draft7-валидация идёт ДО normalize_optional_empty_as_omit,
и для enum-поля "" не доживает до нормализации. Флаг backend.openai_strict_tools_schema
на это не влияет: подсказка навешивается при обоих значениях, а при False она даже
прямее обещает, что "" примут.

Тесты закрывают обе стороны: подсказку не вешаем туда, где она ложна, и всё равно
принимаем "", если модель его прислала.
"""

from __future__ import annotations

import copy
import json

import pytest
from gc_core.tools.arguments_validation import (
    WrongFunctionArgumentsException,
    parse_and_validate_tool_arguments,
)
from gc_core.tools.fs.append_to_file import AppendToFile
from gc_core.tools.fs.file_info import FileInfo
from gc_core.tools.fs.write_to_file_tool import WriteToFile
from gc_core.execution.tool_schema import (
    apply_openai_strict_parameters,
    augment_optional_property_descriptions,
    resolve_optional_property_keys,
)

# Формулировка подсказки зависит от strict-флага («pass empty string ""» против
# «Empty string "" is also accepted»), общая часть — вот эта.
_EMPTY_STRING_PROMISE = "empty string"


def _spec(fabric, *, strict: bool) -> dict:
    spec = copy.deepcopy(fabric.tool_description())
    if strict:
        apply_openai_strict_parameters(spec["parameters"])
    augment_optional_property_descriptions(spec, openai_strict=strict)
    return spec


def _call(fabric, tool_name: str, args: dict, *, strict: bool) -> dict:
    return parse_and_validate_tool_arguments(
        tool_name=tool_name,
        arguments=json.dumps(args),
        tool_spec=_spec(fabric, strict=strict),
        allow_empty_for_empty_schema=False,
    )


# Поля с enum: пустая строка под-схему не проходит.
_ENUM_CASES = [
    (
        WriteToFile,
        "fs_write_file",
        {"path": "/tmp/x.txt", "content": "hi"},
        "newline",
    ),
    (
        WriteToFile,
        "fs_write_file",
        {"path": "/tmp/x.txt", "content": "hi"},
        "mode",
    ),
    (FileInfo, "fs_file_info", {"path": "/tmp/x.txt"}, "hash_algorithm"),
]


@pytest.mark.parametrize("strict", [True, False], ids=["strict", "non-strict"])
@pytest.mark.parametrize("fabric, tool_name, base, field", _ENUM_CASES)
def test_empty_string_in_enum_field_is_treated_as_unset(
    fabric, tool_name, base, field, strict
):
    out = _call(fabric, tool_name, {**base, field: ""}, strict=strict)
    assert field not in out, (
        f"{field}='' должен сниматься, а не доезжать до инструмента"
    )
    for key, value in base.items():
        assert out[key] == value


@pytest.mark.parametrize("strict", [True, False], ids=["strict", "non-strict"])
@pytest.mark.parametrize("fabric, tool_name, base, field", _ENUM_CASES)
def test_enum_field_without_empty_string_promise(
    fabric, tool_name, base, field, strict
):
    """Подсказку не вешаем туда, где она ложна — иначе модель идёт в гарантированный отказ."""
    props = _spec(fabric, strict=strict)["parameters"]["properties"]
    assert (
        _EMPTY_STRING_PROMISE
        not in (props[field].get("description") or "").lower()
    )


@pytest.mark.parametrize("strict", [True, False], ids=["strict", "non-strict"])
def test_free_form_string_keeps_the_promise(strict):
    """У обычного строкового поля подсказка остаётся: там "" действительно принимается."""
    props = _spec(WriteToFile, strict=strict)["parameters"]["properties"]
    assert (
        _EMPTY_STRING_PROMISE
        in (props["encoding"].get("description") or "").lower()
    )
    out = _call(
        WriteToFile,
        "fs_write_file",
        {"path": "/tmp/x.txt", "content": "hi", "encoding": ""},
        strict=strict,
    )
    assert "encoding" not in out


@pytest.mark.parametrize("strict", [True, False], ids=["strict", "non-strict"])
def test_real_enum_value_survives(strict):
    out = _call(
        WriteToFile,
        "fs_write_file",
        {"path": "/tmp/x.txt", "content": "hi", "newline": "lf"},
        strict=strict,
    )
    assert out["newline"] == "lf"


@pytest.mark.parametrize("strict", [True, False], ids=["strict", "non-strict"])
def test_garbage_enum_value_still_rejected(strict):
    """Послабление касается только пустой строки: мусор обязан падать как раньше."""
    with pytest.raises(WrongFunctionArgumentsException):
        _call(
            WriteToFile,
            "fs_write_file",
            {"path": "/tmp/x.txt", "content": "hi", "newline": "zz"},
            strict=strict,
        )


def test_preserve_empty_string_field_is_untouched():
    """Для separator у fs_append_file пустая строка — значимое значение, не «не задано»."""
    spec = _spec(AppendToFile, strict=True)
    assert "separator" not in resolve_optional_property_keys(
        spec["parameters"]
    )
    out = _call(
        AppendToFile,
        "fs_append_file",
        {"path": "/tmp/x.txt", "content": "hi", "separator": ""},
        strict=True,
    )
    assert out["separator"] == ""
