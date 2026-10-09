"""Разбор адреса: ведёт ли URL «внутрь машины».

Только стандартная библиотека, и это намеренно. Правило нужно трём разным местам —
загрузчику конфига (`config_modules.voice`), автозапуску голосового сервиса и
голосовому HTTP-пути, — а жило оно в `voice_runtime.net` рядом с `requests`. Из-за
соседства каждый потребитель был вынужден импортировать его ЛЕНИВО, лишь бы не
тащить `requests` на горячий путь: два таких обхода стояли в
`voice_runtime/autostart.py` и `voice_runtime/wire.py`, оба с объяснением в
комментарии. Вынос в модуль без зависимостей снимает и обходы, и их причину.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

#: Хосты-ИМЕНА, которые ведут «внутрь машины»: разбору `ipaddress` они не поддаются,
#: поэтому перечислены явно. `0.0.0.0` здесь потому, что как ЦЕЛЬ запроса означает
#: «эта машина», хотя петлёй в смысле `ipaddress` он не считается. Числовые адреса
#: сюда не входят — их разбирает `is_loopback`.
_LOOPBACK_NAMES = frozenset({"localhost", "0.0.0.0"})


def is_loopback(url: str) -> bool:
    """Адрес ведёт «внутрь машины» — туда, где живёт локальная батарейка.

    Числовой хост разбираем как АДРЕС, а не как строку. Прежняя проверка
    `host.startswith("127.")` пропускала `http://127.0.0.1.evil.com:8735/…` — имя
    начинается на «127.», но ведёт наружу. Последствия те же, ради которых сосед
    `autostart.url_is_ours` отдельно оговаривает `localhost.чужой-домен.com`: запрос
    минует корп-прокси (`net.post` не резолвит его для петли), а автозапуск считает
    удалённый хост своим сервисом. Найдено сторожем `tests/utils/test_url_addr.py`
    27.08.2026 при выносе правила в общий модуль.

    ТА ЖЕ ДЫРА ОСТАЛАСЬ у копии правила в MCP-транспорте —
    `tools/connect_mcp/stream_client._is_loopback_url:171`. Свести копии и починить
    её — отдельная задача: она задевает поведение транспорта.
    """
    host = (urlsplit((url or "").strip()).hostname or "").lower()
    if not host:
        return False
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:  # не числовой адрес — значит обычное имя, наружу
        return False


def same_service(a: str, b: str) -> bool:
    """Адреса ведут на один сервис: тот же порт и оба — на этой машине.

    `localhost` и `127.0.0.1` человек пишет как привык, а сервис за ними один и тот же.

    Мусор вместо порта — не «тот же сервис», а НЕ он: `urlsplit(...).port` на таком
    бросает ValueError, и без этой обёртки опечатка в config.yml (`:87x5`) роняла
    ЗАПУСК АГЕНТА сырым исключением — сверка зовётся из загрузки настроек на каждом старте,
    как только прописана локальная батарейка. Сосед по правилу (`autostart.url_is_ours`)
    эту же ошибку ловит; здесь её когда-то пропустили.
    """
    try:
        port_a, port_b = urlsplit(a).port, urlsplit(b).port
    except ValueError:
        return False
    if port_a is None or port_a != port_b:
        return False
    return is_loopback(a) and is_loopback(b)
