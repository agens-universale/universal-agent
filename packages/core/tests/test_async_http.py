"""Пер-loop httpx-клиент (план async-переезда, Задача 8)."""

import asyncio

from gc_core.utils.async_http import (
    aclose_clients,
    get_async_client,
    mounts_from_proxies,
)


async def test_client_is_reused_within_one_loop():
    """Пул соединений имеет смысл только при переиспользовании клиента."""
    a = get_async_client(verify=True)
    b = get_async_client(verify=True)
    assert a is b
    await aclose_clients()


async def test_different_verify_gives_different_client():
    a = get_async_client(verify=True)
    b = get_async_client(verify=False)
    assert a is not b
    await aclose_clients()


async def test_different_proxies_give_different_clients():
    a = get_async_client(proxies={"https": "http://p1:1"})
    b = get_async_client(proxies={"https": "http://p2:2"})
    assert a is not b
    await aclose_clients()


def test_client_is_not_shared_across_loops():
    """Клиент привязан к своему loop: общий на процесс дал бы «Event loop is
    closed» на живом запросе к модели.

    Сравнивать id() объектов нельзя — после закрытия первого клиента второй
    может лечь по тому же адресу. Считаем фактические создания.
    """
    import httpx

    created = []
    real_init = httpx.AsyncClient.__init__

    def _counting_init(self, *args, **kwargs):
        created.append(1)
        return real_init(self, *args, **kwargs)

    httpx.AsyncClient.__init__ = _counting_init
    try:

        async def grab():
            get_async_client(verify=True)
            get_async_client(verify=True)  # тот же loop → переиспользование
            await aclose_clients()

        asyncio.run(grab())
        asyncio.run(grab())
    finally:
        httpx.AsyncClient.__init__ = real_init

    assert len(created) == 2, "на каждый loop должен создаваться свой клиент"


async def test_trust_env_is_off():
    """Тот же контракт, что у синхронной NO_NETRC_SESSION: ~/.netrc не читаем
    (Basic-auth оттуда перетирает явный Authorization → 401)."""
    c = get_async_client(verify=True)
    assert c.trust_env is False
    await aclose_clients()


async def test_closed_client_is_replaced():
    c = get_async_client(verify=True)
    await c.aclose()
    assert get_async_client(verify=True) is not c
    await aclose_clients()


def test_mounts_from_proxies_translates_requests_dict():
    """resolve_proxies отдаёт форму requests; httpx её не понимает."""
    m = mounts_from_proxies({"http": "http://p:1", "https": "http://p:1"})
    assert set(m) == {"http://", "https://"}
    assert mounts_from_proxies({}) is None
    assert mounts_from_proxies(None) is None
    assert mounts_from_proxies({"http": None}) is None
