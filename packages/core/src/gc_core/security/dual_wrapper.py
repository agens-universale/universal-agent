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
import threading


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


#: Слоты для ТЕЛ ГВАРДОВ — свои, не общие с MCP.
#:
#: Прежняя редакция звала ``run_abandonable``, у которого пул один на процесс,
#: и обещала в докстроке «не в общем пуле», не замечая, что это и есть пул
#: MCP-дискавери и MCP-вызовов. Тело модерации в режиме ``ask`` держит слот всё
#: время, пока человек думает над подтверждением: несколько агентов на одном
#: loop с ожидающими подтверждениями выбирали восемь слотов и останавливали
#: MCP-вызовы соседей.
#:
#: Потолок здесь щедрее, потому что эти потоки почти всё время ЖДУТ — человека
#: или судью, — а не считают. Он нужен лишь чтобы сорвавшийся цикл не завёл
#: неограниченное число потоков.
_GUARD_SLOTS = threading.BoundedSemaphore(32)


async def _off_loop(
    fn: Callable[..., Any], *args: Any, on_abandoned: Callable[[], None] | None = None
) -> Any:
    """Исполнить блокирующую часть гварда вне loop — на своём потоке.

    Не ``asyncio.to_thread``: его пул общий и небольшой, и тело гварда встало
    бы в очередь за чужой работой. Замерено на забитом пуле — команда, которая
    сама идёт секунду, занимала 2.25 с, потому что сперва ждала слот.

    Поток-демон на вызов даёт ``run_abandonable_in``; слоты передаём СВОИ —
    см. ``_GUARD_SLOTS``.
    """
    from gc_core.tools.connect_mcp.abandonable import run_abandonable_in

    return await run_abandonable_in(
        _GUARD_SLOTS,
        fn,
        *args,
        _thread_name="guard-blocking",
        _on_abandoned=on_abandoned,
    )


#: Тот же маршрут для тула, который зовёт гард САМ, а не через декоратор
#: (`download_file`: гард записи там внутри `acall`). Имя публичное нарочно:
#: третья копия слотов и `asyncio.to_thread` мимо `run_abandonable_in` стоили
#: бы метки «брошен» — а без неё подтверждение, оставшееся от снятого вызова,
#: дописывает решение по чужому нажатию (находки R4.2 и R5.1).
off_loop = _off_loop


# --- Канал запросов тела ------------------------------------------------------
#
# Тело гварда — синхронный генератор, но блокирующая работа внутри него бывает
# двух РАЗНЫХ сортов, и лечатся они по-разному:
#
#   * поход в сеть (LLM-судья модерации) — у него есть нативная асинхронная
#     форма, и правильное место для неё — loop ВЫЗЫВАЮЩЕГО. Раньше тело
#     целиком уезжало в поток, где живого loop нет, и судья звался через
#     `run_coro_blocking`: свой `asyncio.run` на каждый вызов, а значит свежий
#     пул httpx и новое TLS-рукопожатие к LLM на КАЖДЫЙ модерируемый
#     `exec_command`. Соединение агента при этом простаивало рядом;
#   * ожидание человека (подтверждение в TUI) — асинхронной формы у него нет,
#     и его по-прежнему уводим в поток.
#
# Поэтому тело не блокируется само, а ОТДАЁТ запрос драйверу и получает ответ
# обратно через `send`. Отказ доставляется через `throw`, чтобы `try/except`
# вокруг точки запроса в теле работал ровно так же, как если бы вызов был
# прямым.


class AwaitOnCaller:
    """Дождаться корутины на loop вызывающего — не заводя своего."""

    __slots__ = ("make_coro",)

    def __init__(self, make_coro: Callable[[], Any]) -> None:
        self.make_coro = make_coro


#: «Перехват не нужен» — отличаем от `on_interrupt=False`, где False и есть
#: значение, которое надо отдать телу.
_NO_INTERRUPT = object()


