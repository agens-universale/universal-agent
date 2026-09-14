import functools
import json
import math
from typing import Any, Dict, Type

from jsonschema import Draft7Validator

from gc_core.tools.connect_mcp.validation import _validate_tool_arguments
from gc_core.tools.schema_optional import normalize_optional_empty_as_omit, parameters_schema_for_local_validation, resolve_optional_property_keys


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
    except Exception:  # noqa: BLE001
        return None
    if hint is None:
        return None
    text = str(hint).strip()
    return text if text else None


def _describe_schema_type(spec: Dict[str, Any]) -> str:
    """Человекочитаемый тип поля для 'Correct call format'.
    Раскрывает anyOf/oneOf (object|array|...), а не наивный <any>."""
    if not isinstance(spec, dict):
        return "any"
    t = spec.get("type")
    if isinstance(t, str):
        return t
    if isinstance(t, list):
        return "|".join(str(x) for x in t if x != "null") or "any"
    for key in ("anyOf", "oneOf"):
        branches = spec.get(key)
        if isinstance(branches, list):
            types = _branch_type_names(branches)
            if types:
                return "|".join(types)
    return "any"


def _branch_type_names(branches: list) -> list[str]:
    """Уникальные не-null типы веток anyOf/oneOf в порядке появления."""
    types: list[str] = []
    for b in branches:
        bt = b.get("type") if isinstance(b, dict) else None
        if isinstance(bt, str) and bt != "null" and bt not in types:
            types.append(bt)
    return types


def _field_entries(params: Dict[str, Any]) -> "tuple[list[str], list[str]]":
    """Строки полей для 'Correct call format': (обязательные, опциональные)."""
    properties = params.get("properties", {}) or {}
    required_fields = params.get("required", []) or []
    optional_keys = set(resolve_optional_property_keys(params))
    required_entries: list[str] = []
    optional_entries: list[str] = []
    for field, spec in properties.items():
        ftype = _describe_schema_type(spec)
        fdesc = spec.get("description", "")
        fdesc_short = fdesc.split(".")[0] if fdesc else ""
        entry = f'  "{field}": <{ftype}>'
        if fdesc_short:
            entry += f"  // {fdesc_short}"
        if field not in optional_keys and field in required_fields:
            required_entries.append(entry)
        else:
            optional_entries.append(entry)
    return required_entries, optional_entries


def _join_field_lines(all_field_lines: list[str]) -> str:
    # Add comma after each field entry (non-comment lines), except the last one
    body_lines = []
    last_field_idx = max(
        i for i, line in enumerate(all_field_lines) if not line.strip().startswith("//")
    )
    for i, line in enumerate(all_field_lines):
        is_comment = line.strip().startswith("//")
        is_last_field = (not is_comment and i == last_field_idx)
        body_lines.append(line if is_comment or is_last_field else line + ",")
    return "\n".join(body_lines)


def _correct_call_format_body(expected_schema: Any) -> "str | None":
    """Тело JSON-блока 'Correct call format' или None, если полей нет."""
    params = expected_schema.get("parameters", {}) if isinstance(expected_schema, dict) else {}
    required_entries, optional_entries = _field_entries(params)
    if not required_entries and not optional_entries:
        return None
    all_field_lines: list[str] = []
    if required_entries:
        all_field_lines.append("  // REQUIRED")
        all_field_lines.extend(required_entries)
    if optional_entries:
        all_field_lines.append("  // OPTIONAL")
        all_field_lines.extend(optional_entries)
    return _join_field_lines(all_field_lines)


