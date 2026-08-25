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
import threading

from gc_core.mcp_session_owner import copy_context_for_thread
from typing import Any, Callable

#: Одновременных блокирующих дискавери. Больше смысла нет: они ждут сеть, а не
#: считают. До переезда границы не было вовсе — поток на каждый сервер.
_MAX_WORKERS = 8

_slots = threading.BoundedSemaphore(_MAX_WORKERS)


async def run_abandonable(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Выполнить ``fn`` в потоке-демоне и дождаться результата.

    Если вызывающего отменили (истёк ``asyncio.timeout``), поток продолжит
    работу в фоне: результат к тому моменту уже никому не нужен, а прервать
    блокирующий сокет всё равно нечем.
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

    def _target() -> None:
        with _slots:
            try:
                result = ctx_snapshot.run(fn, *args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 — переносим как есть
                _deliver(future.set_exception, exc)
            else:
                _deliver(future.set_result, result)

    threading.Thread(
        target=_target, daemon=True, name="mcp-blocking"
    ).start()
    return await future


__all__ = ["run_abandonable"]
