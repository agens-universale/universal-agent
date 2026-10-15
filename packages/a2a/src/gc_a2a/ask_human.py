"""Порты человека: вопрос пиру и отказ от подтверждения в терминале."""

from __future__ import annotations

from typing import Any

from gc_core.security.confirm_port import CONFIRM_DENY_ONCE


def bind_ask_human(agent: Any, port: Any) -> None:
    """Поставить порт на контекст хода.

    ``AsyncAgent.create`` не принимает ``ask_human``. Без порта тул читает
    stdin процесса.
    """
    agent.executor._ctx.ask_human = port


def bind_confirm_deny(agent: Any) -> None:
    """Подтверждение безопасности не читает stdin сервера.

    Без порта и при tty ``confirm_available`` уводит вопрос в ``input()``:
    задача висит в ``WORKING``, пир её не видит. Явный отказ — ход
    продолжается, оператор сервера за пира не отвечает.
    """
    agent.executor._ctx.confirm_request = _deny_confirm


def _deny_confirm(_request: Any) -> str:
    return CONFIRM_DENY_ONCE
