"""Клиент из реестра принадлежит ТОМУ loop, который его просит.

Ключ кэша включает `id(loop)`, а id в CPython переиспользуются: у loop,
созданных подряд через `asyncio.run`, адреса повторяются — проверено запуском,
шестой совпал с третьим. Без сверки самого объекта новый loop получал бы
клиента МЁРТВОГО, и запрос к модели падал бы «Event loop is closed».

Пока агент жил на одном долгом loop (портал), дыра была недостижима. С
переходом фронтендов на свой `asyncio.run` на каждый прогон — достижима, и
проявлялась бы редко и не воспроизводимо: только когда аллокатор отдаст новому
loop адрес умершего.
"""
import asyncio
import gc

from gc_core.utils import async_http


def test_client_is_rebuilt_when_the_loop_id_is_reused():
    """Тот же ключ, другой loop — новый клиент, а не чужой мёртвый."""
    async_http._clients.clear()

    async def take():
        client = async_http.get_async_client()
        return id(asyncio.get_running_loop()), client

    seen: dict[int, object] = {}
    reused_id = None
    for _ in range(12):
        loop_id, client = asyncio.run(take())
        if loop_id in seen:
            reused_id = loop_id
            # Тот же адрес loop — но клиент обязан быть ДРУГИМ объектом.
            assert client is not seen[loop_id], (
                "новый loop получил клиента, созданного для умершего loop с тем "
                "же id — это и есть «Event loop is closed» на живом запросе"
            )
            break
        seen[loop_id] = client
        gc.collect()

    if reused_id is None:
        # Аллокатор не переиспользовал ни одного адреса за 12 попыток: сам факт
        # переиспользования от нас не зависит, поэтому это не провал теста.
        # Проверяем инвариант напрямую — записи помнят СВОЙ loop.
        assert all(
            isinstance(entry, tuple) and len(entry) == 2
            for entry in async_http._clients.values()
        ), "реестр не хранит ссылку на loop — сверить принадлежность нечем"


def test_registry_entry_remembers_its_loop():
    """Прямая проверка инварианта, без надежды на переиспользование адреса."""
    async_http._clients.clear()

    async def take():
        async_http.get_async_client()
        return asyncio.get_running_loop()

    loop = asyncio.run(take())

    assert async_http._clients, "клиент не попал в реестр"
    for loop_ref, _client in async_http._clients.values():
        assert loop_ref() is None or loop_ref() is loop, (
            "запись реестра указывает на ЧУЖОЙ loop"
        )
