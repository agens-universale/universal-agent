"""Ожидание фонового поиска файлового пикера в тестах экрана.

Поиск считает поток сеанса (`gc_tui.services.file_search.FileSearchSession`), а
результат он отдаёт экрану через синхронный `call_from_thread` — то есть ждёт UI-поток.
Поэтому ждать его простоя из UI-потока нельзя (взаимная блокировка): `wait_idle` уходит
в поток, цикл событий свободен и применяет результат. После возврата результат уже на
экране — остаётся `pilot.pause()` на отрисовку."""

from __future__ import annotations

import asyncio

# Только страховка от зависания: у прежнего `worker.wait()` таймаута не было, а под
# нагрузкой полного прогона короткий срок ловил бы флапы.
_TIMEOUT = 30.0


def wait_idle(search, timeout: float) -> bool:
    """True — всё, что отдано сеансу через `want`, посчитано и отдано в `on_result`
    (или отброшено из-за закрытия), либо поток вышел; False — вышел таймаут. Не из
    UI-потока: поток может ждать его внутри `on_result`. Читает условие и флаги
    сеанса (`_cond`, `_pending`, `_busy`, `_exited`) — прод-коду ждать простоя не нужно."""
    with search._cond:
        return search._cond.wait_for(
            lambda: (
                (search._pending is None and not search._busy)
                or search._exited
            ),
            timeout,
        )


async def settled(search) -> None:
    """Поток сеанса посчитал всё, что ему отдали, и отдал экрану — или вышел."""
    if not await asyncio.to_thread(wait_idle, search, _TIMEOUT):
        raise TimeoutError(f"поиск файлов не закончился за {_TIMEOUT} с")
