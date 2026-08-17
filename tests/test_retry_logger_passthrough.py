"""retry_with_backoff: logger должен пробрасываться внутрь декорированной функции,
если её сигнатура его принимает. Без этого функции, которые делают подробное
логирование тел ошибок (например body 400-ответа в connect_backend), молча
получают logger=None и ничего не пишут — пропадает наблюдаемость."""
from __future__ import annotations

import logging

from gc_core.utils.retry import retry_with_backoff


def test_logger_kwarg_forwarded_when_function_accepts_it():
    """func(logger=...) должен реально получить переданный logger из kwargs."""
    received = {}

    @retry_with_backoff(operation_name="test")
    def func(*, payload, logger=None):
        received["logger"] = logger
        return "ok"

    my_logger = logging.getLogger("my-test-logger")
    func(payload={"a": 1}, logger=my_logger, use_retry=False)
    assert received["logger"] is my_logger


def test_logger_not_in_kwargs_falls_back_to_module_logger():
    """func без переданного logger получает дефолт от декоратора, не None."""
    received = {}

    @retry_with_backoff(operation_name="test")
    def func(*, payload, logger=None):
        received["logger"] = logger
        return "ok"

    func(payload={"a": 1}, use_retry=False)
    # logger не должен остаться None — декоратор вкладывает module logger.
    assert isinstance(received["logger"], logging.Logger)
    assert received["logger"].name  # имя выставлено


def test_function_without_logger_param_does_not_get_logger():
    """Если у функции нет параметра logger — декоратор его не подсовывает (без TypeError)."""
    received = {}

    @retry_with_backoff(operation_name="test")
    def func(*, payload):
        received["payload"] = payload
        return "ok"

    # logger=... передан, но func не принимает — должно сработать без TypeError.
    my_logger = logging.getLogger("my-test-logger")
    result = func(payload={"a": 1}, logger=my_logger, use_retry=False)
    assert result == "ok"
    assert received["payload"] == {"a": 1}


def test_logger_forwarded_during_retry_loop_too():
    """logger пробрасывается и при use_retry=True (каждая попытка получает logger)."""
    received_loggers = []
    call_count = {"n": 0}

    @retry_with_backoff(max_attempts=3, initial_delay_sec=0.0, operation_name="test")
    def func(*, payload, logger=None):
        received_loggers.append(logger)
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise RuntimeError("transient")
        return "ok"

    my_logger = logging.getLogger("my-test-logger")
    result = func(payload={"a": 1}, logger=my_logger, use_retry=True)
    assert result == "ok"
    assert len(received_loggers) == 3
    assert all(lg is my_logger for lg in received_loggers)
