import json
import re
from itertools import pairwise
from typing import Any, Dict

from gc_core.execution.tool_schema import resolve_optional_property_keys


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
        i
        for i, line in enumerate(all_field_lines)
        if not line.strip().startswith("//")
    )
    for i, line in enumerate(all_field_lines):
        is_comment = line.strip().startswith("//")
        is_last_field = not is_comment and i == last_field_idx
        body_lines.append(line if is_comment or is_last_field else line + ",")
    return "\n".join(body_lines)


def _correct_call_format_body(expected_schema: Any) -> "str | None":
    """Тело JSON-блока 'Correct call format' или None, если полей нет."""
    params = (
        expected_schema.get("parameters", {})
        if isinstance(expected_schema, dict)
        else {}
    )
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


#: Фрагмент битых аргументов в ответе: начало, место ошибки и конец
#: (у оборванной строки парсер указывает на её начало, обрыв — в конце).
_PREVIEW_EDGE = 300
_PREVIEW_AROUND = 150


def _preview_spans(size: int, error_at: int) -> list[list[int]]:
    """Отрезки [начало, конец) для показа; пересекающиеся склеены."""
    spans = sorted(
        [
            [0, _PREVIEW_EDGE],
            [error_at - _PREVIEW_AROUND, error_at + _PREVIEW_AROUND],
            [size - _PREVIEW_EDGE, size],
        ]
    )
    merged: list[list[int]] = []
    for start, end in spans:
        start, end = max(0, start), min(size, end)
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _broken_arguments_preview(raw: Any) -> "str | None":
    """Фрагмент аргументов, которые не JSON-объект; иначе None.

    Такие аргументы уходят на бэкенд как ``{}`` — что модель отправила и где
    сломалось, видно только отсюда. Объект модель видит в самом вызове.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed, error_at = json.loads(raw), len(raw)
    except (ValueError, RecursionError) as e:
        # Глубокая вложенность (модель зациклилась на «[») — RecursionError.
        parsed, error_at = None, getattr(e, "pos", len(raw))
    if isinstance(parsed, dict):
        return None
    spans = _preview_spans(len(raw), error_at)
    parts = [raw[spans[0][0] : spans[0][1]]]
    for (_, prev_end), (start, end) in pairwise(spans):
        parts.append(f"… [{start - prev_end} chars omitted] …")
        parts.append(raw[start:end])
    body = "\n".join(parts)
    # Ограда длиннее любой серии обратных кавычек внутри — не закроется.
    longest = max((len(run) for run in re.findall("`+", body)), default=0)
    fence = "`" * max(3, longest + 1)
    omitted = ", middle omitted" if len(spans) > 1 else ""
    return (
        f"Received arguments ({len(raw)} chars{omitted}):\n"
        f"{fence}\n{body}\n{fence}\n\n"
    )


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

        preview = _broken_arguments_preview(self.received_args)
        if preview is not None:
            feedback += preview

        if self.extra_hint:
            hint = str(self.extra_hint).strip()
            feedback += hint if hint.endswith("\n") else f"{hint}\n"
            if not feedback.endswith("\n\n"):
                feedback += "\n"

        if self.expected_schema:
            body = _correct_call_format_body(self.expected_schema)
            if body is not None:
                feedback += (
                    f"Correct call format:\n```json\n{{\n{body}\n}}\n```\n\n"
                )

        feedback += "Fix the arguments and retry the tool call."
        return feedback
