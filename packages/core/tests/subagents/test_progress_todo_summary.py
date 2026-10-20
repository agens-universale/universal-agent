"""Строка чек-листа в блоке субагента: `todos=<list 6>` → `чек-лист 1/3 · …`.

Одностроч. сводка живёт в tool_args_summary — она общая для всех трёх путей
(живой прогресс, переоткрытие из sidecar, экран деталей).
"""

from __future__ import annotations

import json

from gc_core.constants.tool_names import SESSION_TODO_TOOL_NAME
from gc_core.execution.progress import (
    todo_items_from_arguments,
    tool_args_summary,
)

ITEMS = [
    {"content": "Прочитать файлы", "status": "completed"},
    {"content": "Написать ревью", "status": "in_progress"},
    {"content": "Свести замечания", "status": "pending"},
]


def test_todo_summary_shows_progress_and_current_step():
    out = tool_args_summary(SESSION_TODO_TOOL_NAME, {"todos": ITEMS})
    assert out == "чек-лист 1/3 · Написать ревью"


def test_todo_summary_without_current_step():
    items = [{"content": "Готово", "status": "completed"}]
    out = tool_args_summary(SESSION_TODO_TOOL_NAME, {"todos": items})
    assert out == "чек-лист 1/1"


def test_todo_summary_empty_list_means_cleared():
    assert (
        tool_args_summary(SESSION_TODO_TOOL_NAME, {"todos": []})
        == "чек-лист очищен"
    )


def test_todo_summary_accepts_json_string_arguments():
    out = tool_args_summary(
        SESSION_TODO_TOOL_NAME, json.dumps({"todos": ITEMS})
    )
    assert out == "чек-лист 1/3 · Написать ревью"


def test_other_tools_keep_their_own_summary():
    out = tool_args_summary("fs_read_file", {"path": "a.py"})
    assert out == "a.py"


def test_todo_arguments_without_tool_name_stay_generic():
    out = tool_args_summary(None, {"todos": ITEMS})
    assert out == "todos=<list 3>"


def test_parse_items_from_json_string():
    args = json.dumps({"todos": ITEMS})
    assert todo_items_from_arguments(args) == ITEMS


def test_parse_rejects_whole_list_on_any_invalid_item():
    """Зеркало валидации тула: один кривой пункт — весь вызов не чек-лист.

    Тул (tools/session/todo.py) отклоняет вызов целиком и состояние не меняет —
    значит рендер обязан классифицировать так же, иначе нарисует «чек-лист очищен»
    при целом списке."""
    good = {"content": "Шаг", "status": "pending"}
    assert todo_items_from_arguments({"todos": [good, "мусор"]}) is None
    assert (
        todo_items_from_arguments(
            {"todos": [good, {"content": "", "status": "pending"}]}
        )
        is None
    )
    assert (
        todo_items_from_arguments(
            {"todos": [good, {"content": "x", "status": "done"}]}
        )
        is None
    )
    assert (
        todo_items_from_arguments(
            {"todos": [{"content": "x", "status": "pending", "id": 1}]}
        )
        is None
    )


def test_parse_accepts_fully_valid_list():
    assert todo_items_from_arguments({"todos": list(ITEMS)}) == ITEMS


# --- Микро-фикс честности: битые аргументы -> НЕ чек-лист -------------------


def test_parse_garbage_returns_none_not_empty():
    """None = «не удалось прочитать как чек-лист»; [] зарезервирован за
    валидной очисткой (todos: []). Различие маршрутизирует рендер."""
    assert todo_items_from_arguments("не json") is None
    assert todo_items_from_arguments(None) is None
    assert todo_items_from_arguments({"other": 1}) is None
    assert todo_items_from_arguments({"todos": "не список"}) is None


def test_parse_valid_empty_returns_empty_list():
    assert todo_items_from_arguments({"todos": []}) == []
    assert todo_items_from_arguments('{"todos": []}') == []


def test_tool_args_summary_garbage_args_fall_back_to_generic():
    """Битые аргументы session_todo не должны рендериться «чек-лист очищен»."""
    out = tool_args_summary(SESSION_TODO_TOOL_NAME, {"other": 1})
    assert "очищен" not in out
    assert out == "other=1"


def test_tool_args_summary_valid_empty_stays_cleared():
    out = tool_args_summary(SESSION_TODO_TOOL_NAME, {"todos": []})
    assert out == "чек-лист очищен"


def test_tool_args_summary_partially_broken_list_falls_back_to_generic():
    """Частично битый список — не «очищен», а обычное превью аргументов."""
    raw = {"todos": [{"content": "Шаг", "status": "нетакой"}]}
    out = tool_args_summary(SESSION_TODO_TOOL_NAME, raw)
    assert "очищен" not in out
    assert out == "todos=<list 1>"


def test_tool_args_summary_truncates_long_current_item():
    """Однострочная сводка обязана соблюдать общий лимит, как все прочие тулы."""
    long_item = {
        "content": "Пройти по всем файлам сервиса и " + "деталь " * 40,
        "status": "in_progress",
    }
    out = tool_args_summary(SESSION_TODO_TOOL_NAME, {"todos": [long_item]})
    assert len(out) <= 80, f"сводка длиной {len(out)}: {out[:120]}"
    assert out.startswith("чек-лист 0/1 · ")
    assert out.endswith("…")


def test_tool_args_summary_read_file_shows_path():
    assert (
        tool_args_summary(
            "fs_read_file", json.dumps({"path": "a.py", "offset": 9})
        )
        == "a.py"
    )
