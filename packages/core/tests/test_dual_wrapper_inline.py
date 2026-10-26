"""Тело гварда исполняется на loop, а не в потоке из пула.

``dual_wrap`` даёт корутинной функции корутинную обёртку, и она уводила тело
гварда (``_start``/``_finish``) в ``asyncio.to_thread``. Мотив был один:
в режиме ``ask`` внутри тела живёт ожидание человека, и прямой прогон
заморозил бы loop до ответа.

Замеры показали, что цена не окупается:

* тело — чистый CPU на 15-25 мкс (``inspect.signature``, ``urlsplit``,
  ``fnmatch``, ``json.dumps``), нативной асинхронной формы у него нет;
* два похода в пул стоят 88 мкс — в 1.6 раза дороже переносимой работы;
* ``_finish`` не делает НИЧЕГО: все ``yield`` во всех пяти гвардах кодовой
  базы — ``return (yield)``, постобработки нет ни у одного. Поход в пул ради
  0.3 мкс;
* хуже того, пул общий: те же тулы уводят туда разбор HTML
  (``fetch_url.py``), и на параллельных фетчах гвард вставал в очередь за
  собственным тулом — замерено 383 мс ожидания на 15 мкс работы.

Ожидание человека при этом на дефолтном конфиге недостижимо (пустой
``url_blacklist`` → любой http/https разрешён), а на сервере отсекается в
``confirm_port.confirm_available`` до всякого ожидания. Там же, где оно
достижимо, инлайн даёт ровно ту заморозку, которая уже принята для четырнадцати
файловых тулов: их ``acall`` зовёт синхронную ветку ``dual_wrap`` прямо на loop.

После правки ``awrapper`` отличается от ``wrapper`` одним словом ``await`` —
той формой, которую обещает докстринг модуля.
"""

import asyncio
import threading

import pytest
from gc_core.security.dual_wrapper import dual_wrap


def _recording_flow(seen: dict):
    """Тело гварда, запоминающее, на каком потоке его исполнили."""

    def _flow(*_args, **_kwargs):
        seen["start"] = threading.get_ident()
        result = yield
        seen["finish"] = threading.get_ident()
        return result

    return _flow


async def test_async_guard_body_runs_on_the_calling_thread():
    """Оба конца тела — ``_start`` и ``_finish`` — идут инлайн."""
    seen = {}

    async def target(x):
        return f"ok:{x}"

    wrapped = dual_wrap(target, _recording_flow(seen))

    assert await wrapped("раз") == "ok:раз"
    assert seen["start"] == threading.get_ident(), "начало тела уехало в поток"
    assert seen["finish"] == threading.get_ident(), "конец тела уехал в поток"


async def test_async_guard_can_still_refuse_before_calling_the_tool():
    """Ранний отказ работает: тул не зовётся, ответ гварда доезжает."""
    called = {"n": 0}

    def _blocking_flow(*_args, **_kwargs):
        # Ранний отказ по контракту `_start`: тело завершается, НЕ отдав
        # управление. `yield` ниже недостижим и нужен лишь для того, чтобы
        # функция осталась генераторной.
        return {"blocked": True}
        yield  # pragma: no cover

    async def target(_x):
        called["n"] += 1
        return "не должно случиться"

    wrapped = dual_wrap(target, _blocking_flow)

    assert await wrapped("раз") == {"blocked": True}
    assert called["n"] == 0


async def test_async_guard_closes_the_body_when_the_tool_raises():
    """Исключение тула пробрасывается, а генератор закрывается.

    Брошенный незакрытым генератор всплыл бы предупреждением интерпретатора
    уже в чужом месте — поведение синхронной ветки, которое обязано совпасть.
    """
    seen = {}

    async def target(_x):
        raise RuntimeError("тул упал")

    wrapped = dual_wrap(target, _recording_flow(seen))

    with pytest.raises(RuntimeError, match="тул упал"):
        await wrapped("раз")
    assert "finish" not in seen, (
        "после падения тула постобработки быть не должно"
    )


async def test_guard_does_not_queue_behind_a_busy_default_executor():
    """Гвард не ждёт освобождения пула, занятого этим же тулом.

    Прежняя форма ставила проверку строки в ту же очередь, куда тул уводит
    разбор HTML: замерено 383 мс ожидания на 15 мкс работы. Инлайн от этого
    свободен по построению — тест сторожит именно это.
    """
    seen = {}

    async def target(_x):
        return "ok"

    wrapped = dual_wrap(target, _recording_flow(seen))

    # Забиваем пул: воркеров у него немного, а заданий заведомо больше.
    hogs = [
        asyncio.create_task(asyncio.to_thread(_busy_wait, 0.5))
        for _ in range(24)
    ]
    await asyncio.sleep(0.05)  # даём им занять места

    started = asyncio.get_running_loop().time()
    assert await wrapped("раз") == "ok"
    spent = asyncio.get_running_loop().time() - started

    for h in hogs:
        h.cancel()
    await asyncio.gather(*hogs, return_exceptions=True)

    assert spent < 0.1, (
        f"гвард ждал {spent * 1000:.0f} мс при занятом пуле — он всё ещё ходит в него"
    )


def _busy_wait(seconds: float) -> None:
    import time

    time.sleep(seconds)


def test_sync_guard_path_unchanged():
    """Синхронная ветка не тронута — по ней идут четырнадцать файловых тулов."""
    seen = {}

    def target(x):
        return f"ok:{x}"

    wrapped = dual_wrap(target, _recording_flow(seen))

    assert wrapped("раз") == "ok:раз"
    assert seen["start"] == threading.get_ident()
    assert seen["finish"] == threading.get_ident()


async def test_explicit_guard_chain_preserves_order_and_postprocessing():
    from gc_core.security.dual_wrapper import arun_guard_chain, dual_wrap

    trace = []

    async def body(value):
        trace.append("body")
        return value

    def inner(_value):
        trace.append("inner_before")
        result = yield
        trace.append("inner_after")
        return result + 1

    def outer(_value):
        trace.append("outer_before")
        result = yield
        trace.append("outer_after")
        return result * 2

    wrapped = dual_wrap(dual_wrap(body, inner), outer)
    assert await wrapped(3) == 8
    local_trace = list(trace)
    trace.clear()

    async def effect():
        trace.append("body")
        return 3

    assert await arun_guard_chain(wrapped, 3, invoke_leaf=effect) == 8
    assert trace == local_trace
