"""Порт вопроса человеку для хода Responses.

Пустая строка — тот же исход, что человек ничего не ответил. Ход на вопросе
не встаёт, события «ждём ответ UI» нет.
"""

from __future__ import annotations

from typing import Any


async def empty_ask_human(_question: str) -> str:
    return ""


def bind_ask_human(agent: Any) -> None:
    """Поставить порт на контекст хода.

    ``AsyncAgent.create`` не принимает ``ask_human``. Без порта тул читает
    stdin процесса.
    """
    agent.executor._ctx.ask_human = empty_ask_human
