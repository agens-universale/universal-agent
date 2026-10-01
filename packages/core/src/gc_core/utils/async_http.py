"""Пер-loop реестр ``httpx.AsyncClient`` (план async-переезда, Задача 8).

Процессно-глобальный клиент, как синхронная ``NO_NETRC_SESSION`` у requests,
здесь невозможен: ``AsyncClient`` держит пул соединений, привязанный к своему
event loop. Клиент, созданный на одном loop и переиспользованный на loop
приложения, даёт «Event loop is closed» в самом неудобном месте — на живом
запросе к модели. Поэтому ключ кэша включает ``id(loop)``.

Контракт по окружению — тот же, что у синхронной сессии: ``trust_env=False``.
``~/.netrc`` не читаем (Basic-auth оттуда перетирает явный Authorization и даёт
401), env-прокси и REQUESTS_CA_BUNDLE не подхватываем — CA и прокси передаём
явно.
"""

from __future__ import annotations

import asyncio
import os
import ssl
import threading
import weakref
from typing import TYPE_CHECKING, Any, Optional, Union

if TYPE_CHECKING:
    import httpx


#: ``ключ -> (слабая ссылка на loop, клиент)``. Слабая ссылка — не украшение.
#:
#: Ключ включает ``id(loop)``, а id в CPython ПЕРЕИСПОЛЬЗУЮТСЯ: у loop, созданных
#: подряд через ``asyncio.run``, адреса повторяются (проверено запуском — шестой
#: совпал с третьим). Без сверки самого объекта новый loop получил бы клиента
#: МЁРТВОГО, и запрос к модели падал бы «Event loop is closed» в самом неудобном
#: месте. Пока агент жил на одном долгом loop, дыра была недостижима;
#: с переходом фронтендов на свой ``asyncio.run`` на прогон — достижима.
_clients: dict[tuple, tuple] = {}


def build_ssl_context(
    verify: Union[bool, str] = True,
    cert: Optional[tuple] = None,
    *,
    trust_env: bool = False,
) -> ssl.SSLContext:
    """Собрать TLS-контекст из пары ``verify``/``cert``.

    Собираем сами, а не отдаём httpx: в его ``create_ssl_context`` ветка
    ``verify=<str>`` стоит РАНЬШЕ блока ``if cert:`` и завершается ранним
    return — при CA-бандле, заданном путём, клиентский сертификат молча не
    загружается. Провал тихий: соединение встанет, сервер просто не увидит
    сертификат и ответит 401/403, уводя диагностику в сторону токена.
    ``requests`` обрабатывал verify и cert независимо, и этот контракт мы
    обязаны сохранить.

    Клиентский сертификат загружается независимо от проверки сервера:
    ``verify=False`` не должен отключать mTLS. Только явный False выключает
    проверку; CA-путь сохраняет проверку цепочки и имени сервера.

    ``trust_env`` учитывает SSL_CERT_FILE/SSL_CERT_DIR только при verify=True,
    как httpx. Для MCP включён ради корпоративных CA; клиенты модели
    по-прежнему берут CA из явного конфига. На netrc и прокси флаг не влияет.
    """
    import httpx

    if verify is False:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # Явный opt-in verify=False; обоснование: CONTRIBUTING.md.
        ctx.check_hostname = False  # NOSONAR: явный opt-in
        ctx.verify_mode = ssl.CERT_NONE  # NOSONAR: явный opt-in
    elif isinstance(verify, str):
        verify = os.path.expanduser(os.path.expandvars(verify))
        ctx = ssl.create_default_context(
            capath=verify if _is_dir(verify) else None,
            cafile=None if _is_dir(verify) else verify,
        )
    else:
        ctx = httpx.create_ssl_context(verify=True, trust_env=trust_env)
    if cert:
        if isinstance(cert, str):
            ctx.load_cert_chain(cert)
        else:
            ctx.load_cert_chain(*cert)
    return ctx


def _is_dir(path: str) -> bool:
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
    import httpx

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


#: Лок на модульные реестры.
#:
#: Потоков с собственным loop в процессе несколько ПО ДИЗАЙНУ: рабочий поток
#: TUI, guard-потоки модерации, воркеры сервера. Итерация
#: ``[k for k in _clients ...]`` при вставке из другого потока даёт
#: ``dictionary changed size during iteration`` — и вылетает она из ``finally``
#: в ``_closing_clients``, то есть подменяет результат судьи модерации на
#: отказ. Дефект воспроизведён стрессом.
#:
#: GIL сам по себе тут не спасает: он атомарен на уровне байткода, а цикл по
#: словарю — это много байткодов.
_registry_lock = threading.Lock()


