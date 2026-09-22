"""Запуск блокирующей работы, которую можно БРОСИТЬ по дедлайну.

``asyncio.to_thread`` для этого не годится, и это не мелочь: пул у него
дефолтный, а ``asyncio.run`` на выходе зовёт ``shutdown_default_executor()`` и
ЖДЁТ все его потоки. Один зависший MCP-сервер задерживал бы весь процесс, хотя
дедлайн дискавери давно вышел — проверено тестом
``test_build_registry_parallel_and_status``: с ``to_thread`` он показывал 3.0 с
при дедлайне 1 с.

``ThreadPoolExecutor`` тоже не подходит: его воркеры создаются и стартуют
внутри ``_adjust_thread_count``, выставить им ``daemon`` уже нельзя
(``RuntimeError: cannot set daemon status of active thread``), а не-демон
поток задержит выход интерпретатора не хуже atexit-join.

Дискавери зовёт БЛОКИРУЮЩИЙ код (requests, subprocess) — отменить его нечем,
единственный выход — перестать его ждать. Поэтому здесь поток-демон на вызов,
ограниченный семафором: брошенный воркер дочитает свой сокет в никуда и умрёт
вместе с процессом.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from typing import Any, Callable

from gc_core.mcp_session_owner import copy_context_for_thread

#: Одновременных блокирующих дискавери. Больше смысла нет: они ждут сеть, а не
#: считают. До переезда границы не было вовсе — поток на каждый сервер.
_MAX_WORKERS = 8

_slots = threading.BoundedSemaphore(_MAX_WORKERS)


logger = logging.getLogger(__name__)

#: Как потоку спросить, не бросили ли вызов, ради которого он работает. Вне
#: `run_abandonable_in` — «нет».
_ABANDONED: contextvars.ContextVar[Callable[[], bool]] = contextvars.ContextVar(
    "gc_call_abandoned", default=lambda: False
)


def current_call_abandoned() -> bool:
    """Брошен ли вызов, в потоке которого исполняется код.

    Для большинства работ «бросили» значит лишь «результат никому не нужен». Но
    у ожидания человека после отказа ждать остаются ПОСЛЕДСТВИЯ: брошенное по
    Ctrl+C подтверждение дождалось бы строки, набранной уже для следующего
    вопроса, и записало бы решение в память разрешений по чужому нажатию. Тело
    спрашивает этот признак, чтобы не доигрывать то, что вызывающий бросил.
    """
    return _ABANDONED.get()()


async def run_abandonable_in(
    slots: threading.BoundedSemaphore,
    fn: Callable[..., Any],
    /,
    *args: Any,
    _thread_name: str = "mcp-blocking",
    _on_abandoned: Callable[[], None] | None = None,
    **kwargs: Any,
) -> Any:
    """То же, что :func:`run_abandonable`, но с ЯВНЫМ набором слотов.

    Слоты разделены не из аккуратности. Пока пул был один на процесс, тела
    гвардов делили восемь слотов с MCP-дискавери и вызовами тулов, а тело
    модерации в режиме ``ask`` держит слот всё время, пока человек думает над
    подтверждением. Несколько агентов на одном loop (собственный критерий
    приёмки ветки) с ожидающими подтверждениями выбирали пул целиком и
    останавливали MCP-вызовы СОСЕДНИХ агентов. Докстрока в ``dual_wrapper``
    при этом обещала «не в общем пуле», не замечая, что это и есть пул MCP.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def _settle(setter: Callable[[Any], None], value: Any) -> None:
        # Вызывающего могли отменить, пока поток работал — тогда future уже
        # закрыт, и повторная установка результата бросила бы InvalidStateError
        # прямо в колбэке loop.
        if not future.done():
            setter(value)

    def _deliver(setter: Callable[[Any], None], value: Any) -> None:
        # Поток мог пережить не только вызывающего, но и весь его loop —
        # именно так и выглядит «брошенный». Тогда доставлять некуда и незачем;
        # без этой проверки в потоке всплывает RuntimeError: Event loop is
        # closed, и pytest справедливо ругается на исключение в чужом потоке.
        try:
            loop.call_soon_threadsafe(_settle, setter, value)
        except RuntimeError:
            pass

    # Голый поток контекст НЕ наследует (в отличие от asyncio.to_thread).
    # Без явного снимка сессия, поднятая дискавери здесь, оказалась бы ничьей,
    # и её не закрыл бы ни один прогон — Chromium пережил бы агента.
    ctx_snapshot = copy_context_for_thread()

    def _abandoned() -> bool:
        """Ждать результат уже некому: вызывающего сняли или loop закрыт."""
        return future.cancelled() or loop.is_closed()

    def _marked() -> Any:
        # Признак ставится ВНУТРИ снимка контекста: он виден только этому
        # вызову и его потоку, а не соседним вызовам с тем же снимком.
        _ABANDONED.set(_abandoned)
        return fn(*args, **kwargs)

    def _cleanup_if_abandoned() -> None:
        """Уборка за брошенным вызовом. Сам ВЫЗЫВАЮЩИЙ прибраться не может: в
        момент отмены `fn` ещё исполняется, и попытка закрыть его тело даёт
        `ValueError: generator already executing`. Здесь же `fn` либо уже
        вернулся, либо не запускался вовсе — закрывать безопасно, и делать это
        надо именно тут, в потоке, который один и дожил до конца."""
        if _on_abandoned is not None and _abandoned():
            try:
                _on_abandoned()
            except BaseException:  # уборку не роняем
                logger.debug("abandoned cleanup failed", exc_info=True)

    def _target() -> None:
        with slots:
            # Слот мог освободиться уже после того, как вызов бросили: у MCP их
            # восемь, и на веере дискавери или нескольких агентах очередь
            # реальна. Тело тогда не исполняем вовсе — иначе отменённый ход всё
            # равно уходил бы на сервер с побочными эффектами. Доставлять
            # нечего: future уже отменён либо loop закрыт. А уборку зовём — она
            # закрывает тело гварда, созданное ДО ухода в поток.
            if _abandoned():
                _cleanup_if_abandoned()
                return
            try:
                result = ctx_snapshot.run(_marked)
            except BaseException as exc:  # переносим как есть
                _deliver(future.set_exception, exc)
            else:
                _deliver(future.set_result, result)
            _cleanup_if_abandoned()

    threading.Thread(target=_target, daemon=True, name=_thread_name).start()
    return await future


async def run_abandonable(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Выполнить ``fn`` в потоке-демоне на пуле MCP и дождаться результата.

    Если вызывающего отменили (истёк ``asyncio.timeout``), поток продолжит
    работу в фоне: результат к тому моменту уже никому не нужен, а прервать
    блокирующий сокет всё равно нечем.
    """
    return await run_abandonable_in(_slots, fn, *args, **kwargs)


__all__ = ["current_call_abandoned", "run_abandonable", "run_abandonable_in"]
