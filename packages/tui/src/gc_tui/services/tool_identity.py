"""Канонический id тула для рендера TUI.

Master сменил имена тулов на проводе (namespace + rename, TOOL_NAMESPACES_SPEC).
Здесь — единственный владелец знания «как зовётся тул»: любое wire-имя (новое
namespaced ИЛИ старое голое, для чтения старых тредов) → стабильный канонический
id, равный старому голому имени, на которое завязан trace_mapper. Тонкий слой:
только имя→id. Без Textual, без бизнес-логики.

Маппим ТОЛЬКО тулы, на имена которых завязан trace_mapper и экран: 3 control-
тула (детект баннера/финала/мьюта) и исторический набор переименованных. Подпись
строки и сводку аргументов даёт ядро (`tool_display`, `tool_args_summary`) по
wire-имени — канонический id для показа не нужен."""

from __future__ import annotations

# Новые имена из core-контракта (единый источник истины; правильное направление
# зависимости TUI→ядро). Если master снова переименует — правка здесь.
from gc_core.constants.tool_names import (
    ASK_HUMAN_TOOL_NAME,
    EXEC_COMMAND_NAME,
    EXEC_FETCH_URL_NAME,
    EXEC_PYTHON_NAME,
    FINISH_TOOL_NAME,
    FS_READ_FILE_NAME,
    FS_WRITE_FILE_NAME,
    INTERMEDIATE_REPLY_TOOL_NAME,
)

# wire-имя (новое) → канонический id (старое голое имя, на которое завязан trace_mapper).
_NEW_TO_CANONICAL: dict[str, str] = {
    ASK_HUMAN_TOOL_NAME: "ask_human",
    FINISH_TOOL_NAME: "submit_solution",
    INTERMEDIATE_REPLY_TOOL_NAME: "intermediate_reply",
    FS_READ_FILE_NAME: "read_file",
    FS_WRITE_FILE_NAME: "write_to_file",
    EXEC_COMMAND_NAME: "execute_command",
    EXEC_PYTHON_NAME: "run_python_code",
    EXEC_FETCH_URL_NAME: "fetch_url",
    # Тулы без core-константы (имена сняты в Task 3 на merged-дереве):
    "fs_append_file": "append_to_file",
    "fs_list_files": "list_files",
    "fs_find_files_by_name": "find_files_by_name",
    # Канонический id поиска — тот же, что у старого имени ниже: старые и
    # новые треды маппятся одинаково (подпись и сводку даёт ядро).
    "fs_find_in_files": "search_files",
    # Старое wire-имя: нужно для чтения тредов, записанных до переименования.
    "fs_search_files": "search_files",
    "fs_search_and_replace": "search_and_replace",
}


def tool_id(wire_name: str | None) -> str | None:
    """Канонический id по wire-имени. None/'' и старые/неизвестные имена
    возвращаются как есть (старые треды и будущие тулы рендерятся штатно)."""
    if not wire_name:
        return wire_name
    return _NEW_TO_CANONICAL.get(wire_name, wire_name)
