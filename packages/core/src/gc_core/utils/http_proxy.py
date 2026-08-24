"""
HTTP proxy resolver для backend-вызовов.

D1: trust_env=False, proxy только через явный proxies= на запросе.
D2: одно поле backend.proxy → {http, https} в dict.
D3: config → env (HTTPS_PROXY→HTTP_PROXY→ALL_PROXY) → None.
D4: NO_PROXY через utils.no_proxy.should_bypass_proxies (url-параметр обязателен).
D5: скоуп v1 — только backend, не тулы.

Proxies считаются НА ЗАПРОС (нужен url для D4), не один раз на сессию.
"""

from __future__ import annotations

from typing import Optional

from gc_core.utils.no_proxy import should_bypass_proxies


def _env_proxy(environ: dict[str, str]) -> Optional[str]:
    """
    D3: fallback в env — по приоритету.
    uppercase первым, потом lowercase.
    """
    for key in ("HTTPS_PROXY", "https_proxy"):
        val = environ.get(key)
        if val:
            return val
    for key in ("HTTP_PROXY", "http_proxy"):
        val = environ.get(key)
        if val:
            return val
    for key in ("ALL_PROXY", "all_proxy"):
        val = environ.get(key)
        if val:
            return val
    return None


def resolve_proxies(
    config_proxy: Optional[str],
    environ: dict[str, str],
    url: str,
) -> Optional[dict[str, str]]:
    """
    Резолв: (config_proxy, environ, url) → proxies dict | None.

    Правила:
    - config_proxy (strip) непустой → используется (D2 + D3), env игнорируется для proxy URL.
    - иначе env fallback (HTTPS_PROXY → HTTP_PROXY → ALL_PROXY).
    - иначе None (direct).
    - NO_PROXY из environ применяется к url через should_bypass_proxies —
      действует и при proxy из config, и при proxy из env (D4).

    Аргументы:
        config_proxy: значение поля backend.proxy (Optional[str]).
        environ:     словарь переменных окружения (напр. os.environ).
        url:         полный URL текущего backend-запроса (нужен для NO_PROXY).

    Возвращает:
        None или пустой dict — bypass (direct).
        dict{"http": proxy_url, "https": proxy_url} — проксировать.
    """
    # D3: берём proxy_url из config или env
    raw = config_proxy
    if raw is not None:
        raw = raw.strip()

    if raw:
        proxy_url = raw
    else:
        proxy_url = _env_proxy(environ)

    if not proxy_url:
        return None

    proxies = {"http": proxy_url, "https": proxy_url}

    # D4: NO_PROXY вручную — trust_env=False + явный proxies= не применяет его сам
    # Сливаем NO_PROXY из uppercase/lowercase; should_bypass_proxies принимает строку
    raw_no_proxy = environ.get("NO_PROXY", "") or environ.get("no_proxy", "")

    if raw_no_proxy and should_bypass_proxies(url, raw_no_proxy):
        return None

    return proxies