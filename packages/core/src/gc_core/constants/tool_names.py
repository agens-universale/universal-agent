"""Canonical agent-visible builtin tool names (TOOL_NAMESPACES_SPEC v1.1)."""

# Turn
FINISH_TOOL_NAME = "turn_finish"
ASK_HUMAN_TOOL_NAME = "turn_ask_human"
INTERMEDIATE_REPLY_TOOL_NAME = "turn_intermediate_reply"
PROPOSE_PLAN_TOOL_NAME = "turn_propose_plan"

# Тулы, завершающие ход агента (ход не продолжается после них). intermediate_reply
# сюда НЕ входит: он «Finalizes turn: no» — агент продолжает работу в том же ходу.
TURN_FINALIZING_TOOLS = frozenset(
    {FINISH_TOOL_NAME, ASK_HUMAN_TOOL_NAME, PROPOSE_PLAN_TOOL_NAME}
)

# Session
SESSION_MEMORY_TOOL_NAME = "session_memory"
SESSION_THINK_TOOL_NAME = "session_think"
READ_STORED_TOOL_OUTPUT_NAME = "session_read_stored_tool_output"
READ_USER_INTENT_LOG_NAME = "session_read_user_intent_log"
SESSION_COMPACT_TOOL_NAME = "session_compact"
SESSION_TODO_TOOL_NAME = "session_todo"

# Project
PROJECT_MEMORY_TOOL_NAME = "project_memory"

# FS (representative; prefer cls.tool_description()["name"] in tool implementations)
FS_READ_FILE_NAME = "fs_read_file"
FS_WRITE_FILE_NAME = "fs_write_file"
FS_APPEND_FILE_NAME = "fs_append_file"
# Просмотр изображения (спека §9.2). Имя нужно раньше самого тула: набор тулов
# запроса фильтруется по «кто смотрит» (message_policy.attachments_outbound).
FS_VIEW_IMAGE_NAME = "fs_view_image"

# Exec (representative)  # noqa: ERA001  # описание формата, не выключенный код
EXEC_COMMAND_NAME = "exec_command"
EXEC_PYTHON_NAME = "exec_python"
EXEC_FETCH_URL_NAME = "exec_fetch_url"
DOWNLOAD_FILE_NAME = "download_file"

# FS (продолжение representative-набора)
FS_APPLY_FILE_EDITS_NAME = "fs_apply_file_edits"
FS_SEARCH_AND_REPLACE_NAME = "fs_search_and_replace"
FS_SAVE_BASE64_FILE_NAME = "fs_save_base64_file"

# Skills
SKILLS_LIST_NAME = "skills_list"
SKILLS_LOAD_NAME = "skills_load"
SKILLS_RUN_SCRIPT_NAME = "skills_run_script"
SKILLS_READ_REFERENCE_NAME = "skills_read_reference"
SKILLS_READ_ASSET_NAME = "skills_read_asset"

# Остальные встроенные тулы fs / exec (имена для таблицы отображения)
FS_FILE_INFO_NAME = "fs_file_info"
FS_LIST_FILES_NAME = "fs_list_files"
FS_FIND_FILES_BY_NAME_NAME = "fs_find_files_by_name"
FS_FIND_IN_FILES_NAME = "fs_find_in_files"
EXEC_CALCULATOR_NAME = "exec_calculator"
EXEC_SLEEP_NAME = "exec_sleep"

# Тулы записи файлов — создают или меняют файлы проекта.
FILE_WRITE_TOOL_NAMES = frozenset(
    {
        FS_WRITE_FILE_NAME,
        FS_APPEND_FILE_NAME,
        FS_APPLY_FILE_EDITS_NAME,
        FS_SEARCH_AND_REPLACE_NAME,
        FS_SAVE_BASE64_FILE_NAME,
        DOWNLOAD_FILE_NAME,
    }
)

# Тулы запуска — исполняют команды и код: два встроенных и тул скиллов.
EXEC_TOOL_NAMES = frozenset(
    {
        EXEC_COMMAND_NAME,
        EXEC_PYTHON_NAME,
        SKILLS_RUN_SCRIPT_NAME,
    }
)

# Тулы записи файлов и исполнения кода; MCP по имени не классифицируются.
ARTIFACT_TOOL_NAMES = FILE_WRITE_TOOL_NAMES | EXEC_TOOL_NAMES

# «Важные» тулы — меняют мир или закрывают ход: запись файлов, память агента,
# сжатие контекста, финал, вопрос человеку и предложение плана. Запуск команд сюда не входит
# (`EXEC_TOOL_NAMES` — отдельный набор); чтение, поиск, `session_think`,
# `exec_fetch_url` и `turn_intermediate_reply` — тоже. План (`session_todo`) не
# входит: слабая почти всегда лишь отмечает пункт, а его переигрыш давал половину
# переигрышей замера 2026-09-24. Тулы субагентов по имени не перечислить
# (кастомных заводит пользователь) — их распознаёт
# `gc_core.tools.subagents.registry.is_subagent_tool`.
IMPORTANT_TOOL_NAMES = FILE_WRITE_TOOL_NAMES | frozenset(
    {
        SESSION_MEMORY_TOOL_NAME,
        PROJECT_MEMORY_TOOL_NAME,
        SESSION_COMPACT_TOOL_NAME,
        FINISH_TOOL_NAME,
        ASK_HUMAN_TOOL_NAME,
        PROPOSE_PLAN_TOOL_NAME,
    }
)
