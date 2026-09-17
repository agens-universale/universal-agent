"""Ctrl+C во время ожидания человека — отказ отвечать, а не падение прогона.

Тело гарда исполняется в рабочем потоке, а сигналы CPython доставляет только в
главный. Поэтому `except KeyboardInterrupt` внутри `prompt_user_confirmation_choice`
— мёртвая ветка: поток её не увидит никогда. Пока её не было видно, Ctrl+C на
вопросе `y/n` попадал в обработчик asyncio, тот снимал главную задачу, и прогон
рушился целиком. На develop это работало правильно, потому что подтверждение
шло инлайн на главном потоке.

Перехват сделан УЗКИМ намеренно: он включается только там, где вызывающий сам
сказал `on_interrupt=...`, то есть на шести местах ожидания человека. Ожидание
LLM-судьи (`command_moderation`) флага не несёт — там Ctrl+C обязан по-прежнему
прерывать прогон, потому что означает «останови всё», а не «я не разрешаю».
"""
import asyncio
import os
import signal
import time

import pytest

from gc_core.security.dual_wrapper import OffLoop, dual_wrap

pytestmark = pytest.mark.skipif(
    not hasattr(signal, "SIGINT"), reason="платформа без SIGINT"
)


def _blocking(seconds: float) -> str:
    """Человек, который думает дольше, чем длится тест."""
    time.sleep(seconds)
    return "человек ответил"


async def _fire_sigint(after: float = 0.2) -> None:
    await asyncio.sleep(after)
    os.kill(os.getpid(), signal.SIGINT)


def _wrapped(on_interrupt, seen: list):
    """Тул с телом-гардом, которое ждёт человека и записывает, что получило."""

    async def tool() -> str:
        return "тул отработал"

    def flow():
        request = (
            OffLoop(_blocking, 5.0)
            if on_interrupt is _ABSENT
            else OffLoop(_blocking, 5.0, on_interrupt=on_interrupt)
        )
        seen.append((yield request))
        return (yield)

    return dual_wrap(tool, flow)


_ABSENT = object()


async def test_ctrl_c_during_a_human_wait_returns_the_declared_value():
    """Тело получает объявленное значение, а прогон продолжается."""
    seen: list = []
    call = _wrapped(False, seen)

    fire = asyncio.ensure_future(_fire_sigint())
    result = await asyncio.wait_for(call(), 5)
    await fire

    assert seen == [False], f"телу гарда пришло не то: {seen}"
    assert result == "тул отработал", "прогон не продолжился после Ctrl+C"


async def test_the_interceptor_restores_the_previous_handler():
    """Перехват живёт только на время ожидания: дальше Ctrl+C снова общий."""
    before = signal.getsignal(signal.SIGINT)
    seen: list = []
    call = _wrapped(False, seen)

    fire = asyncio.ensure_future(_fire_sigint())
    await asyncio.wait_for(call(), 5)
    await fire

    assert signal.getsignal(signal.SIGINT) is before, (
        "обработчик не восстановлен — Ctrl+C вне вопроса перестанет работать"
    )


async def test_without_the_flag_the_wait_is_not_intercepted():
    """Без признака ожидание не перехватывается — это путь LLM-судьи.

    Проверяем не отмену (в pytest сигнал уходит чужому обработчику и тест бы
    рухнул не там), а факт: тело дождалось НАСТОЯЩЕГО результата, то есть
    перехват не сработал там, где его не просили.
    """
    seen: list = []
    call = _wrapped(_ABSENT, seen)

    caught: list = []
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: caught.append(True))
    try:
        fire = asyncio.ensure_future(_fire_sigint(0.1))
        result = await asyncio.wait_for(call(), 10)
        await fire
    finally:
        signal.signal(signal.SIGINT, previous)

    assert caught, "сигнал не дошёл — тест ничего не проверил"
    assert seen == ["человек ответил"], (
        f"ожидание перехватили там, где не просили: {seen}"
    )
    assert result == "тул отработал"
