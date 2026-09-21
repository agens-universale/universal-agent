"""Общий синхронный HTTP-клиент для backend/auth/probe: ``trust_env=False``.

Вынесено из connect_backend, чтобы prod_auth/model_picker не создавали
циклический импорт connect_backend → config_modules → auth → prod_auth.

Клиент без чтения ``~/.netrc``: и requests, и httpx при ``trust_env=True``
читают его, а Basic-auth оттуда перетирает явный Authorization-заголовок и
даёт 401. ``trust_env=False`` заодно отключает env-прокси и CA из окружения —
CA передаём явно через ``verify=``, прокси через ``resolve_proxies``.
Без cookies и auth — общий клиент безопасно звать из разных потоков.

Имя ``NO_NETRC_SESSION`` историческое (была ``requests.Session``) и оставлено:
на него ссылаются вызывающие и тесты, а смысл прежний.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx


class _CompatClient(httpx.Client):
    """httpx.Client, принимающий пару kwargs в форме requests.

    Переезд на httpx менял бы иначе все call-site разом: у requests таймаут,
    прокси и клиентский сертификат задаются НА ЗАПРОСЕ, у httpx — при
    создании клиента (кроме таймаута). Здесь разница гасится в одном месте.

    ``proxies={'http': url, 'https': url}`` на запросе httpx не понимает
    вовсе, и молча проигнорировать его нельзя: запрос ушёл бы мимо прокси.
    Поэтому под такой вызов поднимается отдельный короткоживущий клиент с
    нужными ``mounts``.
    """

    # get/post переопределены отдельно: у httpx.Client они объявлены с ЯВНОЙ
    # сигнатурой и отсеивают неизвестные kwargs до того, как управление дойдёт
    # до request() — verify=... там падает TypeError, а не доезжает сюда.
    def get(self, url: Any, **kwargs: Any) -> httpx.Response:  # type: ignore[override]
        return self.request("GET", url, **kwargs)

    def post(self, url: Any, **kwargs: Any) -> httpx.Response:  # type: ignore[override]
        return self.request("POST", url, **kwargs)

    def request(self, method: str, url: Any, **kwargs: Any) -> httpx.Response:  # type: ignore[override]
        proxies = kwargs.pop("proxies", None)
        verify = kwargs.pop("verify", None)
        cert = kwargs.pop("cert", None)
        # `fresh_client=True` — «не бери общий, подними свой». Нужно ровно там,
        # где изменился САМ ssl-модуль: `truststore.inject_into_ssl()` влияет
        # на контексты, созданные после инъекции, а контекст общего клиента
        # построен при импорте. Повтор общим клиентом шёл бы со старыми
        # корнями — и падал так же (голос, `with_system_trust_retry`).
        fresh = bool(kwargs.pop("fresh_client", False))
        # allow_redirects — имя из requests; у httpx оно follow_redirects.
        if "allow_redirects" in kwargs:
            kwargs["follow_redirects"] = kwargs.pop("allow_redirects")

        # Одноразовый клиент — только если настройки ОТЛИЧАЮТСЯ от общих.
        #
        # Условие было `verify is not None`, а вызывающие передают `verify=`
        # всегда: общий пул не использовался НИ РАЗУ, и каждый запрос платил
        # своим TLS-рукопожатием. `verify=True` без прокси и клиентского
        # сертификата — это ровно то, с чем создан общий клиент.
        needs_own = (
            fresh or bool(proxies) or cert is not None or verify not in (None, True)
        )
        if needs_own:
            from gc_core.utils.async_http import build_ssl_context, mounts_from_proxies

            ssl_context = build_ssl_context(
                True if verify is None else verify, cert
            )
            with httpx.Client(
                verify=ssl_context,
                mounts=_sync_mounts(proxies, ssl_context),
                trust_env=False,
            ) as one_shot:
                return one_shot.request(method, url, **kwargs)

        return super().request(method, url, **kwargs)


def _sync_mounts(proxies: Optional[dict], ssl_context: Any) -> Optional[dict]:
    """То же, что ``mounts_from_proxies``, но транспорт синхронный."""
    if not proxies:
        return None
    mounts = {}
    for scheme, proxy_url in proxies.items():
        if not proxy_url:
            continue
        mounts[f"{scheme}://"] = httpx.HTTPTransport(
            proxy=proxy_url,
            verify=True if ssl_context is None else ssl_context,
            trust_env=False,
        )
    return mounts or None


NO_NETRC_SESSION = _CompatClient(trust_env=False, follow_redirects=True)
