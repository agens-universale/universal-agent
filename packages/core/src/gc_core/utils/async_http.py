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
import ssl
from typing import Any, Optional, Union

import httpx

_clients: dict[tuple, httpx.AsyncClient] = {}


def build_ssl_context(
    verify: Union[bool, str] = True,
    cert: Optional[tuple] = None,
) -> Union[ssl.SSLContext, bool]:
    """Собрать TLS-контекст из пары ``verify``/``cert``.

    Собираем сами, а не отдаём httpx: в его ``create_ssl_context`` ветка
    ``verify=<str>`` стоит РАНЬШЕ блока ``if cert:`` и завершается ранним
    return — при CA-бандле, заданном путём, клиентский сертификат молча не
    загружается. Провал тихий: соединение встанет, сервер просто не увидит
    сертификат и ответит 401/403, уводя диагностику в сторону токена.
    ``requests`` обрабатывал verify и cert независимо, и этот контракт мы
    обязаны сохранить.

    ``verify=False`` возвращаем как есть — httpx сам сделает контекст без
    проверки; собирать его руками смысла нет.
    """
    if verify is False:
        return False
    if isinstance(verify, str):
        ctx = ssl.create_default_context(
            capath=verify if _is_dir(verify) else None,
            cafile=None if _is_dir(verify) else verify,
        )
    else:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
    if cert:
        if isinstance(cert, str):
            ctx.load_cert_chain(cert)
        else:
            ctx.load_cert_chain(*cert)
    return ctx


def _is_dir(path: str) -> bool:
    import os

    return os.path.isdir(path)


def mounts_from_proxies(
    proxies: Optional[dict],
    ssl_context: Union[ssl.SSLContext, bool, None] = None,
) -> Optional[dict]:
    """Перевести requests-словарь прокси в httpx-``mounts``.

    ``utils.http_proxy.resolve_proxies`` отдаёт ``{'http': url, 'https': url}``
    (форма requests). httpx такого не понимает: у него либо один ``proxy=``, либо
    ``mounts`` по схемам — а нам нужно уметь разные прокси для http и https,
    как позволял requests.

    ``ssl_context`` передаём в каждый транспорт: без него mount строится на
    дефолтах httpx, и настроенный прокси молча отменял бы корпоративный CA и
    клиентский сертификат — запросы шли бы мимо TLS-настроек вызывающего.
    """
    if not proxies:
        return None
    mounts: dict[str, Any] = {}
    for scheme, url in proxies.items():
        if not url:
            continue
        mounts[f"{scheme}://"] = httpx.AsyncHTTPTransport(
            proxy=url,
            verify=True if ssl_context is None else ssl_context,
            trust_env=False,
        )
    return mounts or None


def get_async_client(
    *,
    verify: Union[bool, str] = True,
    cert: Optional[tuple] = None,
    proxies: Optional[dict] = None,
) -> httpx.AsyncClient:
    """Клиент для текущего loop. Переиспользуется — пул соединений живёт."""
    loop_key = id(asyncio.get_running_loop())
    proxy_key = tuple(sorted((proxies or {}).items()))
    # verify кладём в ключ КАК ЕСТЬ, а не bool(verify): при bool() два разных
    # CA-бандла попадали в одну ячейку, и вызывающий получал клиент с чужим
    # корнем доверия — чьим именно, решал порядок вызовов.
    key = (loop_key, verify, cert, proxy_key)
    client = _clients.get(key)
    if client is None or client.is_closed:
        ssl_context = build_ssl_context(verify, cert)
        client = httpx.AsyncClient(
            verify=ssl_context,
            mounts=mounts_from_proxies(proxies, ssl_context),
            trust_env=False,
            # Таймаут НЕ здесь: он у каждого запроса свой (backend.request_timeout_sec
            # против коротких проб), а клиент переиспользуется. Зашитый в клиент
            # таймаут молча применился бы к чужому запросу.
        )
        _clients[key] = client
    return client


#: Сколько владельцев (обычно ``AsyncAgent``) держат пул этого loop.
_holders: dict[int, int] = {}


def acquire_clients() -> None:
    """Заявить права на пул текущего loop.

    Пул общий на loop, а агентов на нём может быть несколько. Без учёта
    владельцев ``aclose`` одного агента закрывал бы клиенты, которыми прямо
    сейчас пользуется соседний, и рвал его запрос к модели на полуслове.
    """
    loop_key = id(asyncio.get_running_loop())
    _holders[loop_key] = _holders.get(loop_key, 0) + 1


async def release_clients() -> None:
    """Отпустить пул текущего loop; закрыть, когда ушёл последний владелец."""
    loop_key = id(asyncio.get_running_loop())
    left = _holders.get(loop_key, 0) - 1
    if left > 0:
        _holders[loop_key] = left
        return
    _holders.pop(loop_key, None)
    await aclose_clients()


async def aclose_clients() -> None:
    """Безусловно закрыть клиенты текущего loop.

    Процедура завершения процесса (конец ``gc_cli.amain``, остановка
    приложения), а НЕ способ попрощаться одному из агентов — для этого есть
    ``release_clients``. Клиенты чужих loop не трогаем: закрывать их отсюда
    нечем — они живут на своих loop, и корректно закроет их только владелец.
    """
    loop_key = id(asyncio.get_running_loop())
    _holders.pop(loop_key, None)
    for key in [k for k in _clients if k[0] == loop_key]:
        client = _clients.pop(key)
        await client.aclose()


__all__ = [
    "acquire_clients",
    "aclose_clients",
    "build_ssl_context",
    "get_async_client",
    "mounts_from_proxies",
    "release_clients",
]
