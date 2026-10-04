"""Готовые сценарии событий для StubExecutor."""

from __future__ import annotations

import json

#: Время всех записей сценариев — кадрам ленты важен порядок, а не время.
_SCENARIO_TS = "2026-05-21T12:00:00+00:00"


def scenario_full_dialog(user_messages: list) -> list[dict]:
    """User-msg → tool-call OK → tool-call OK → tool-call ERROR → submit_solution с comment."""
    records: list[dict] = []
    seq = 1
    for i, m in enumerate(user_messages, start=1):
        records.append(
            {
                "type": "message",
                "seq": seq,
                "ts_utc": _SCENARIO_TS,
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
                "ts_utc": _SCENARIO_TS,
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


def _image_attachment(
    n: int, *, mime: str, width: int, height: int, source: str
) -> dict:
    """Картинка в форме `Attachment.to_dict()`. Файла нет: кадру ленты нужны
    подпись, формат, размер и источник, а не пиксели."""
    from gc_core.attachments.model import ORIGIN_USER, Attachment, user_label
    from gc_core.attachments.normalize import MIME_PNG

    ext = "png" if mime == MIME_PNG else "jpg"
    return Attachment(
        id=f"{n:016x}",
        kind="image",
        mime=mime,
        width=width,
        height=height,
        size=1024,
        path=f"/work/threads/images/cli_snap_{n:016x}.{ext}",
        origin=ORIGIN_USER,
        label=user_label(n),
        source=source,
        orig_width=width,
        orig_height=height,
    ).to_dict()


def scenario_image_reply(user_messages: list) -> list[dict]:
    """Реплика с двумя картинками (визуальный контракт картинок §3). Ответа модели нет:
    серая строка «изображение не просмотрено» только живая — её кладёт сам тест."""
    from gc_core.attachments.model import chip_text
    from gc_core.attachments.normalize import MIME_JPEG, MIME_PNG

    text = user_messages[0] if user_messages else ""
    return [
        {
            "type": "message",
            "seq": 1,
            "ts_utc": _SCENARIO_TS,
            "payload": {
                "id": "u1",
                "role": "user",
                "content": f"{chip_text(8)} {chip_text(9)} {text}".rstrip(),
                "attachments": [
                    _image_attachment(
                        8,
                        mime=MIME_PNG,
                        width=1456,
                        height=819,
                        source="скрин.png",
                    ),
                    _image_attachment(
                        9,
                        mime=MIME_JPEG,
                        width=1024,
                        height=768,
                        source="clipboard",
                    ),
                ],
            },
        }
    ]


def scenario_ask_human_image_answer(user_messages: list) -> list[dict]:
    """Вопрос агента → ответ с картинкой (дизайн картинок §20.3): текст ответа с
    плашкой — в output тула, сама картинка — в следующем техническом сообщении."""
    from gc_core.attachments.model import Attachment, chip_text, wh_text
    from gc_core.attachments.normalize import MIME_PNG
    from gc_core.attachments.texts import TECH_LINE_ASK
    from gc_core.message_policy.user_message_policy import (
        wrap_technical_user_content,
    )

    att = _image_attachment(
        3, mime=MIME_PNG, width=1280, height=800, source="ошибка.png"
    )
    question = "Покажите, как выглядит ошибка?"
    tech_line = TECH_LINE_ASK.format(
        label=att["label"], wh=wh_text(Attachment.from_dict(att))
    )
    payloads = [
        {
            "id": "u1",
            "role": "user",
            "content": user_messages[0] if user_messages else "почини вход",
        },
        {
            "id": "fc1",
            "type": "function_call",
            "call_id": "c1",
            "name": "turn_ask_human",
            "arguments": json.dumps(
                {"question": question}, ensure_ascii=False
            ),
        },
        {
            "id": "fco1",
            "type": "function_call_output",
            "call_id": "c1",
            "output": json.dumps(
                {
                    "tool": "turn_ask_human",
                    "ok": True,
                    "error": None,
                    "question": question,
                    "response": f"{chip_text(3)} вот так выглядит ошибка",
                },
                ensure_ascii=False,
            ),
        },
        {
            "id": "t1",
            "role": "user",
            "content": wrap_technical_user_content(
                tech_line, technical_type="technical"
            ),
            "attachments": [att],
        },
    ]
    return [
        {"type": "message", "seq": i, "ts_utc": _SCENARIO_TS, "payload": p}
        for i, p in enumerate(payloads, start=1)
    ]


SCENARIOS = {
    "full_dialog": scenario_full_dialog,
    "ask_human_flow": scenario_ask_human_flow,
    "image_reply": scenario_image_reply,
    "ask_human_image_answer": scenario_ask_human_image_answer,
}