class OffLoop:
    """Выполнить блокирующий вызов в отдельном потоке.

    ``on_interrupt`` — что отдать телу гарда, если во время ожидания пришёл
    Ctrl+C. Ставится ТОЛЬКО там, где ждут человека: подтверждение записи,
    чтения, URL, команды. Без него (умолчание) SIGINT работает как раньше —
    отменяет прогон, и это правильно для ожидания LLM-судьи: там прерывание
    означает «останови всё», а не «человек отказался отвечать».

    Зачем вообще: тело гарда исполняется в рабочем потоке, а сигналы CPython
    доставляет только в главный. Поэтому `except KeyboardInterrupt` внутри
    `prompt_user_confirmation_choice` — мёртвая ветка: поток её не увидит
    никогда, а обработчик asyncio тем временем снимает главную задачу, и
    Ctrl+C на вопросе `y/n` рушит весь прогон вместо вежливого «не разрешаю».
    """

    __slots__ = ("fn", "args", "on_interrupt")

    def __init__(self, fn: Callable[..., Any], *args: Any,
                 on_interrupt: Any = _NO_INTERRUPT) -> None:
        self.fn = fn
        self.args = args
        self.on_interrupt = on_interrupt


def _step(flow: Generator, outcome: Any) -> Any:
    """Отдать телу ответ на его запрос: значение — ``send``, отказ — ``throw``."""
    if isinstance(outcome, _Failed):
        return flow.throw(outcome.exc)
    return flow.send(outcome)


class _Failed:
    """Запрос тела не удался; исключение отдаём в точку запроса."""

    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


_INITIAL = object()


