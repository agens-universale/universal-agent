import functools
import json
import math
from typing import Any, Dict, Type

from jsonschema import Draft7Validator

from gc_core.execution.tool_schema import (
    normalize_optional_empty_as_omit,
    parameters_schema_for_local_validation,
)
from gc_core.tools.connect_mcp.validation import _validate_tool_arguments
from gc_core.tools.inject_tool_fields import strip_injected_tool_args


def resolve_tool_arguments_feedback_hint(
    tool_fabric: Type[Any] | None,
    *,
    received_args: str,
    parsed_args: dict[str, Any] | None,
    message: str,
) -> str | None:
    """Delegate optional hint to tool class when it defines wrong_arguments_feedback_hint."""
    if tool_fabric is None:
        return None
    hook = getattr(tool_fabric, "wrong_arguments_feedback_hint", None)
    if not callable(hook):
        return None
    try:
        hint = hook(
            received_args=received_args,
            parsed_args=parsed_args,
            _message=message,
        )
    except Exception:
        return None
    if hint is None:
        return None
    text = str(hint).strip()
    return text if text else None


from gc_core.execution.arguments_error import WrongFunctionArgumentsException


def _raise_wrong_function_arguments(
    *,
    message: str,
    received_args: str,
    expected_schema: dict | None,
    tool_name: str,
    tool_fabric: Type[Any] | None,
    parsed_args: dict[str, Any] | None,
) -> None:
    raise WrongFunctionArgumentsException(
        message=message,
        received_args=received_args,
        expected_schema=expected_schema,
        tool_name=tool_name,
        extra_hint=resolve_tool_arguments_feedback_hint(
            tool_fabric,
            received_args=received_args,
            parsed_args=parsed_args,
            message=message,
        ),
    )


def _coerce_double_encoded_fields(
    parsed_args: Dict[str, Any], params_schema: Dict[str, Any]
) -> tuple[Dict[str, Any], bool]:
    """Recovery-проход: развернуть поля, пришедшие JSON-строкой второго уровня.

    Для каждого строкового значения, которое не проходит свою под-схему, пробуем
    json.loads; принимаем только dict/list, который под-схему проходит. Строки,
    валидные как есть, не трогаем (поэтому "123" не превращается в 42). Возвращаем
    (изменённая_копия_или_исходник, changed)."""
    properties = params_schema.get("properties", {}) or {}
    if not isinstance(parsed_args, dict) or not properties:
        return parsed_args, False
    coerced = dict(parsed_args)
    changed = False
    for field, value in parsed_args.items():
        subschema = properties.get(field)
        if not isinstance(value, str) or not isinstance(subschema, dict):
            continue
        decoded = _decode_double_encoded(value, subschema)
        if decoded is not None:
            coerced[field] = decoded
            changed = True
    return coerced, changed


def _decode_double_encoded(value: str, subschema: Dict[str, Any]) -> Any:
    """dict/list из JSON-строки, если сама строка под-схему не проходит, а декод проходит; иначе None."""
    if Draft7Validator(subschema).is_valid(value):
        return None  # строка валидна как есть — не трогаем
    try:
        decoded = json.loads(value)
    except Exception:
        return None
    if not isinstance(decoded, (dict, list)):
        return None
    return decoded if Draft7Validator(subschema).is_valid(decoded) else None


def unknown_property_names(
    parsed_args: Dict[str, Any], params_schema: Dict[str, Any]
) -> list[str]:
    """Ключи верхнего уровня, которых нет в ``properties``.

    ``additionalProperties`` — инструкция модели в схеме, не рантайм-политика:
    лишнее на корне снимаем всегда, даже если схема его формально разрешает.
    """
    properties = params_schema.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}
    return [key for key in parsed_args if key not in properties]


def format_dropped_arguments_note(names: list[str]) -> str:
    """Информационная строка для вывода тула: какие поля сняты и почему."""
    listed = ", ".join(names)
    if len(names) == 1:
        return (
            f"Field {listed} was dropped because it is not in the tool schema."
        )
    return f"Fields {listed} were dropped because they are not in the tool schema."


