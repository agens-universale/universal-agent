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

    assert async_http._clients == {}, f"клиенты одноразовых loop остались в реестре: {len(async_http._clients)}"


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
        f"счётчик владельцев завис на {async_http._holder_count(loop)} — пул этого loop уже не закроется"
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
            except BaseException as exc:
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
            # Держим САМ объект, а не id: после сборки id переиспользуются,
            # и проверка на различие стала бы случайной.
            seen.append(async_http.get_async_client())

        asyncio.run(_body())

    # Каждый поток со своим loop — ключи разные, но код создания общий:
    # проверяем, что параллельное создание не оставляет записей-сирот.
    threads = [threading.Thread(target=_worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert len(seen) == 6, seen
    # Ни один не затёрт молча: у каждого потока СВОЙ клиент.
    #
    # Считать записи в реестре здесь больше нельзя: loop'ы этих потоков к
    # моменту проверки уже мертвы, а мёртвые записи снимаются (см.
    # `test_registry_does_not_accumulate_dead_loops`). Затёртый клиент виден
    # прямо — как совпадение объектов, — и это и есть проверяемое свойство:
    # затёртый утёк бы сокетом.
    assert len({id(c) for c in seen}) == 6, "два потока получили одного клиента"


# --- L6: реестр не копит мёртвые записи, общий пул не декорация ------------


def test_registry_does_not_accumulate_dead_loops():
    """Записи мёртвых loop'ов снимаются, а не копятся до конца процесса.

    Раньше их убирала только защита «id совпал, а объект другой» — то есть
    лишь когда id ПЕРЕИСПОЛЬЗОВАЛИ. Процесс с множеством коротких
    ``asyncio.run`` (TUI заводит по одному на ход) рос словарём без счёта:
    сокеты закрывал выход из ``asyncio.run``, а запись оставалась.
    """
    import gc

    async def _touch():
        async_http.get_async_client()

    for _ in range(30):
        asyncio.run(_touch())
    gc.collect()
    # Ещё один вызов — он и запускает уборку.
    asyncio.run(_touch())
    gc.collect()

    alive = [k for k, (ref, _c) in async_http._clients.items() if ref() is not None]
    assert len(async_http._clients) <= len(alive) + 1, (
        f"реестр копит мёртвые записи: всего {len(async_http._clients)}, живых {len(alive)}"
    )


def test_shared_sync_client_is_actually_used():
    """`verify=True` обязан идти через ОБЩИЙ клиент, а не поднимать свой.

    Условие было `verify is not None`, а вызывающие передают `verify=`
    всегда: общий пул не использовался ни разу, и каждый запрос платил своим
    TLS-рукопожатием.
    """
    import httpx
    from gc_core.utils.http_session import NO_NETRC_SESSION

    one_shots: list = []
    real_client = httpx.Client

    class _Counting(httpx.Client):
        def __init__(self, *a, **kw):
            one_shots.append(kw)
            super().__init__(*a, **kw)

    def _dispatch(request):
        return httpx.Response(200, json={"ok": True})

    # Клиент ОБЩИЙ на процесс, поэтому подменённый транспорт обязан вернуться
    # так же, как `httpx.Client`. Без этого следующие тесты прогона получают
    # через него `200 {"ok": true}`: тест голоса ждал отказа соединения, видел
    # пустой ответ и падал — в изоляции зелёный, в полном прогоне красный.
    real_transport = NO_NETRC_SESSION._transport
    NO_NETRC_SESSION._transport = httpx.MockTransport(_dispatch)
    httpx.Client = _Counting
    try:
        NO_NETRC_SESSION.request("GET", "https://example.com", verify=True)
    finally:
        httpx.Client = real_client
        NO_NETRC_SESSION._transport = real_transport

    assert one_shots == [], "verify=True снова поднял одноразовый клиент — общий пул декоративен"
