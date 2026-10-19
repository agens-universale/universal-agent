"""Порт вопроса пиру. Подтверждения безопасности спросить некого: это
объявляет канал (``human_channel=False`` в ``gc_a2a.main``), и ядро отказывает
само, не читая терминал сервера."""

from __future__ import annotations

from typing import Any


def bind_ask_human(agent: Any, port: Any) -> None:
    """Поставить порт на контекст хода.

    ``AsyncAgent.create`` не принимает ``ask_human``. Без порта тул читает
    stdin процесса.
    """
    agent.executor._ctx.ask_human = port
