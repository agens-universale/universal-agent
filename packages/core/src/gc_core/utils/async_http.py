"""Пер-loop реестр ``httpx.AsyncClient`` (план async-переезда, Задача 8).

Процессно-глобальный клиент, как синхронная ``NO_NETRC_SESSION`` у requests,
здесь невозможен: ``AsyncClient`` держит пул соединений, привязанный к своему
event loop. Клиент, созданный на портальном loop и переиспользованный на loop
приложения, даёт «Event loop is closed» в самом неудобном месте — на живом
запросе к модели. Поэтому ключ кэша включает ``id(loop)``.

Контракт по окружению — тот же, что у синхронной сессии: ``trust_env=False``.
``~/.netrc`` не читаем (Basic-auth оттуда перетирает явный Authorization и даёт
401), env-прокси и REQUESTS_CA_BUNDLE не подхватываем — CA и прокси передаём
явно.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

_clients: dict[tuple, httpx.AsyncClient] = {}


def mounts_from_proxies(proxies: Optional[dict]) -> Optional[dict]:
    """Перевести requests-словарь прокси в httpx-``mounts``.

    ``utils.http_proxy.resolve_proxies`` отдаёт ``{'http': url, 'https': url}``
    (форма requests). httpx такого не понимает: у него либо один ``proxy=``, либо
    ``mounts`` по схемам — а нам нужно уметь разные прокси для http и https,
    как позволял requests.
    """
    if not proxies:
        return None
    mounts: dict[str, Any] = {}
    for scheme, url in proxies.items():
        if not url:
            continue
        mounts[f"{scheme}://"] = httpx.AsyncHTTPTransport(proxy=url)
    return mounts or None


def get_async_client(
    *,
    verify: bool = True,
    cert: Optional[tuple] = None,
    proxies: Optional[dict] = None,
) -> httpx.AsyncClient:
    """Клиент для текущего loop. Переиспользуется — пул соединений живёт."""
    loop_key = id(asyncio.get_running_loop())
    proxy_key = tuple(sorted((proxies or {}).items()))
    key = (loop_key, bool(verify), cert, proxy_key)
    client = _clients.get(key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            verify=verify,
            cert=cert,
            mounts=mounts_from_proxies(proxies),
            trust_env=False,
            # Таймаут НЕ здесь: он у каждого запроса свой (backend.request_timeout_sec
            # против коротких проб), а клиент переиспользуется. Зашитый в клиент
            # таймаут молча применился бы к чужому запросу.
        )
        _clients[key] = client
    return client


async def aclose_clients() -> None:
    """Закрыть клиенты текущего loop.

    Зовётся на штатном завершении (``AsyncAgent.aclose``, остановка портала).
    Клиенты чужих loop не трогаем: закрывать их отсюда нечем — они живут на
    своих loop, и корректно закроет их только их владелец.
    """
    loop_key = id(asyncio.get_running_loop())
    for key in [k for k in _clients if k[0] == loop_key]:
        client = _clients.pop(key)
        await client.aclose()


__all__ = ["aclose_clients", "get_async_client", "mounts_from_proxies"]