def annotate_output_with_dropped_arguments(
    output: str, dropped: list[str]
) -> str:
    """Вставить заметку об отброшенных полях в вывод тула.

    JSON-объект получает поле ``dropped_arguments`` первым, чтобы заметка
    попала в префикс длинного ответа. Иной текст — строка перед выводом.
    """
    if not dropped:
        return output
    note = format_dropped_arguments_note(dropped)
    stripped = output.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and "dropped_arguments" not in data:
            return json.dumps(
                {"dropped_arguments": note, **data}, ensure_ascii=False
            )
    if not output:
        return note
    return f"{note}\n{output}"


def _drop_unknown_properties(
    parsed_args: Dict[str, Any], params_schema: Dict[str, Any]
) -> tuple[Dict[str, Any], list[str]]:
    """Снять ключи верхнего уровня, которых нет в ``properties``.

    Не зависит от ``additionalProperties``: это контракт для модели. Вложенные
    объекты не трогаем — там лишний ключ может быть частью данных. Срез сам по
    себе вызов не спасает: если после него не хватает required или не сходятся
    типы, отказ сохраняется, а список снятых ключей наружу не отдаём.
    """
    unknown = unknown_property_names(parsed_args, params_schema)
    if not unknown:
        return parsed_args, []
    properties = params_schema.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}
    stripped = {
        key: value for key, value in parsed_args.items() if key in properties
    }
    return stripped, unknown


def _drop_empty_optional_keys(
    parsed_args: Dict[str, Any], params_schema: Dict[str, Any]
) -> tuple[Dict[str, Any], bool]:
    """Recovery-проход: необязательный ключ с "" или null — это «не задано».

    Зачем отдельный проход, если ниже и так вызывается
    ``normalize_optional_empty_as_omit``: та отрабатывает ПОСЛЕ валидации, а до
    неё дело не доходит, если "" не проходит под-схему. Так и было с enum-полями
    (``fs_write_file.newline``, ``.mode``, ``fs_file_info.hash_algorithm``,
    ``fs_search_and_replace.occurrence``): подсказка в описании обещает модели,
    что пустую строку примут, а Draft7 отвергает её как значение не из enum.
    Модель следует контракту — и получает ошибку аргументов на ровном месте.

    Семантической дельты нет: проход тождественен нормализации ниже, он лишь
    перенесён на failure-путь. Поля с ``x-preserve-empty-string`` (например
    ``separator`` у fs_append_file) в список необязательных не попадают, для них
    "" остаётся значением.
    """
    coerced = normalize_optional_empty_as_omit(parsed_args, params_schema)
    return coerced, coerced != parsed_args


def _numeric_target_schema(
    subschema: Dict[str, Any],
) -> "tuple[str, Dict[str, Any]] | None":
    """(ожидаемый числовой type, схема для валидации) или None, если поле не числовое."""
    expected = subschema.get("type")
    if expected is not None:
        return (
            (expected, subschema)
            if expected in ("integer", "number")
            else None
        )
    # anyOf/oneOf without a top-level type (e.g. `occurrence` in
    # fs_search_and_replace / fs_apply_file_edits): locate a numeric branch
    # so numeric strings are coerced like plain integer/number fields.
    composite = subschema.get("anyOf") or subschema.get("oneOf")
    if not isinstance(composite, list):
        return None
    for branch in composite:
        if isinstance(branch, dict) and branch.get("type") in (
            "integer",
            "number",
        ):
            return branch["type"], branch
    return None


def _parse_numeric_string(value: str, expected: str) -> Any:
    """Число из строки под ожидаемый тип; None — парс неоднозначен/невозможен."""
    try:
        num: Any = float(value)
    except ValueError:
        return None
    if not math.isfinite(num):
        return None
    if expected == "integer":
        if not float(num).is_integer():
            return None
        num = int(num)
    return num


def _coerce_numeric_strings(
    parsed_args: Dict[str, Any], params_schema: Dict[str, Any]
) -> tuple[Dict[str, Any], bool]:
    """Recovery-проход: строка там, где схема ждёт число, и парс однозначен → число.

    Консервативно: только для полей с type integer/number; дробное значение в
    integer-поле не принимаем; не-конечные значения (inf/nan из "1e999") отвергаем;
    строковые поля не трогаем (поэтому "123" в string-поле остаётся строкой)."""
    properties = params_schema.get("properties", {}) or {}
    if not isinstance(parsed_args, dict) or not properties:
        return parsed_args, False
    coerced = dict(parsed_args)
    changed = False
    for field, value in parsed_args.items():
        if not isinstance(value, str):
            continue
        subschema = properties.get(field)
        if not isinstance(subschema, dict):
            continue
        numeric = _numeric_target_schema(subschema)
        if numeric is None:
            continue
        expected, validate_schema = numeric
        num = _parse_numeric_string(value, expected)
        if num is not None and Draft7Validator(validate_schema).is_valid(num):
            coerced[field] = num
            changed = True
    return coerced, changed


