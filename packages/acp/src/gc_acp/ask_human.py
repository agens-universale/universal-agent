"""Порт вопроса человеку для ACP-сессии.

`session/request_permission` в этом релизе не вызывается. Пустая строка —
тот же исход, что человек ничего не ответил: так уже умеет `turn_ask_human`.
"""

from __future__ import annotations

from typing import Any


async def empty_ask_human(_question: str) -> str:
    return ""


def bind_ask_human(agent: Any) -> None:
    """Поставить порт на контекст сессии.

    `AsyncAgent.create` не принимает `ask_human`: контекст собран внутри,
    наружу торчит исполнитель. Без порта тул читает stdin процесса и
    съедает следующий JSON-RPC.
    """
    agent.executor._ctx.ask_human = empty_ask_human
