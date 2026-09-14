import json
import re
from typing import Any

_FENCE_OPEN = re.compile(r"```(?:json)?[ \t]*\r?\n?", re.IGNORECASE)


def _fenced_json_object(raw: str) -> str | None:
    """Первый fenced-блок, содержимое которого выглядит как JSON-объект."""
    for opening in _FENCE_OPEN.finditer(raw):
        close = raw.find("```", opening.end())
        if close == -1:
            return None
        body = raw[opening.end():close].strip()
        if body.startswith("{") and body.endswith("}"):
            return body
    return None


def extract_json_candidate(text: str) -> str:
    """
    Return best-effort JSON object candidate from arbitrary text.

    Order:
    1. markdown fence `` ```json {…} ``` ``;
    2. whole text if it already starts with ``{``;
    3. slice from first ``{`` to last ``}`` (prose/preamble before JSON);
    4. otherwise the stripped raw text.
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    fenced = _fenced_json_object(raw)
    if fenced is not None:
        return fenced
    if raw.startswith("{"):
        return raw
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        return raw[start : end + 1]
    return raw


def parse_json_from_text(text: str, default: Any = None) -> Any:
    """
    Parse JSON object/array from plain or fenced text.

    Returns `default` if parsing fails.
    """
    candidate = extract_json_candidate(text)
    if not candidate:
        return default
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return default


def _unwrap_embedded_json_strings(value: Any) -> Any:
    """Рекурсивно распарсить строковые поля, содержащие вложенный JSON."""
    if isinstance(value, dict):
        return {k: _unwrap_embedded_json_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap_embedded_json_strings(item) for item in value]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return value
            return _unwrap_embedded_json_strings(parsed)
    return value


def format_json_for_display(value: Any) -> str:
    """
    Человекочитаемый JSON для CLI и логов.

    - ``None`` → пустая строка;
    - JSON-строка → pretty-print с ``indent=2``;
    - dict/list → pretty-print; вложенные JSON-строки (напр. ``handoff``) разворачиваются.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        parsed = parse_json_from_text(value, default=None)
        if parsed is None:
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return value
        value = parsed
    if isinstance(value, (dict, list)):
        value = _unwrap_embedded_json_strings(value)
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)
