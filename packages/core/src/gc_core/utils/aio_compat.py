"""Исполнить корутину из синхронного кода, даже если на потоке живёт loop.

Переехало из ``tools/connect_mcp/stream_client._run_coro``, где решало ровно
эту задачу: MCP-клиент внутри асинхронный, а звали его из синхронного ядра —
в том числе из-под работающего loop Textual, где ``asyncio.run`` падает
с ``RuntimeError: cannot be called from a running event loop``.

Поток одноразовый и loop не переиспользуется: это костыль для считаных швов,
где синхронное тело обязано дождаться корутины, а не транспорт синхронного
API — синхронного API у ядра больше нет.

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


async def _closing_clients(coro: Coroutine[Any, Any, Any]) -> Any:
    """Выполнить корутину и прибрать за ней пул httpx на ЭТОМ loop.

    Без этого каждый вызов протекал. ``get_async_client`` кэширует клиент по
    ключу с ``id(loop)``, а loop здесь одноразовый: клиент, его keep-alive
    сокет и сам мёртвый loop оставались в модульном реестре навсегда. Защита
    «заменим запись, если id повторился» не срабатывала — мёртвый loop не
    собирался сборщиком, потому что реестр держал на него ссылку.

    Заметнее всего это на конфиге по умолчанию (``command_moderation.enabled``,
    ``mode: ask``): судья модерации живёт в теле гварда, которое исполняется
    вне loop, поэтому КАЖДЫЙ модерируемый ``exec_command`` терял сокет и пул и
    платил новый TLS-handshake.

    Закрываем только клиентов текущего loop (``aclose_clients`` фильтрует по
    нему) — чужие живут на своих loop, и закрыть их отсюда всё равно нечем.
    """
    from gc_core.utils.async_http import aclose_clients

    try:
        return await coro
    finally:
        await aclose_clients()


def run_coro_blocking(coro: Coroutine[Any, Any, Any]) -> Any:
    """Дождаться корутины из синхронного кода.

    Нет активного loop — обычный ``asyncio.run``. Есть — уводим корутину в
    отдельный поток с собственным loop: сам вызывающий поток при этом честно
    блокируется, как и ожидает синхронный код вокруг.

    В обоих случаях loop одноразовый, поэтому за ним прибираем: см.
    :func:`_closing_clients`.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_closing_clients(coro))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, _closing_clients(coro)).result()


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
