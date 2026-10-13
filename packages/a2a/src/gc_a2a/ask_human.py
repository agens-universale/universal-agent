"""Порт вопроса человеку: тул зовёт его и ждёт ответ пира."""

from __future__ import annotations

from typing import Any


def bind_ask_human(agent: Any, port: Any) -> None:
    """Поставить порт на контекст хода.

    ``AsyncAgent.create`` не принимает ``ask_human``. Без порта тул читает
    stdin процесса.
    """
    agent.executor._ctx.ask_human = port
