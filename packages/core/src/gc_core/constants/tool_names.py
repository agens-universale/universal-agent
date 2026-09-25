"""Canonical agent-visible builtin tool names (TOOL_NAMESPACES_SPEC v1.1)."""

# Turn
FINISH_TOOL_NAME = "turn_finish"
ASK_HUMAN_TOOL_NAME = "turn_ask_human"
INTERMEDIATE_REPLY_TOOL_NAME = "turn_intermediate_reply"

# Тулы, завершающие ход агента (ход не продолжается после них). intermediate_reply
# сюда НЕ входит: он «Finalizes turn: no» — агент продолжает работу в том же ходу.
TURN_FINALIZING_TOOLS = frozenset({FINISH_TOOL_NAME, ASK_HUMAN_TOOL_NAME})

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

# Exec (representative)  # noqa: ERA001  # описание формата, не выключенный код
EXEC_COMMAND_NAME = "exec_command"
EXEC_PYTHON_NAME = "exec_python"
EXEC_FETCH_URL_NAME = "exec_fetch_url"
DOWNLOAD_FILE_NAME = "download_file"
