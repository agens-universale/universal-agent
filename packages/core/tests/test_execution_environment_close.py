"""Источники ID и времени: закрытие в чужом контексте — не ошибка.

Temporal выгружает run из кэша и закрывает его корутину (GeneratorExit) уже
в другом контексте; сброс contextvar там падал ValueError, и pytest ловил
его как необработанное исключение при сборке мусора."""

import contextvars
from datetime import UTC, datetime

from gc_core.execution.environment import execution_environment, utc_now


def test_closing_in_another_context_does_not_raise():
    fixed = datetime(2026, 1, 1, tzinfo=UTC)
    manager = execution_environment(
        new_id=lambda p: f"{p}-1", now=lambda: fixed
    )
    inner = contextvars.copy_context()
    inner.run(manager.__enter__)
    assert inner.run(utc_now) == fixed
    # Закрытие снаружи, как при выгрузке run.
    manager.__exit__(GeneratorExit, GeneratorExit(), None)
    assert utc_now() != fixed


def test_normal_use_restores_sources():
    fixed = datetime(2026, 1, 1, tzinfo=UTC)
    with execution_environment(new_id=lambda p: p, now=lambda: fixed):
        assert utc_now() == fixed
    assert utc_now() != fixed
