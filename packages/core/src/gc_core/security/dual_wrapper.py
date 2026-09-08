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


async def _off_loop(fn: Callable[..., Any], *args: Any) -> Any:
    """Исполнить блокирующую часть гварда вне loop — на своём потоке.

    Не ``asyncio.to_thread``: его пул общий и небольшой, и тело гварда встало
    бы в очередь за чужой работой. Замерено на забитом пуле — команда, которая
    сама идёт секунду, занимала 2.25 с, потому что сперва ждала слот.

    ``run_abandonable`` даёт поток-демон на вызов; ровно для этого он и
    написан, см. его докстроку.
    """
    from gc_core.tools.connect_mcp.abandonable import run_abandonable

    return await run_abandonable(fn, *args)


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
    *,
    body_may_block: bool = False,
) -> Callable[..., Any]:
    """Обернуть ``func`` телом-генератором ``flow``.

    Синхронный ``func`` получает синхронную обёртку, корутинный — корутинную.
    ``functools.wraps`` обязателен: гварды достают имена аргументов из
    сигнатуры обёрнутой функции (``_extract_named_value``), и без переноса
    метаданных они извлекали бы не тот аргумент.

    ``body_may_block`` — про ТЕЛО гварда, не про тул. По умолчанию тело идёт
    инлайн: у четырёх гвардов из пяти это десятки микросекунд чистого CPU, и
    поход в пул стоит дороже самой работы.

    Исключение одно — ``moderate_command_execution``: его тело зовёт LLM-судью
    через ``request_auxiliary_completion_text``, а тот идёт ЧЕРЕЗ ПОРТАЛ
    (``summarize/llm``). Портал из-под живого event loop поднимает
    ``SyncApiInsideEventLoopError``, поэтому такому телу нужен поток — там
    живого loop нет. Флаг ставится в самом гварде: только он знает, что у него
    внутри.

    На нативность самого тула это не влияет: ``func`` в обеих ветках ждётся
    через ``await``, и запуск процесса остаётся асинхронным.
    """
    if inspect.iscoroutinefunction(func):
        @functools.wraps(func)
        async def awrapper(*args: Any, **kwargs: Any) -> Any:
            gen = flow(*args, **kwargs)
            # Тело гварда идёт ИНЛАЙН — от синхронной ветки ниже эта
            # отличается ровно словом `await` перед вызовом тула.
            #
            # Раньше здесь стоял `asyncio.to_thread`, чтобы не морозить loop на
            # время раздумий человека в режиме `ask`. Замеры показали, что
            # платим мы за это всегда, а получаем почти никогда:
            #   * тело — 15-25 мкс чистого CPU (signature, urlsplit, fnmatch,
            #     json.dumps), нативной асинхронной формы у него нет;
            #   * два похода в пул стоят 88 мкс — дороже переносимой работы;
            #   * пул ОБЩИЙ, и те же тулы уводят туда разбор HTML
            #     (fetch_url): на параллельных фетчах гвард вставал в очередь
            #     за собственным тулом — 383 мс ожидания на 15 мкс работы;
            #   * ожидание человека на дефолтном конфиге недостижимо (пустой
            #     url_blacklist → любой http/https разрешён), а на сервере
            #     отсекается в confirm_port.confirm_available.
            # Там, где оно достижимо, инлайн даёт ту же заморозку, что уже
            # принята для файловых тулов: их `acall` зовёт синхронную ветку
            # прямо на loop.
            if body_may_block:
                needs_call, early = await _off_loop(_start, gen)
            else:
                needs_call, early = _start(gen)
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
            # Постобработки нет ни у одного гварда в ядре: все `yield` — это
            # `return (yield)`. Поход в пул стоил бы здесь 0.3 мкс работы.
            if body_may_block:
                return await _off_loop(_finish, gen, result)
            return _finish(gen, result)

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
