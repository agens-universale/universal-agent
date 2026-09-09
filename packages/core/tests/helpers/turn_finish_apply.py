"""Стаб диспетчера тулов для тестов, где цикл исполняет turn_finish без tools_data.

Приехал с develop под именем ``apply_tool``. У ядра форма диспетчера ОДНА и
она корутинная (``aapply_tool``), поэтому стаб тоже корутинный: синхронную
заглушку цикл получил бы как результат вместо awaitable и упал бы на ``await``.
"""

from __future__ import annotations

import json


async def stub_turn_finish_apply(**kwargs):
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