async def _await_off_loop(request: "OffLoop", *, on_abandoned: Callable[[], None]) -> Any:
    """Дождаться блокирующего вызова; для ожидания человека — с перехватом Ctrl+C.

    Перехват НАРОЧНО узкий и временный: он живёт только пока ждут человека и
    только если вызывающий сам об этом попросил. Ставить его на всякое
    ожидание нельзя — тогда Ctrl+C во время работы гарда перестал бы прерывать
    прогон, а это единственный способ остановить агента. Обработчик общий на
    процесс (`intercept_sigint`): ожидания на одном loop выходят не в порядке
    входа, и своё «запомнить и вернуть» у каждого затирало обработчик соседа.

    Поток, сидящий в ожидании, при перехвате бросается: у ``run_abandonable_in``
    это штатное состояние (демон-поток, доставка в закрытый future молча
    отбрасывается). Но брошенное подтверждение живо и могло бы доиграть до
    конца по строке, набранной уже для следующего вопроса. Поэтому оно само
    спрашивает `current_call_abandoned`: читатель `y/n` уходит, не трогая
    чужую строку, а `request_confirm` не пишет решение брошенного вопроса.
    """
    from gc_core.utils.sigint_waits import intercept_sigint

    coro = _off_loop(request.fn, *request.args, on_abandoned=on_abandoned)
    if request.on_interrupt is _NO_INTERRUPT:
        return await coro

    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(coro)
    interrupted = loop.create_future()

    def _on_sigint() -> None:
        loop.call_soon_threadsafe(
            lambda: interrupted.done() or interrupted.set_result(True)
        )

    try:
        with intercept_sigint(_on_sigint):
            done, _ = await asyncio.wait(
                {task, interrupted}, return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                return task.result()
            task.cancel()                  # человека ждать больше не нужно
            return request.on_interrupt
    finally:
        if not interrupted.done():
            interrupted.cancel()


async def _aserve_request(flow: Generator, request: Any) -> Any:
    """Выполнить запрос тела (корутина или off-loop вызов); сбой — как ``_Failed``."""
    try:
        if isinstance(request, AwaitOnCaller):
            return await request.make_coro()
        return await _await_off_loop(request, on_abandoned=lambda: _close_quietly(flow))
    except asyncio.CancelledError:
        # Отмену в тело НЕ бросаем: его `except Exception` её не
        # поймает, но проглотить её могло бы чужое `except
        # BaseException`, и вызов остался бы неотменяемым.
        _close_quietly(flow)
        raise
    except Exception as exc:  # noqa: BLE001 — отдаём в точку запроса
        return _Failed(exc)


async def _drive_async(flow: Generator, first: Any = _INITIAL) -> tuple[bool, Any]:
    """Крутить тело, обслуживая его запросы, до точки вызова тула или конца.

    Возвращает ``(нужен_ли_вызов_тула, результат)``.
    """
    outcome: Any = first
    while True:
        try:
            request = next(flow) if outcome is _INITIAL else _step(flow, outcome)
        except StopIteration as stop:
            return False, stop.value
        if isinstance(request, (AwaitOnCaller, OffLoop)):
            outcome = await _aserve_request(flow, request)
            continue
        return True, None


def _drive_sync(flow: Generator, first: Any = _INITIAL) -> tuple[bool, Any]:
    """То же для синхронной обёртки: живого loop здесь нет, ждать негде."""
    from gc_core.utils.aio_compat import run_coro_blocking

    outcome: Any = first
    while True:
        try:
            request = next(flow) if outcome is _INITIAL else _step(flow, outcome)
        except StopIteration as stop:
            return False, stop.value
        if isinstance(request, (AwaitOnCaller, OffLoop)):
            try:
                if isinstance(request, AwaitOnCaller):
                    outcome = run_coro_blocking(request.make_coro())
                else:
                    outcome = request.fn(*request.args)
            except Exception as exc:  # noqa: BLE001
                outcome = _Failed(exc)
            continue
        return True, None


def _close_quietly(flow: Generator) -> None:
    """Закрыть тело гварда, если оно не исполняется прямо сейчас.

    В момент отмены тело обычно ещё внутри блокирующего вызова в своём
    потоке, и `close()` на нём даёт `ValueError: generator already
    executing`. Тогда закрывать нечего и рано — это сделает `on_abandoned`
    в том самом потоке, когда тело вернётся.
    """
    try:
        flow.close()
    except ValueError:
        pass


def _forbid_second_call(again: bool) -> None:
    """У тела должна быть ровно одна точка вызова тула на путь исполнения.

    Запросы (`AwaitOnCaller`, `OffLoop`) сюда не считаются: их драйвер
    обслуживает сам и телу их отдаёт обратно, не выходя из цикла.
    """
    if again:
        raise RuntimeError(
            "тело гварда отдало управление дважды: у него должен быть ровно "
            "один yield на путь исполнения"
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

    Исключение одно — ``moderate_command_execution``: его тело синхронное и
    внутри зовёт LLM-судью. Синхронный сетевой вызов на loop заморозил бы
    приложение на всё время ответа судьи, поэтому такому телу нужен поток —
    там живого loop нет и блокировать нечего. Флаг ставится в самом гварде:
    только он знает, что у него внутри.

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
            #     за собственным тулом — 383 мс ожидания на 15 мкс работы.
            #
            # Блокирующие куски тела едут не вместе с телом, а поштучно —
            # через его запросы (`AwaitOnCaller`, `OffLoop`), см. канал выше.
            if body_may_block:
                try:
                    needs_call, early = await _off_loop(
                        _start, gen, on_abandoned=lambda: _close_quietly(gen)
                    )
                except asyncio.CancelledError:
                    # Вызывающего сняли, пока тело работало в своём потоке.
                    # Поток мы не остановим — он в блокирующем вызове, и
                    # прервать его нечем; это заявленное свойство
                    # `run_abandonable`. Закрыть тело отсюда тоже нельзя, пока
                    # оно исполняется, — уборку доделает `on_abandoned` в том
                    # же потоке, когда тело вернётся.
                    _close_quietly(gen)
                    raise
            else:
                needs_call, early = await _drive_async(gen)
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
            # Постобработка идёт ИНЛАЙН всегда, даже у тел с `body_may_block`.
            #
            # Постобработки нет ни у одного гварда в ядре: все `yield` — это
            # `return (yield)`, то есть 0.3 мкс работы. Второй поток-демон на
            # неё заводился по симметрии с `_start`, и это была чистая трата:
            # у `run_abandonable` каждый вызов — свой поток, значит один
            # `exec_command` стоил ДВУХ потоков вместо одного.
            again, value = await _drive_async(gen, result)
            _forbid_second_call(again)
            return value

        return awrapper

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        gen = flow(*args, **kwargs)
        needs_call, early = _drive_sync(gen)
        if not needs_call:
            return early
        try:
            result = func(*args, **kwargs)
        except BaseException:
            gen.close()
            raise
        again, value = _drive_sync(gen, result)
        _forbid_second_call(again)
        return value

    return wrapper


__all__ = ["AwaitOnCaller", "OffLoop", "dual_wrap"]