def get_async_client(
    *,
    verify: Union[bool, str] = True,
    cert: Optional[tuple] = None,
    proxies: Optional[dict] = None,
) -> httpx.AsyncClient:
    """Клиент для текущего loop. Переиспользуется — пул соединений живёт."""
    import httpx

    loop = asyncio.get_running_loop()
    loop_key = id(loop)
    proxy_key = tuple(sorted((proxies or {}).items()))
    # verify кладём в ключ КАК ЕСТЬ, а не bool(verify): при bool() два разных
    # CA-бандла попадали в одну ячейку, и вызывающий получал клиент с чужим
    # корнем доверия — чьим именно, решал порядок вызовов.
    key = (loop_key, verify, cert, proxy_key)
    # Весь get-or-create ПОД ЛОКОМ, а не только чтение: иначе два потока
    # создают по клиенту, один затирает другого, и затёртый течёт сокетом.
    # Создание клиента не ходит в сеть, так что лок держится микросекунды.
    with _registry_lock:
        entry = _clients.get(key)
        client = None
        if entry is not None:
            loop_ref, cached = entry
            # Тот ли это loop, для которого клиент создан: id совпал, а объект
            # мог смениться (см. комментарий у `_clients`).
            client = cached if loop_ref() is loop else None
        if client is not None and not client.is_closed:
            return client

        ssl_context = build_ssl_context(verify, cert)
        client = httpx.AsyncClient(
            verify=ssl_context,
            mounts=mounts_from_proxies(proxies, ssl_context),
            trust_env=False,
            # Редиректы следуем, как синхронная `NO_NETRC_SESSION` и как
            # `requests.Session`, откуда транспорт переезжал: у httpx дефолт
            # обратный, и `raise_for_status()` бросает уже на 3xx. Ретрай это
            # не спасал — 307 не 429 и не 5xx. Корпоративные шлюзы отвечают
            # 307/308 на нормализацию http→https или завершающего слэша у
            # `/v1/chat/completions`; такие конфиги работали и обязаны работать.
            # 307/308 сохраняют метод и тело, то есть POST остаётся POST.
            follow_redirects=True,
            # Таймаут НЕ здесь: он у каждого запроса свой (backend.request_timeout_sec
            # против коротких проб), а клиент переиспользуется. Зашитый в клиент
            # таймаут молча применился бы к чужому запросу.
        )
        # Прибираем записи мёртвых loop'ов. Раньше их снимала только защита
        # «id совпал, а объект другой» — то есть лишь когда id ПЕРЕИСПОЛЬЗОВАЛИ.
        # Процесс, который гоняет много коротких `asyncio.run` (TUI — по
        # одному на ход), копил записи без счёта: сокеты закрывал выход из
        # `asyncio.run`, а вот словарь рос до конца процесса.
        _purge_dead_locked()
        _clients[key] = (weakref.ref(loop), client)
        return client


def _purge_dead_locked() -> None:
    """Снять записи, чей loop уже собран. Звать ТОЛЬКО под ``_registry_lock``."""
    dead = [k for k, (loop_ref, _c) in _clients.items() if loop_ref() is None]
    for k in dead:
        _clients.pop(k, None)


#: Сколько владельцев (обычно ``AsyncAgent``) держат пул этого loop.
#: Значение — ``(weakref на loop, счётчик)``: голого ``id`` мало по той же
#: причине, что и у ``_clients`` — id переиспользуются между последовательными
#: ``asyncio.run``, и новый loop унаследовал бы чужой счётчик.
_holders: dict[int, tuple[weakref.ref, int]] = {}


def _holder_count(loop) -> int:
    entry = _holders.get(id(loop))
    if entry is None:
        return 0
    loop_ref, count = entry
    return count if loop_ref() is loop else 0


def acquire_clients() -> None:
    """Заявить права на пул текущего loop.

    Пул общий на loop, а агентов на нём может быть несколько. Без учёта
    владельцев ``aclose`` одного агента закрывал бы клиенты, которыми прямо
    сейчас пользуется соседний, и рвал его запрос к модели на полуслове.
    """
    loop = asyncio.get_running_loop()
    with _registry_lock:
        _holders[id(loop)] = (weakref.ref(loop), _holder_count(loop) + 1)


async def release_clients() -> None:
    """Отпустить пул текущего loop; закрыть, когда ушёл последний владелец.

    Лишний вызов — НЕ повод закрывать пул. Счётчик считался как
    ``left = count - 1`` без нижней границы, поэтому повторный ``aclose``
    того же агента давал ``-1``, ветка ``left > 0`` не срабатывала, и пул
    закрывался целиком — вместе с клиентами СОСЕДНЕГО агента, чей запрос в
    этот момент шёл. Защита «aclose одного не рвёт запрос другого» обходилась
    одной лишней строкой у вызывающего.
    """
    loop = asyncio.get_running_loop()
    with _registry_lock:
        count = _holder_count(loop)
    if count <= 0:
        # Владельцев и не было: закрывать нечего, а закрыть можно чужое.
        return
    left = count - 1
    with _registry_lock:
        if left > 0:
            _holders[id(loop)] = (weakref.ref(loop), left)
        else:
            _holders.pop(id(loop), None)
    if left > 0:
        return
    await aclose_clients()


async def aclose_clients() -> None:
    """Безусловно закрыть клиенты текущего loop.

    Процедура завершения процесса (конец ``gc_cli.arun_cli``, остановка
    приложения), а НЕ способ попрощаться одному из агентов — для этого есть
    ``release_clients``. Клиенты чужих loop не трогаем: закрывать их отсюда
    нечем — они живут на своих loop, и корректно закроет их только владелец.
    """
    loop_key = id(asyncio.get_running_loop())
    with _registry_lock:
        _holders.pop(loop_key, None)
        # Снимок ПОД ЛОКОМ и материализованный: итерировать словарь, в который
        # в этот момент пишет соседний поток, нельзя.
        mine = [k for k in list(_clients) if k[0] == loop_key]
        taken = [_clients.pop(k) for k in mine]
    for _loop_ref, client in taken:
        await client.aclose()


__all__ = [
    "aclose_clients",
    "acquire_clients",
    "build_ssl_context",
    "get_async_client",
    "mounts_from_proxies",
    "release_clients",
]
