"""Текст ответа задачи A2A: правило конца хода ядра (``turn_close``) —
ответ модели и итог ``turn_finish`` простым текстом, план — с ответом.

Перенесено из ``packages/a2a/tests/test_turns.py`` develop (пакет
``gc_a2a`` в этой ветке заменён поверхностью ``gc_server.a2a``)."""

import json
from types import SimpleNamespace

from gc_server.a2a.executor import answer_text


def _agent(reply: str, last: dict) -> SimpleNamespace:
    return SimpleNamespace(
        executor=SimpleNamespace(last_closing_reply=reply),
        last_answer=json.dumps(last),
    )


def test_answer_text_reply_and_summary():
    """turn_finish: ответ модели и итог в одном рендере."""
    agent = _agent(
        "Результат готов.",
        {
            "solution_type": "full_solution",
            "task": "Подсчитать файлы",
            "task_report": "Найдено 42 файла",
            "handoff": "{}",
        },
    )
    assert answer_text(agent) == (
        "Результат готов.\n\n* итог:\n\n  Найдено 42 файла"
    )


def test_answer_text_plan_after_reply():
    """turn_propose_plan: план показывается вместе с ответом модели."""
    agent = _agent(
        "Вот план:", {"plan": "1. шаг\n2. проверка", "status": "proposed"}
    )
    assert answer_text(agent) == "Вот план:\n\n1. шаг\n2. проверка"


def test_answer_text_report_only():
    """turn_finish без ответа модели: только отчёт без ярлыка."""
    agent = _agent(
        "",
        {
            "solution_type": "full_solution",
            "task": "Задача",
            "task_report": "Выполнено",
            "handoff": "{}",
        },
    )
    assert answer_text(agent) == "Выполнено"
