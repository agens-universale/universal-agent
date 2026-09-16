"""Сеть голосового пути: corp-TLS и единая точка POST.

Corp-TLS: за перехватывающим прокси (MITM с корп-CA) python-стек
(requests/huggingface_hub → certifi) не доверяет корп-CA из системного хранилища →
SSLError, хотя браузеры работают. Лечение — как у uv: truststore.inject_into_ssl()
переключает создание SSLContext на системное хранилище. Инъекция процесс-глобальна,
поэтому включаем её ТОЛЬКО по факту TLS-ошибки в voice-пути и ровно один раз на
процесс.

POST: `post()` — единственный способ голоса сходить в сеть. Клиент по умолчанию
сюда не годится: при trust_env=True он читает ~/.netrc, и Basic-auth оттуда
ПЕРЕТИРАЕТ явный Authorization → 401 на рабочем токене (грабля, которую backend
уже обошёл через utils.http_session.NO_NETRC_SESSION). Этот клиент — httpx:
ядро переехало на него целиком, и голос ходит в сеть им же."""

from __future__ import annotations

import os
import ssl
from typing import Callable, Optional, TypeVar

from gc_core.utils.url_addr import is_loopback

T = TypeVar("T")

_injected = False


def is_tls_error(exc: BaseException) -> bool:
    """TLS ли это — по всей цепочке причин, а не по верхнему типу.

    httpx отдаёт провал рукопожатия как `ConnectError`, и настоящая причина лежит
    глубже: `httpx.ConnectError -> httpcore.ConnectError -> ssl.SSLError`
    (проверено на самоподписанном сертификате). huggingface_hub заворачивает свои
    сетевые ошибки так же. Раньше здесь стоял ещё и `requests.exceptions.SSLError`,
    но POST давно идёт клиентом ядра на httpx, и совпасть тот тип уже не мог.
    """
    seen = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, ssl.SSLError):
            return True
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return False


def _inject() -> None:
    import truststore

    truststore.inject_into_ssl()


def with_system_trust_retry(fn: Callable[[], T]) -> T:
    """fn(); на TLS-ошибку — однократная инъекция системного траста и повтор.

    Повтор осмыслен РОВНО после инъекции: она и есть то, что меняет исход. Если траст
    уже переключён в этом процессе, TLS-ошибка означает настоящую беду с сертификатом
    (просрочен, чужой хост) — и второй заход просто отправит весь WAV по сети ещё раз
    и вдвое отложит «STT недоступен» для человека."""
    global _injected
    try:
        return fn()
    except Exception as exc:
        if not is_tls_error(exc) or _injected:
            raise
        _inject()
        _injected = True
        return fn()


def reset_for_tests() -> None:
    global _injected
    _injected = False


def post(url: str, *, config_proxy: Optional[str] = None, **kwargs):
    """POST голосового пути: общая сессия агента + прокси по его же правилам.

    Сессия — `NO_NETRC_SESSION` (trust_env=False): не читает ~/.netrc и не подменяет
    им Authorization. Плата за trust_env=False — сессия перестаёт видеть и env-прокси,
    поэтому прокси считаем явно тем же резолвером, что backend: config → env → прямо.
    Loopback исключаем всегда: локальная батарейка живёт на этой же машине.
    """
    from gc_core.utils.http_proxy import resolve_proxies
    from gc_core.utils.http_session import NO_NETRC_SESSION

    proxies = None if is_loopback(url) else resolve_proxies(config_proxy, os.environ, url)
    return NO_NETRC_SESSION.post(url, proxies=proxies, **kwargs)