def _parse_arguments_object(
    arguments: Any, reject, *, allow_empty: bool
) -> Any:
    """dict из аргументов (dict / JSON-строка); reject(message=…) бросает при пустоте или битом JSON."""
    if isinstance(arguments, dict):
        return arguments
    arg_str = "" if arguments is None else str(arguments).strip()
    if arg_str == "":
        if allow_empty:
            return {}
        reject(
            message="аргументы инструмента пустые, ожидается валидный JSON-объект"
        )
    try:
        return json.loads(arg_str)
    except Exception as e:
        reject(message=f"некорректный JSON: {e}")


def _recover_arguments(
    tool_name: str,
    parsed_args: Dict[str, Any],
    params_schema: Dict[str, Any],
    validation_schema: Any,
) -> "Dict[str, Any] | None":
    """Коэрсеры по очереди; первый вариант, проходящий валидацию, либо None."""
    candidate = parsed_args
    # Пустую строку нельзя оставлять в enum-поле до числового коэрсера —
    # он об неё споткнётся.
    for coercer in (
        _drop_empty_optional_keys,
        _coerce_double_encoded_fields,
        _coerce_numeric_strings,
    ):
        coerced, changed = coercer(candidate, params_schema)
        if not changed:
            continue
        candidate = coerced
        try:
            _validate_tool_arguments(tool_name, validation_schema, candidate)
        except Exception:
            continue
        return candidate
    return None


def prepare_tool_arguments(
    raw: str | Dict[str, Any] | None, entry: Dict[str, Any]
) -> tuple[Dict[str, Any] | None, Dict[str, Any]]:
    """Разбор JSON → `prepare_args_before_strip` фабрики → снятие служебных полей
    (`entry["injected_fields"]`). Единственный путь для всех потребителей аргументов
    (исполнение, вердикт финализатора, отпечаток loop-детектора, чтение оценок —
    решение 6 канона автороутинга): служебное поле снимается ДО валидации схемы,
    мусор в нём вызов не роняет. Возвращает `(аргументы_без_служебных, служебные)`;
    `(None, {})` — не JSON-объект (валидация ниже даст честную ошибку по исходной строке)."""
    if isinstance(raw, dict):
        parsed: Any = dict(raw)
    else:
        text = "" if raw is None else str(raw).strip()
        if not text:
            # Пустые аргументы у тула без собственных параметров — пустой объект:
            # служебные поля (`injected_fields`) параметрами тула не считаются,
            # иначе инжект оценки в auto отбивал бы `""` у тула без параметров.
            return ({}, {}) if _has_no_own_parameters(entry) else (None, {})
        parsed = _json_or_none(text)
    parsed = _fabric_prepared_object(parsed, entry)
    if parsed is None:
        return None, {}
    injected = [
        f for f in (entry.get("injected_fields") or []) if isinstance(f, str)
    ]
    service = {name: parsed[name] for name in injected if name in parsed}
    return strip_injected_tool_args(parsed, injected_fields=injected), service


