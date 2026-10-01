"""Conservative metadata on the existing registry, not another tool registry."""

_STATE_TOOLS = frozenset({"session_memory", "session_todo"})
FILE_EFFECT_TOOLS = frozenset(
    {
        "fs_write_file",
        "fs_append_file",
        "fs_apply_file_edits",
        "fs_search_and_replace",
        "fs_save_base64_file",
        "download_file",
    }
)
COMMAND_TOOLS = frozenset({"exec_command", "exec_python"})
GUARDED_TOOLS = FILE_EFFECT_TOOLS | COMMAND_TOOLS | {"turn_ask_human"}
_READ_TOOLS = frozenset(
    {
        "fs_read_file",
        "fs_file_info",
        "fs_list_files",
        "fs_find_files_by_name",
        "fs_find_in_files",
        "session_read_stored_tool_output",
    }
)
_PURE_TOOLS = frozenset(
    {"exec_calculator", "turn_intermediate_reply", "turn_finish"}
)


def execution_policy(name: str) -> dict[str, str]:
    if name in FILE_EFFECT_TOOLS | COMMAND_TOOLS:
        return {"kind": "guarded_effect", "retry": "reconcile_only"}
    if name == "turn_ask_human":
        return {"kind": "human_wait", "retry": "reply_receipt"}
    if name in _STATE_TOOLS:
        kind = "state"
    elif name in _READ_TOOLS:
        kind = "read"
    elif name in _PURE_TOOLS:
        kind = "pure"
    elif name == "exec_sleep":
        kind = "wait"
    else:
        return {"kind": "legacy", "retry": "never"}
    return {"kind": kind, "retry": "safe_with_operation_receipt"}
