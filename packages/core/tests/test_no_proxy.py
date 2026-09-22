"""``NO_PROXY``: своя реализация вместо requests.utils.should_bypass_proxies.

Отдельный модуль появился, когда из ядра убирали ``requests``. httpx
применяет ``NO_PROXY`` только сам, при ``trust_env=True``, а нам это не
подходит: вместе с прокси включилось бы чтение ``~/.netrc``, чей Basic-auth
перетирает явный Authorization и даёт 401.
"""

import itertools
import json
from pathlib import Path

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


#: Эталон, снятый с ``requests.utils.should_bypass_proxies`` в момент отказа от
#: requests. Без него сверка была ЖИВОЙ и висела на модульном
#: ``importorskip("requests.utils")`` — а requests из зависимостей ядра ушёл, и
#: в чистой установке пропускался ВЕСЬ файл, включая проверки выше, которые
#: requests не нужны вовсе. Таблица делает эквивалентность проверяемой везде.
_REFERENCE = json.loads(
    (Path(__file__).parent / "fixtures" / "no_proxy_reference_requests.json").read_text(encoding="utf-8")
)["pairs"]


def test_reference_covers_every_pair():
    """Эталон обязан покрывать всю решётку — иначе он тихо усохнет."""
    assert len(_REFERENCE) == len(_URLS) * len(_LISTS), (
        f"в эталоне {len(_REFERENCE)} пар, а решётка даёт {len(_URLS) * len(_LISTS)} — пересними fixture"
    )
    assert any(r[2] for r in _REFERENCE) and not all(r[2] for r in _REFERENCE), (
        "эталон вырожден: все ответы одинаковые, сверять нечего"
    )


@pytest.mark.parametrize("url,no_proxy,expected", _REFERENCE)
def test_no_proxy_matches_the_requests_reference(url, no_proxy, expected):
    """Поведение совпадает с requests пара-в-пару.

    Это и есть доказательство, что переезд с requests ничего не изменил.
    """
    assert should_bypass_proxies(url, no_proxy) is expected


@pytest.mark.parametrize("url,no_proxy", list(itertools.product(_URLS, _LISTS)))
def test_reference_still_matches_live_requests(url, no_proxy):
    """Там, где requests ещё стоит, сверяем и ЖИВОЙ ответ.

    Страхует от протухшего эталона: если requests изменит поведение, таблицу
    надо пересматривать осознанно, а не узнавать об этом от пользователя.
    Пропускается там, где requests нет, — и пропускается ОДИН тест, а не файл.
    """
    requests_utils = pytest.importorskip("requests.utils")

    assert should_bypass_proxies(url, no_proxy) == requests_utils.should_bypass_proxies(url, no_proxy=no_proxy)
