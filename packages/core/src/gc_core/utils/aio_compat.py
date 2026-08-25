"""Исполнить корутину из синхронного кода, даже если на потоке живёт loop.

Переехало из ``tools/connect_mcp/stream_client._run_coro``, где решало ровно
эту задачу: MCP-клиент внутри асинхронный, а звали его из синхронного ядра —
в том числе из-под работающего loop Textual, где ``asyncio.run`` падает
с ``RuntimeError: cannot be called from a running event loop``.

Отличие от ``utils.portal``: здесь поток одноразовый и loop не переиспользуется.
Это совместимостный костыль для редких мест, а не транспорт синхронного API —
для него есть портал.

Раньше ту же дыру закрывал ``gc_server`` глобальным monkey-patch ``asyncio.run``
(снят вместе с появлением этого модуля). Патч действовал на весь процесс,
включая код встраивающего приложения: библиотека не имеет права менять
семантику asyncio под тем, кто её импортировал.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
from typing import Any, Coroutine


def run_coro_blocking(coro: Coroutine[Any, Any, Any]) -> Any:
    """Дождаться корутины из синхронного кода.

    Нет активного loop — обычный ``asyncio.run``. Есть — уводим корутину в
    отдельный поток с собственным loop: сам вызывающий поток при этом честно
    блокируется, как и ожидает синхронный код вокруг.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


async def await_callback(result: Any) -> Any:
    """Дождаться результата КОЛБЭКА, если он корутинный.

    Узкое и оговорённое место, а не общее «подождём, если вдруг корутина». У
    двух колбэков ядра — персиста батча и записи трассы субагента —
    контрактом объявлено: возвращают ``None`` ЛИБО awaitable. Ядро передаёт
    асинхронные (тред может лежать в базе), внешние вызывающие и тесты —
    синхронные, и ломать их не за что. Тот же приём, что у ASGI-приложений.

    Не путать с удалённым ``_maybe_await``: тот стоял на ЗНАЧЕНИЯХ по всему
    ядру и молча проглатывал забытый ``await`` у наших собственных функций.
    Здесь ждут ровно то, чему разрешено быть корутиной, и только на шве, где
    колбэк вызывают.
    """
    if inspect.isawaitable(result):
        return await result
    return result


__all__ = ["await_callback", "run_coro_blocking"]
