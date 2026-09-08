"""Стаб apply_tool для тестов, где run_agent исполняет turn_finish без tools_data."""

from __future__ import annotations

import json


def stub_turn_finish_apply(**kwargs):
    tool_name = kwargs.get("tool_name")
    arguments = kwargs.get("arguments")
    if tool_name != "turn_finish":
        return json.dumps({"tool": tool_name, "ok": True}, ensure_ascii=False)
    try:
        payload = json.loads(arguments) if isinstance(arguments, str) else {}
    except Exception:
        payload = {}
    result = {
        "tool": "turn_finish",
        "ok": True,
        "status": "submitted",
        "message": "Task outcome submitted.",
        "error": None,
    }
    if isinstance(payload, dict):
        for key in ("solution_type", "comment", "task_report", "handoff"):
            if key in payload:
                result[key] = payload[key]
    return json.dumps(result, ensure_ascii=False)
