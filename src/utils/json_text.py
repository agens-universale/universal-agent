import json
import re
from typing import Any


def extract_json_candidate(text: str) -> str:
    """
    Return best-effort JSON candidate from arbitrary text.

    Handles common LLM pattern when JSON is wrapped in markdown fences:
    ```json
    { ... }
    ```
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    fenced = re.search(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced:
        return fenced.group(1).strip()
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
