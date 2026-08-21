"""Одно тело гварда на два мира: синхронный тул и асинхронный.

План async-переезда, Задача 4. Политика гвардов (проверки над строками) —
чистый CPU и остаётся синхронной; отличается ТОЛЬКО то, как зовётся сам тул:
обычной функцией или через ``await``.

Наивная форма «проверить → позвать → обработать» здесь не годится: каждый
гвард решает «звать ли дальше» в пяти-шести разных ветках (выключен, не удалось
извлечь аргумент, политика разрешила, режим allow, пользователь подтвердил).
Разложить это на precheck/postprocess нельзя, а писать две копии тела — ровно
то, чего переезд обязан избежать: политика безопасности, разъехавшаяся между
sync- и async-копией, — это дыра, которую никто не заметит.

Поэтому тело гварда — генератор. Он ведёт свою логику как раньше, а вместо
``return func(*args, **kwargs)`` пишет ``return (yield)``: «позови тул и отдай
мне результат». Драйверов два, каждый в шесть строк, и различаются они одним
словом ``await``.

    def enforce_something(...):
        def decorator(func):
            def _flow(*args, **kwargs):
                if not enabled:
                    return (yield)          # позвать тул, вернуть как есть
                if blocked:
                    return "отказ"          # тул не зовём вовсе
                result = yield              # позвать и что-то сделать с ответом
                return postprocess(result)
            return dual_wrap(func, _flow)
        return decorator
"""
from __future__ import annotations

import asyncio
import functools
import inspect
from typing import Any, Callable, Generator


def _start(flow: Generator) -> tuple[bool, Any]:
    """Прокрутить тело до первого ``yield``.

    Возвращает ``(нужен_ли_вызов, результат)``: гвард мог решить всё сам и
    завершиться, ни разу не отдав управление (ранний отказ).
    """
    try:
        next(flow)
    except StopIteration as stop:
        return False, stop.value
    return True, None


def _finish(flow: Generator, result: Any) -> Any:
    """Вернуть телу результат тула и забрать финальный ответ."""
    try:
        flow.send(result)
    except StopIteration as stop:
        return stop.value
    raise RuntimeError(
        "тело гварда отдало управление дважды: у него должен быть ровно один "
        "yield на путь исполнения"
    )


def dual_wrap(
    func: Callable[..., Any],
    flow: Callable[..., Generator],
) -> Callable[..., Any]:
    """Обернуть ``func`` телом-генератором ``flow``.

    Синхронный ``func`` получает синхронную обёртку, корутинный — корутинную.
    ``functools.wraps`` обязателен: гварды достают имена аргументов из
    сигнатуры обёрнутой функции (``_extract_named_value``), и без переноса
    метаданных они извлекали бы не тот аргумент.
    """
    if inspect.iscoroutinefunction(func):
        @functools.wraps(func)
        async def awrapper(*args: Any, **kwargs: Any) -> Any:
            gen = flow(*args, **kwargs)
            # Тело гварда синхронное, и в режиме `ask` внутри него живёт
            # ожидание человека (request_confirm → input()). Прямой прогон
            # заморозил бы event loop вызывающего до ответа — то есть,
            # возможно, навсегда. В поток: политика над строками — чистый CPU,
            # ждать человека там безопасно.
            needs_call, early = await asyncio.to_thread(_start, gen)
            if not needs_call:
                return early
            try:
                result = await func(*args, **kwargs)
            except BaseException:
                # Исключение тула пробрасываем как раньше, но тело закрываем:
                # брошенный незакрытым генератор всплыл бы предупреждением
                # интерпретатора уже в чужом месте.
                gen.close()
                raise
            # _finish тоже может ждать человека (постобработка в режиме ask).
            return await asyncio.to_thread(_finish, gen, result)

        return awrapper

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        gen = flow(*args, **kwargs)
        needs_call, early = _start(gen)
        if not needs_call:
            return early
        try:
            result = func(*args, **kwargs)
        except BaseException:
            gen.close()
            raise
        return _finish(gen, result)

    return wrapper


__all__ = ["dual_wrap"]
