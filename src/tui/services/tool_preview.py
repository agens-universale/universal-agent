"""Парсинг tool-output payload + извлечение preview-строк.

Используется:
- TraceScreen'ом для focus-only preview под ToolCallRow (через extract_preview_lines).
- trace_mapper._summarize_output для inline-row summary (через unwrap_tool_output_dict).

Обрабатывает:
- Голый JSON-dict (read_file / list_files / search_files / execute_command / ...).
- Double-encoded JSON-строку (json.dumps дважды).
- List-обёртку `[{...}]`.
- stored_tool_output envelope (gc-cli оборачивает большие outputs >20000 chars):
  inner JSON-string распаковываем; если truncated — generic partial-JSON parser.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

_MAX_LINES = 3

# Fallback regex: вытаскивает значение поля "content" из обрезанного JSON,
# когда truncate произошёл ВНУТРИ одной длинной content-строки (read_file
# на огромный файл) и `_try_parse_truncated_json` не находит ни одного
# полного `}`/`]`.
_CONTENT_RE = re.compile(r'"content"\s*:\s*"((?:[^"\\]|\\.)*)')


def _unescape_json_string(raw: str) -> str:
    """Минимальный JSON-unescape: \\n / \\t / \\" / \\\\ → реальные символы.
    Используется для regex-извлечённых значений (когда полный json.loads невозможен)."""
    return (
        raw.replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )


def _try_parse_truncated_json(s: str) -> Any:
    """Попытаться распарсить **обрезанный** JSON, закрыв незавершённые скобки.

    Алгоритм: проходим строку, считая `{`/`[` и `}`/`]` (учитывая строки и escapes).
    Запоминаем позицию после последнего полного значения (`}`/`]`). Обрезаем
    строку до неё, закрываем остаточные открытые скобки, парсим.

    Возвращает Python-объект или None если парсинг невозможен.
    """
    in_string = False
    escape = False
    stack: list[str] = []
    last_value_end = 0  # позиция сразу после последнего полного `}`/`]`

    for i, c in enumerate(s):
        if escape:
            escape = False
            continue
        if in_string:
            if c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
            continue
        if c in "{[":
            stack.append(c)
        elif c == "}" and stack and stack[-1] == "{":
            stack.pop()
            last_value_end = i + 1
        elif c == "]" and stack and stack[-1] == "[":
            stack.pop()
            last_value_end = i + 1

    if last_value_end == 0:
        return None  # не нашли ни одного полного значения

    sub = s[:last_value_end]
    # Пересчитать стек на обрезанной строке (мы могли остановиться
    # ПОСЛЕ закрытия некоторых внутренних структур — остальные ещё открыты).
    stack2: list[str] = []
    in_string = False
    escape = False
    for c in sub:
        if escape:
            escape = False
            continue
        if in_string:
            if c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
            continue
        if c in "{[":
            stack2.append(c)
        elif c == "}" and stack2 and stack2[-1] == "{":
            stack2.pop()
        elif c == "]" and stack2 and stack2[-1] == "[":
            stack2.pop()

    closing = "".join("]" if c == "[" else "}" for c in reversed(stack2))
    try:
        return json.loads(sub + closing)
    except (json.JSONDecodeError, ValueError):
        return None


def _from_value(value: Any) -> list[str]:
    """Convert payload value to list of lines.
    - str → split by \\n
    - list[str] → каждый элемент строка
    - list[dict] → каждый элемент → строка вида 'file:line' (для matches) или JSON
    """
    if isinstance(value, str):
        return [ln for ln in value.split("\n") if ln != ""]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                # Эвристика для match-объектов: "file:line"
                if "file" in item and "line" in item:
                    out.append(f"{item['file']}:{item['line']}")
                else:
                    out.append(json.dumps(item, ensure_ascii=False))
            else:
                out.append(str(item))
        return out
    return []


def unwrap_tool_output_dict(raw_output: str) -> Optional[dict]:
    """Распарсить raw tool-output JSON-string и вернуть payload-dict.

    Единый источник истины для всех консьюмеров (preview, row inline summary,
    detail-screen и т.д.). Обрабатывает:
    - Голый JSON-dict.
    - Double-encoded JSON-строку.
    - List-обёртку `[{...}]` → первый dict.
    - stored_tool_output envelope → inner dict (json.loads или partial-parser
      для truncated; ultimate fallback — regex для content-only случая).

    Возвращает dict или None если raw_output не JSON-объект.
    """
    if not raw_output:
        return None
    try:
        parsed = json.loads(raw_output)
    except (json.JSONDecodeError, ValueError):
        return None

    # Двойная JSON-кодировка: некоторые pipeline'ы делают json.dumps дважды.
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (json.JSONDecodeError, ValueError):
            return None

    # List-обёртка: некоторые тулы возвращают [{ok, content, ...}].
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        parsed = parsed[0]

    if not isinstance(parsed, dict):
        return None

    # stored_tool_output envelope → inner result dict.
    if parsed.get("type") == "stored_tool_output":
        inner_str = parsed.get("content")
        if isinstance(inner_str, str):
            try:
                inner = json.loads(inner_str)
                if isinstance(inner, dict):
                    return inner
            except (json.JSONDecodeError, ValueError):
                partial = _try_parse_truncated_json(inner_str)
                if isinstance(partial, dict):
                    return partial
                # Ultimate fallback: truncate внутри длинной content-строки.
                m = _CONTENT_RE.search(inner_str)
                if m:
                    return {"content": _unescape_json_string(m.group(1))}
        # Не смогли развернуть inner — возвращаем envelope как есть
        # (лучше показать что-то про envelope чем ничего).
        return parsed

    return parsed


def extract_preview_lines(raw_output: str) -> tuple[list[str], int]:
    """Вернуть (lines, remaining_count) — первые ≤3 строки реального контента
    output'а + сколько строк/элементов осталось за preview.

    Эвристика по структуре (после unwrap_tool_output_dict):
    - `content` (read_file, fetch_url): first 3 lines of content
    - `entries` (list_files): first 3 entries (JSON dump каждого)
    - `matches` (search_files): first 3 match-описаний ('file:line')
    - `stdout` (execute_command): first 3 lines/items
    - `message` / `error`: одна строка (для tool-ошибок)
    - plain string (output не JSON): first 3 lines
    - dict без контентных ключей: ([], 0) → preview не монтируется
    """
    if not raw_output:
        return [], 0

    parsed = unwrap_tool_output_dict(raw_output)

    if isinstance(parsed, dict):
        # Порядок: основной контент сначала, затем message/error fallback.
        for key in ("content", "entries", "matches", "stdout", "message", "error"):
            if key in parsed:
                all_lines = _from_value(parsed[key])
                if not all_lines:
                    continue
                head = all_lines[:_MAX_LINES]
                remaining = max(0, len(all_lines) - _MAX_LINES)
                return head, remaining
        # Dict без контентных ключей → нечего показывать
        return [], 0

    # plain string fallback (output вообще не JSON)
    all_lines = [ln for ln in raw_output.split("\n") if ln != ""]
    head = all_lines[:_MAX_LINES]
    remaining = max(0, len(all_lines) - _MAX_LINES)
    return head, remaining
