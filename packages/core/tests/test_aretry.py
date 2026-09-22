"""Асинхронные ретраи (план async-переезда, Задача 7).

Главное утверждение — паритет с синхронным двойником: политика ретраев не
должна зависеть от того, каким транспортом ушёл запрос.
"""
import asyncio
import logging

import pytest

from gc_core.utils.aretry import retry_with_backoff_async
from gc_core.utils.retry import retry_with_backoff


async def test_retries_until_success_without_real_sleep(monkeypatch):
    slept = []

    async def fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    @retry_with_backoff_async(max_attempts=3, initial_delay_sec=1.0, backoff_factor=2.0)
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("сеть")
        return "ok"

    assert await flaky(use_retry=True) == "ok"
    assert calls["n"] == 3
    assert slept == [1.0, 2.0]


async def test_sleep_is_asyncio_not_time(monkeypatch):
    """Пауза обязана быть кооперативной: time.sleep заморозил бы весь loop —
    ровно то, ради чего затевался переезд."""
    import time

    monkeypatch.setattr(time, "sleep", lambda *_: pytest.fail("использован time.sleep"))

    async def fake_sleep(_d):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    @retry_with_backoff_async(max_attempts=2, initial_delay_sec=0.0)
    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("сеть")
        return "ok"

    assert await flaky(use_retry=True) == "ok"


async def test_use_retry_false_does_not_retry():
    calls = {"n": 0}

    @retry_with_backoff_async(max_attempts=3)
    async def flaky():
        calls["n"] += 1
        raise ValueError("нет")

    with pytest.raises(ValueError):
        await flaky(use_retry=False)
    assert calls["n"] == 1


async def test_should_retry_gate_respected():
    calls = {"n": 0}

    @retry_with_backoff_async(max_attempts=3, should_retry=lambda e: False)
    async def f():
        calls["n"] += 1
        raise ValueError("не ретраить")

    with pytest.raises(ValueError):
        await f(use_retry=True)
    assert calls["n"] == 1


async def test_unknown_kwargs_are_filtered_like_in_sync_twin(monkeypatch):
    """kwargs фильтруются по сигнатуре — иначе TypeError на чужом ключе.

    Тот же приём, что в синхронном retry: вызывающие транспорта передают общий
    мешок параметров, часть которых конкретной функции не нужна.
    """
    async def fake_sleep(_d):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    @retry_with_backoff_async(max_attempts=2)
    async def f(url: str):
        return url

    assert await f(url="http://x", use_retry=True, лишний="ключ") == "http://x"


async def test_logger_is_reinjected_when_function_accepts_it(monkeypatch, caplog):
    """logger достаётся декоратору, но возвращается функции, если та его принимает.

    Без ре-инъекции вызываемая функция получала бы logger=None и переставала
    писать детали ошибок в файл лога — тот же контракт, что у синхронного.
    """
    seen = {}

    @retry_with_backoff_async(max_attempts=1)
    async def f(url: str, logger=None):
        seen["logger"] = logger
        return "ok"

    my_logger = logging.getLogger("тест-ретраев")
    assert await f(url="http://x", logger=my_logger) == "ok"
    assert seen["logger"] is my_logger


def test_sync_twin_still_behaves_the_same(monkeypatch):
    """Паритет: синхронный двойник с той же настройкой даёт то же число попыток."""
    import time

    monkeypatch.setattr(time, "sleep", lambda *_: None)
    calls = {"n": 0}

    @retry_with_backoff(max_attempts=3, initial_delay_sec=0.0)
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("сеть")
        return "ok"

    assert flaky(use_retry=True) == "ok"
    assert calls["n"] == 3
