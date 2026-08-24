"""``NO_PROXY``: своя реализация вместо requests.utils.should_bypass_proxies.

Отдельный модуль появился, когда из ядра убирали ``requests``. httpx
применяет ``NO_PROXY`` только сам, при ``trust_env=True``, а нам это не
подходит: вместе с прокси включилось бы чтение ``~/.netrc``, чей Basic-auth
перетирает явный Authorization и даёт 401.
"""
import itertools

import pytest

from gc_core.utils.no_proxy import should_bypass_proxies

#: Пары «URL × список NO_PROXY», покрывающие все ветки матчинга.
_URLS = [
    "http://corp.example.com/x",
    "https://corp.example.com:8443/x",
    "http://api.corp.example.com/",
    "http://bad-corp.example.com/",
    "http://example.com/",
    "http://10.1.2.3:9000/",
    "http://192.168.5.5/",
    "http://127.0.0.1:8080/",
    "http://localhost/",
    "http://corp/",
]
_LISTS = [
    "",
    "*",
    "corp.example.com",
    ".corp.example.com",
    "example.com",
    "corp.example.com:8443",
    "10.0.0.0/8",
    "10.1.2.3",
    "192.168.0.0/16",
    "127.0.0.1",
    "localhost",
    "corp",
    "example.com,10.0.0.0/8",
    " example.com , corp ",
    "nomatch.test",
]


def test_empty_no_proxy_never_bypasses():
    assert should_bypass_proxies("http://any.host/", "") is False
    assert should_bypass_proxies("http://any.host/", None) is False


def test_star_bypasses_everything():
    assert should_bypass_proxies("http://any.host/", "*") is True


def test_suffix_match_respects_label_boundary():
    """`bad-corp.example.com` НЕ подпадает под `corp.example.com`.

    Наивный ``endswith`` без точки дал бы здесь ложное совпадение и увёл
    трафик мимо прокси.
    """
    assert should_bypass_proxies("http://api.corp.example.com/", "corp.example.com")
    assert not should_bypass_proxies("http://bad-corp.example.com/", "corp.example.com")


def test_leading_dot_is_equivalent():
    for entry in ("corp.example.com", ".corp.example.com"):
        assert should_bypass_proxies("http://corp.example.com/", entry)


def test_port_is_matched_both_ways():
    assert should_bypass_proxies("https://corp.example.com:8443/", "corp.example.com:8443")
    assert not should_bypass_proxies("https://corp.example.com:8443/", "corp.example.com:9999")
    # без порта в записи хост совпадает независимо от порта
    assert should_bypass_proxies("https://corp.example.com:8443/", "corp.example.com")


def test_ipv4_cidr_and_exact():
    assert should_bypass_proxies("http://10.1.2.3:9000/", "10.0.0.0/8")
    assert should_bypass_proxies("http://10.1.2.3/", "10.1.2.3")
    assert not should_bypass_proxies("http://11.1.2.3/", "10.0.0.0/8")


def test_broken_cidr_does_not_explode():
    assert should_bypass_proxies("http://10.1.2.3/", "10.0.0.0/99") is False


requests_utils = pytest.importorskip(
    "requests.utils",
    reason="requests убран из зависимостей ядра; сверка возможна только там, "
           "где он ещё стоит как транзитивный",
)


@pytest.mark.parametrize("url,no_proxy", list(itertools.product(_URLS, _LISTS)))
def test_no_proxy_matches_requests(url, no_proxy):
    """Поведение совпадает с requests пара-в-пару.

    Это и есть доказательство, что переезд с requests ничего не изменил.
    Тест сам себя выключает там, где requests уже нет.
    """
    assert should_bypass_proxies(url, no_proxy) == requests_utils.should_bypass_proxies(
        url, no_proxy=no_proxy
    )