class WrongFunctionArgumentsException(Exception):
    def __init__(
        self,
        message: str,
        received_args: str,
        expected_schema: dict | None = None,
        tool_name: str | None = None,
        extra_hint: str | None = None,
        call_id: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.received_args = received_args
        self.expected_schema = expected_schema
        self.tool_name = tool_name
        self.extra_hint = extra_hint
        self.call_id = call_id

    def to_tool_output(self) -> str:
        """Отказ валидации как output вызова — тот же контракт, что у unknown_tool.

        Батч не прерывается: этот JSON становится function_call_output упавшего
        вызова, остальные вызовы хода исполняются.
        """
        payload = {
            "ok": False,
            "error": {
                "type": "invalid_arguments",
                "tool": self.tool_name,
                "message": self.message,
            },
            "message": self.to_llm_feedback(),
        }
        return json.dumps(payload, ensure_ascii=False)

    def to_llm_feedback(self) -> str:
        """
        Формирует четкое и структурированное сообщение об ошибке для LLM.
        """
        tool_label = f"`{self.tool_name}`" if self.tool_name else "tool"
        feedback = f"❌ Wrong arguments for {tool_label}: {self.message}\n\n"

        # Исходные аргументы уже находятся в парном function_call и связаны с
        # этим output через call_id. Не дублируем проблемный payload в контекст.

        if self.extra_hint:
            hint = str(self.extra_hint).strip()
            feedback += hint if hint.endswith("\n") else f"{hint}\n"
            if not feedback.endswith("\n\n"):
                feedback += "\n"

        if self.expected_schema:
            body = _correct_call_format_body(self.expected_schema)
            if body is not None:
                feedback += f"Correct call format:\n```json\n{{\n{body}\n}}\n```\n\n"

        feedback += "Fix the arguments and retry the tool call."
        return feedback


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
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(decoded, (dict, list)):
        return None
    return decoded if Draft7Validator(subschema).is_valid(decoded) else None


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


def _numeric_target_schema(subschema: Dict[str, Any]) -> "tuple[str, Dict[str, Any]] | None":
    """(ожидаемый числовой type, схема для валидации) или None, если поле не числовое."""
    expected = subschema.get("type")
    if expected is not None:
        return (expected, subschema) if expected in ("integer", "number") else None
    # anyOf/oneOf without a top-level type (e.g. `occurrence` in
    # fs_search_and_replace / fs_apply_file_edits): locate a numeric branch
    # so numeric strings are coerced like plain integer/number fields.
    composite = subschema.get("anyOf") or subschema.get("oneOf")
    if not isinstance(composite, list):
        return None
    for branch in composite:
        if isinstance(branch, dict) and branch.get("type") in ("integer", "number"):
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


def _parse_arguments_object(arguments: Any, reject, *, allow_empty: bool) -> Any:
    """dict из аргументов (dict / JSON-строка); reject(message=…) бросает при пустоте или битом JSON."""
    if isinstance(arguments, dict):
        return arguments
    arg_str = "" if arguments is None else str(arguments).strip()
    if arg_str == "":
        if allow_empty:
            return {}
        reject(message="аргументы инструмента пустые, ожидается валидный JSON-объект")
    try:
        return json.loads(arg_str)
    except Exception as e:  # noqa: BLE001
        reject(message=f"некорректный JSON: {e}")


def _recover_arguments(tool_name: str, parsed_args: Dict[str, Any], params_schema: Dict[str, Any], validation_schema: Any) -> "Dict[str, Any] | None":
    """Коэрсеры по очереди; первый вариант, проходящий валидацию, либо None."""
    candidate = parsed_args
    # Порядок: сначала снимаем «не задано», потом чиним типы. Обратный
    # порядок оставил бы "" в enum-поле и второй коэрсер об него споткнулся.
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
        except Exception:  # noqa: BLE001
            continue
        return candidate
    return None


def parse_and_validate_tool_arguments(
    *,
    tool_name: str,
    arguments: str | Dict[str, Any] | None,
    tool_spec: Dict[str, Any],
    allow_empty_for_empty_schema: bool,
    tool_fabric: Type[Any] | None = None,
) -> Dict[str, Any]:
    params_schema = tool_spec.get("parameters", {}) if isinstance(tool_spec, dict) else {}
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
    parsed_args = _parse_arguments_object(
        arguments, reject, allow_empty=allow_empty_for_empty_schema and is_empty_schema
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

    validation_schema = parameters_schema_for_local_validation(params_schema)
    try:
        _validate_tool_arguments(tool_name, validation_schema, parsed_args)
    except Exception as e:  # noqa: BLE001
        recovered_args = _recover_arguments(tool_name, parsed_args, params_schema, validation_schema)
        if recovered_args is not None:
            parsed_args = recovered_args
        else:
            _raise_wrong_function_arguments(
                message=str(e),
                received_args=json.dumps(parsed_args, ensure_ascii=False),
                expected_schema=tool_spec,
                tool_name=tool_name,
                tool_fabric=tool_fabric,
                parsed_args=parsed_args,
            )

    return normalize_optional_empty_as_omit(parsed_args, params_schema)


__all__ = [
    "WrongFunctionArgumentsException",
    "parse_and_validate_tool_arguments",
    "resolve_tool_arguments_feedback_hint",
]
