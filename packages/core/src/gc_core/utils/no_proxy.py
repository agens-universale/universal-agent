"""Матчинг ``NO_PROXY`` — единственное, чего нет в httpx как отдельной функции.

httpx умеет ``NO_PROXY`` только сам, при ``trust_env=True``. Нам это не
подходит: ``trust_env=True`` заодно включает чтение ``~/.netrc``, а Basic-auth
оттуда перетирает явный Authorization и даёт 401 (см. ``http_session``).
Поэтому прокси мы резолвим руками, а значит и ``NO_PROXY`` обязаны применять
сами.

Раньше здесь звалась ``requests.utils.should_bypass_proxies``. Поведение
воспроизведено ровно, включая пункты, которые легко упустить:

* сравнение идёт и с хостом, и с ``host:port``;
* ведущая точка в записи списка игнорируется (``.corp`` == ``corp``);
* совпадение по суффиксу — только по границе метки (``bad-corp`` НЕ
  подпадает под ``corp``);
* для IPv4 запись может быть CIDR (``10.0.0.0/8``) или точным адресом;
* ``*`` в списке отключает прокси для всего.

Эквивалентность закреплена тестом ``test_no_proxy_matches_requests``, который
сверяется с ``requests.utils.should_bypass_proxies`` напрямую, пока requests
ещё есть в окружении разработки.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


def _is_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return True


def should_bypass_proxies(url: str, no_proxy: str | None) -> bool:
    """True, если для ``url`` прокси применять НЕ надо."""
    if not no_proxy:
        return False

    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return False

    entries = [h for h in no_proxy.replace(" ", "").split(",") if h]
    if "*" in entries:
        return True

    if _is_ipv4(hostname):
        for entry in entries:
            if "/" in entry:
                try:
                    network = ipaddress.IPv4Network(entry, strict=False)
                except ValueError:
                    continue
                if ipaddress.IPv4Address(hostname) in network:
                    return True
            elif hostname == entry:
                return True
        return False

    host_with_port = hostname
    if parsed.port:
        host_with_port += f":{parsed.port}"

    for entry in entries:
        entry = entry.lstrip(".")
        if hostname == entry or host_with_port == entry:
            return True
        dotted = "." + entry
        if hostname.endswith(dotted) or host_with_port.endswith(dotted):
            return True
    return False


__all__ = ["should_bypass_proxies"]
