"""Краткая причина ошибки в событиях прогресса субагента.

Вложенная строка субагента показывает то же, что основная лента: причину
отказа для человека, а не инструкцию модели.
"""

import json

from gc_core.execution.progress import parse_tool_result_status


def test_error_brief_prefers_human_reason():
    out = json.dumps(
        {
            "ok": False,
            "error": {
                "type": "exec_denied_by_mode",
                "reason": "Command execution not permitted in the current permission mode.",
                "guidance": "Do NOT retry the same call.",
            },
        }
    )
    assert parse_tool_result_status(out) == (
        False,
        "Command execution not permitted in the current permission mode.",
    )


def test_error_brief_falls_back_to_message_then_code():
    assert parse_tool_result_status(
        json.dumps(
            {"ok": False, "error": {"message": "File not found: a.txt"}}
        )
    ) == (False, "File not found: a.txt")
    assert parse_tool_result_status(
        json.dumps({"ok": False, "error": {"code": "E_BOOM"}})
    ) == (False, "E_BOOM")
    assert parse_tool_result_status(json.dumps({"ok": True})) == (True, None)