def _json_or_none(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _fabric_prepared_object(
    parsed: Any, entry: Dict[str, Any]
) -> "Dict[str, Any] | None":
    """JSON-объект после `prepare_args_before_strip` фабрики; не объект до или
    после хука — `None`."""
    if not isinstance(parsed, dict):
        return None
    prepare = getattr(
        entry.get("tool_fabric"), "prepare_args_before_strip", None
    )
    if callable(prepare):
        parsed = prepare(parsed)
    return parsed if isinstance(parsed, dict) else None


def _has_no_own_parameters(entry: Dict[str, Any]) -> bool:
    """Все свойства схемы записи — служебные поля (`injected_fields`) либо их нет."""
    spec = entry.get("spec")
    if not isinstance(spec, dict):
        return False
    params = spec.get("parameters") if isinstance(spec, dict) else None
    properties = params.get("properties") if isinstance(params, dict) else None
    return set(properties or {}) <= set(entry.get("injected_fields") or [])


def arguments_without_service_fields(raw: Any, entry: Dict[str, Any]) -> Any:
    """Аргументы вызова для потребителей вне исполнения тула — возврат хода,
    судья finish gate, intent-scope: служебные поля (`entry["injected_fields"]`)
    сняты тем же помощником `prepare_tool_arguments`. Ничего не снято или
    аргументы не JSON-объект — исходное значение байт в байт."""
    args, service = prepare_tool_arguments(raw, entry)
    if args is None or not service:
        return raw
    return json.dumps(args, ensure_ascii=False)


def parse_and_validate_tool_arguments(
    *,
    tool_name: str,
    arguments: str | Dict[str, Any] | None,
    tool_spec: Dict[str, Any],
    allow_empty_for_empty_schema: bool,
    tool_fabric: Type[Any] | None = None,
    prepared: Dict[str, Any] | None = None,
    dropped_unknown: list[str] | None = None,
) -> Dict[str, Any]:
    params_schema = (
        tool_spec.get("parameters", {}) if isinstance(tool_spec, dict) else {}
    )
    params_schema = params_schema if isinstance(params_schema, dict) else {}
    properties = params_schema.get("properties", {}) or {}
    required = params_schema.get("required", []) or []
    is_empty_schema = not properties and not required

    reject = functools.partial(
        _raise_wrong_function_arguments,
        received_args=str(arguments),
        expected_schema=tool_spec,
        tool_name=tool_name,
        tool_fabric=tool_fabric,
        parsed_args=None,
    )
    parsed_args = (
        prepared
        if prepared is not None
        else _parse_arguments_object(
            arguments,
            reject,
            allow_empty=allow_empty_for_empty_schema and is_empty_schema,
        )
    )
    if not isinstance(parsed_args, dict):
        _raise_wrong_function_arguments(
            message="аргументы инструмента должны быть JSON-объектом",
            received_args=str(arguments),
            expected_schema=tool_spec,
            tool_name=tool_name,
            tool_fabric=tool_fabric,
            parsed_args=None,
        )

    # Лишние ключи корня снимаем до валидации, иначе схема без
    # additionalProperties: false их пропустит и они доедут до тула.
    parsed_args, dropped_names = _drop_unknown_properties(
        parsed_args, params_schema
    )

    validation_schema = parameters_schema_for_local_validation(params_schema)
    try:
        _validate_tool_arguments(tool_name, validation_schema, parsed_args)
    except Exception as e:
        recovered_args = _recover_arguments(
            tool_name, parsed_args, params_schema, validation_schema
        )
        if recovered_args is not None:
            parsed_args = recovered_args
        else:
            _raise_wrong_function_arguments(
                message=str(e),
                received_args=(
                    str(arguments)
                    if prepared is not None
                    else json.dumps(parsed_args, ensure_ascii=False)
                ),
                expected_schema=tool_spec,
                tool_name=tool_name,
                tool_fabric=tool_fabric,
                parsed_args=parsed_args,
            )

    if dropped_unknown is not None and dropped_names:
        dropped_unknown.extend(dropped_names)
    return normalize_optional_empty_as_omit(parsed_args, params_schema)


def tool_arguments_valid(
    tool_name: str,
    arguments: Any,
    tools_data_: Dict[str, Dict[str, Any]],
) -> bool:
    """True, если аргументы прошли бы валидацию aapply_tool (те же правила и spec).

    Неизвестный тул или запись без spec не судим (True): решать будет сам
    aapply_tool. Исключение ловится здесь же, поэтому результат не зависит от
    того, какое поколение модуля держит вызывающая сторона.
    """
    entry = tools_data_.get(tool_name) or {}
    spec = entry.get("spec")
    if not isinstance(spec, dict):
        return True
    try:
        prepared, _service = prepare_tool_arguments(arguments, entry)
        parse_and_validate_tool_arguments(
            tool_name=tool_name,
            arguments=arguments,
            tool_spec=spec,
            allow_empty_for_empty_schema=True,
            tool_fabric=entry.get("tool_fabric"),
            prepared=prepared,
        )
    except WrongFunctionArgumentsException:
        return False
    return True


__all__ = [
    "WrongFunctionArgumentsException",
    "annotate_output_with_dropped_arguments",
    "arguments_without_service_fields",
    "format_dropped_arguments_note",
    "parse_and_validate_tool_arguments",
    "prepare_tool_arguments",
    "resolve_tool_arguments_feedback_hint",
    "tool_arguments_valid",
]
