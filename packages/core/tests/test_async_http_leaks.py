"""Пул httpx-клиентов не должен протекать на одноразовых loop и на отказах.

Две дыры, найденные ревью, у которых общий корень — реестр клиентов ключуется
по loop, а loop иногда одноразовый или недожитый.

**Одноразовый loop.** ``run_coro_blocking`` поднимает свой ``asyncio.run``;
``get_async_client`` кладёт в модульный реестр клиента с ключом от ``id`` этого
loop. Закрыть его потом некому: loop мёртв, а реестр держит на него ссылку,
поэтому и сборщик до него не доберётся, и защита «заменим запись при повторе
id» не сработает. Заметнее всего на конфиге по умолчанию
(``command_moderation.enabled``, ``mode: ask``): судья модерации живёт в теле
гварда, которое исполняется вне loop, — то есть КАЖДЫЙ модерируемый
``exec_command`` терял сокет и пул.

**Недожитая сборка.** ``AsyncAgent.create`` заявляет права на пул до
``acreate_run_context``. Сборка падает (типично — отказ MCP-дискавери),
счётчик владельцев остаётся поднятым, и пул этого loop не закроется уже
никогда.
"""
import asyncio

import pytest

from gc_core.utils import async_http
from gc_core.utils.aio_compat import run_coro_blocking


@pytest.fixture(autouse=True)
def clean_registry():
    """Реестры модульные: тест обязан начинать и заканчивать на чистом."""
    async_http._clients.clear()
    async_http._holders.clear()
    yield
    async_http._clients.clear()
    async_http._holders.clear()


async def _touch_client():
    """Сделать ровно то, что делает любой сетевой вызов: взять клиента."""
    return async_http.get_async_client()


def test_run_coro_blocking_leaves_no_client_behind():
    """Главная проверка: после вызова в реестре пусто."""
    for _ in range(5):
        run_coro_blocking(_touch_client())

    assert async_http._clients == {}, (
        f"клиенты одноразовых loop остались в реестре: {len(async_http._clients)}"
    )


def test_the_client_is_actually_closed_not_just_forgotten():
    """Забыть мало — сокет обязан быть закрыт, иначе течёт файловый дескриптор."""
    captured = []

    async def _grab():
        client = async_http.get_async_client()
        captured.append(client)
        return client

    run_coro_blocking(_grab())

    assert captured, "клиент не создавался — проверка пустая"
    assert captured[0].is_closed, "клиент забыт, но не закрыт"


async def test_nested_call_under_a_live_loop_keeps_the_outer_pool():
    """Уборка касается только своего loop — чужой пул не трогаем.

    Иначе лекарство было бы хуже болезни: вызов из-под живого loop закрывал бы
    клиентов, которыми прямо сейчас пользуется агент.
    """
    outer = async_http.get_async_client()

    await asyncio.to_thread(run_coro_blocking, _touch_client())

    assert not outer.is_closed, "уборка закрыла клиента чужого loop"


async def test_holder_is_released_when_context_build_fails(monkeypatch):
    """Отказ сборки не должен навсегда занимать пул этого loop."""
    from gc_core.aio import agent as agent_mod

    async def _boom(*_a, **_kw):
        raise RuntimeError("MCP-дискавери отказала")

    monkeypatch.setattr(agent_mod, "acreate_run_context", _boom)

    with pytest.raises(RuntimeError):
        await agent_mod.AsyncAgent.create({"model": "m"})

    loop = asyncio.get_running_loop()
    assert async_http._holder_count(loop) == 0, (
        f"счётчик владельцев завис на {async_http._holder_count(loop)} — "
        f"пул этого loop уже не закроется"
    )


async def test_holder_count_ignores_a_reused_loop_id():
    """Счётчик привязан к ОБЪЕКТУ loop, а не к его id: id переиспользуются."""
    loop = asyncio.get_running_loop()
    async_http.acquire_clients()
    assert async_http._holder_count(loop) == 1

    # Тот же ключ, но от чужого (уже мёртвого) loop — счётчик читаться не должен.
    dead = asyncio.new_event_loop()
    dead.close()
    async_http._holders[id(loop)] = (lambda: dead, 7)

    assert async_http._holder_count(loop) == 0


# --- M7: реестр клиентов под локом -----------------------------------------


def test_registry_survives_concurrent_loops():
    """Реестр читают и пишут потоки со СВОИМИ loop — по дизайну.

    Их в процессе несколько: рабочий поток TUI, guard-потоки модерации,
    воркеры сервера. Итерация `[k for k in _clients ...]` при вставке из
    другого потока даёт `dictionary changed size during iteration`, и вылетает
    она из `finally` в `_closing_clients` — то есть подменяет результат судьи
    модерации на отказ. GIL тут не спасает: он атомарен на байткоде, а цикл по
    словарю — это много байткодов.
    """
    import threading

    errors: list = []

    def _worker() -> None:
        async def _body():
            async_http.get_async_client()
            await asyncio.sleep(0)
            await async_http.aclose_clients()

        for _ in range(40):
            try:
                asyncio.run(_body())
            except BaseException as exc:  # noqa: BLE001 — нам важен сам факт
                errors.append(exc)
                return

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    assert not errors, f"гонка на реестре: {errors[:3]}"


def test_registry_hands_out_one_client_per_key():
    """Гонка на создании не должна плодить клиентов: затёртый утечёт сокетом."""
    import threading

    seen: list = []
    ready = threading.Barrier(6)

    def _worker() -> None:
        async def _body():
            ready.wait(10)
            seen.append(id(async_http.get_async_client()))

        asyncio.run(_body())

    # Каждый поток со своим loop — ключи разные, но код создания общий:
    # проверяем, что параллельное создание не оставляет записей-сирот.
    threads = [threading.Thread(target=_worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert len(seen) == 6, seen
    # Все клиенты остались в реестре, ни один не затёрт молча.
    assert len(async_http._clients) == 6, len(async_http._clients)
