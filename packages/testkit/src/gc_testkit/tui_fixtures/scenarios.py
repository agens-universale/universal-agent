"""Готовые сценарии событий для StubExecutor."""

from __future__ import annotations

import json


def scenario_full_dialog(user_messages: list) -> list[dict]:
    """User-msg → tool-call OK → tool-call OK → tool-call ERROR → submit_solution с comment."""
    records: list[dict] = []
    seq = 1
    for i, m in enumerate(user_messages, start=1):
        records.append(
            {
                "type": "message",
                "seq": seq,
                "ts_utc": "2026-05-21T12:00:00+00:00",
                "payload": {"id": f"u{i}", "role": "user", "content": m},
            }
        )
        seq += 1

    # Agent reasoning — рендерится trace_mapper'ом как kind=system/title=think (muted italic)
    records.append(
        {
            "type": "message",
            "seq": seq,
            "ts_utc": "2026-05-21T12:00:01+00:00",
            "payload": {
                "id": "m1",
                "type": "reasoning",
                "summary": "анализирую",
            },
        }
    )
    seq += 1

    # Tool call OK
    records.append(
        {
            "type": "message",
            "seq": seq,
            "ts_utc": "2026-05-21T12:00:02+00:00",
            "payload": {
                "id": "fc1",
                "type": "function_call",
                "call_id": "c1",
                "name": "read_file",
                "arguments": json.dumps({"path": "src/example.py"}),
            },
        }
    )
    seq += 1
    records.append(
        {
            "type": "message",
            "seq": seq,
            "ts_utc": "2026-05-21T12:00:03+00:00",
            "payload": {
                "id": "fco1",
                "type": "function_call_output",
                "call_id": "c1",
                "output": json.dumps(
                    {"tool": "read_file", "ok": True, "lines": 312}
                ),
            },
        }
    )
    seq += 1

    # Tool call ERROR
    records.append(
        {
            "type": "message",
            "seq": seq,
            "ts_utc": "2026-05-21T12:00:04+00:00",
            "payload": {
                "id": "fc2",
                "type": "function_call",
                "call_id": "c2",
                "name": "read_file",
                "arguments": json.dumps({"path": "src/missing.py"}),
            },
        }
    )
    seq += 1
    records.append(
        {
            "type": "message",
            "seq": seq,
            "ts_utc": "2026-05-21T12:00:05+00:00",
            "payload": {
                "id": "fco2",
                "type": "function_call_output",
                "call_id": "c2",
                "output": json.dumps(
                    {
                        "tool": "read_file",
                        "ok": False,
                        "error": "ENOENT: нет такого файла",
                    }
                ),
            },
        }
    )
    seq += 1

    # submit_solution с task_report и comment
    records.append(
        {
            "type": "message",
            "seq": seq,
            "ts_utc": "2026-05-21T12:00:06+00:00",
            "payload": {
                "id": "fc3",
                "type": "function_call",
                "call_id": "c3",
                "name": "submit_solution",
                "arguments": json.dumps(
                    {
                        "solution_type": "answer_question",
                        "task_report": "Проанализирован пример. Это демонстрационный модуль.",
                        "comment": "Файл src/missing.py отсутствует — проверить пути в config.",
                    }
                ),
            },
        }
    )
    seq += 1
    records.append(
        {
            "type": "message",
            "seq": seq,
            "ts_utc": "2026-05-21T12:00:07+00:00",
            "payload": {
                "id": "fco3",
                "type": "function_call_output",
                "call_id": "c3",
                "output": json.dumps(
                    {
                        "tool": "submit_solution",
                        "ok": True,
                        "status": "submitted",
                    }
                ),
            },
        }
    )

    return records


def scenario_ask_human_flow(user_messages: list) -> list[dict]:
    """User-msg → tool-call (skills_load OK) → function_call ask_human (без output — pending)."""
    records: list[dict] = []
    seq = 1
    for i, m in enumerate(user_messages, start=1):
        records.append(
            {
                "type": "message",
                "seq": seq,
                "ts_utc": "2026-05-21T12:00:00+00:00",
                "payload": {"id": f"u{i}", "role": "user", "content": m},
            }
        )
        seq += 1

    records.append(
        {
            "type": "message",
            "seq": seq,
            "ts_utc": "2026-05-21T12:00:01+00:00",
            "payload": {
                "id": "fc1",
                "type": "function_call",
                "call_id": "c1",
                "name": "skills_load",
                "arguments": json.dumps({"skill_id": "skill-creator"}),
            },
        }
    )
    seq += 1
    records.append(
        {
            "type": "message",
            "seq": seq,
            "ts_utc": "2026-05-21T12:00:02+00:00",
            "payload": {
                "id": "fco1",
                "type": "function_call_output",
                "call_id": "c1",
                "output": json.dumps({"tool": "skills_load", "ok": True}),
            },
        }
    )
    seq += 1

    # ask_human БЕЗ output — это «вопрос pending»
    records.append(
        {
            "type": "message",
            "seq": seq,
            "ts_utc": "2026-05-21T12:00:03+00:00",
            "payload": {
                "id": "fc2",
                "type": "function_call",
                "call_id": "c2",
                "name": "ask_human",
                "arguments": json.dumps(
                    {
                        "question": (
                            "Какого типа отчёт нужен?\n"
                            "A) Еженедельный статус по проекту\n"
                            "B) Технический аудит компонента\n"
                            "C) Что-то другое (опишите)"
                        ),
                    }
                ),
            },
        }
    )

    return records


SCENARIOS = {
    "full_dialog": scenario_full_dialog,
    "ask_human_flow": scenario_ask_human_flow,
}
