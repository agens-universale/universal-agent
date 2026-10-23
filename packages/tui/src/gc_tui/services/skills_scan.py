"""Список скиллов реестра без захвата цикла интерфейса (технический слой:
без Textual и текстов интерфейса).

Реестр обходит диск синхронно внутри своих async-методов, поэтому на цикле
интерфейса обход держит ввод. Обход идёт в потоке-демоне со своим циклом и
свежим реестром (у реестра и файлового бэкенда нет объектов, привязанных к
циклу), результат возвращается в цикл вызывающего. Именно демон, а не
`asyncio.to_thread`: дефолтный executor `asyncio.run()` на выходе из
`app.run()` дожидался бы обхода — та же ловушка, что в `file_search`.
Отмена ожидающего (сброс прогрева, уход экрана) результат отбрасывает.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any


def _scan(settings: Any) -> list[dict]:
    from gc_core.tools.skills.registry import get_skills_registry

    reg = get_skills_registry(settings)
    # include_all=True — и внутренние скиллы паков, не только bootstrap.
    return asyncio.run(reg.list_skills(include_all=True))


def _deliver(fut: asyncio.Future, result: Any, exc: Any) -> None:
    if fut.done():  # ожидающий отменён — результат никому не нужен
        return
    if exc is not None:
        fut.set_exception(exc)
    else:
        fut.set_result(result)


async def list_skills_off_loop(settings: Any) -> list[dict]:
    """Все скиллы реестра; обход диска — вне текущего цикла."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    def _work() -> None:
        result, exc = None, None
        try:
            result = _scan(settings)
        except Exception as err:  # отдаём вызывающему как есть
            exc = err
        try:
            loop.call_soon_threadsafe(_deliver, fut, result, exc)
        except RuntimeError:  # цикл уже закрыт — приложение вышло
            pass

    threading.Thread(target=_work, daemon=True, name="skills-scan").start()
    return await fut
